from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from local_job_manager import LocalJobManager
from standalone_app import DEFAULT_VOICE_LABEL, VOICE_OPTIONS, load_preferences, save_preferences


class LocalJobManagerTests(unittest.TestCase):
    def test_voice_dropdown_labels_map_to_edge_tts_names(self) -> None:
        self.assertEqual(VOICE_OPTIONS[DEFAULT_VOICE_LABEL], "en-US-AriaNeural")
        self.assertIn('"en-US-AndrewNeural"', next(iter(VOICE_OPTIONS)))

    def test_preferences_are_saved_and_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            preferences_path = Path(temporary_dir) / "settings.json"
            preferences = {
                "output_directory": "/tmp/translated",
                "voice": "en-US-AndrewNeural",
            }
            save_preferences(preferences_path, preferences)
            self.assertEqual(load_preferences(preferences_path), preferences)

    def wait_for_status(self, manager: LocalJobManager, job_id: str, expected: str) -> dict:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = manager.get_job(job_id)
            if job and job["status"] == expected:
                return job
            time.sleep(0.05)
        self.fail(f"job {job_id} did not reach {expected}: {manager.get_job(job_id)}")

    def make_manager(self, root: Path, succeed: bool = True) -> LocalJobManager:
        def command_builder(job: dict, job_dir: Path) -> list[str]:
            script = (
                "import pathlib, sys, time; "
                "time.sleep(0.1); "
                + ("pathlib.Path(sys.argv[1]).write_bytes(b'video'); " if succeed else "")
                + ("sys.exit(0)" if succeed else "sys.exit(1)")
            )
            return [sys.executable, "-c", script, str(job_dir / "final.mp4")]

        return LocalJobManager(root, command_builder=command_builder, poll_interval=0.05)

    def test_jobs_run_in_fifo_order_and_finish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "input.mp4"
            source.write_bytes(b"input")
            manager = self.make_manager(root)
            manager.start()
            try:
                first = manager.register_job(source)
                output_dir = root / "exports"
                second = manager.register_job(source, output_directory=output_dir)
                self.wait_for_status(manager, first, "done")
                self.wait_for_status(manager, second, "done")
                self.assertEqual(manager.get_job(first)["input_filename"], "input.mp4")
                self.assertEqual(
                    [job["job_id"] for job in manager.get_jobs()], [second, first]
                )
                self.assertTrue((root / "jobs" / first / "output.mp4").exists())
                self.assertTrue((output_dir / "input_translated.mp4").exists())
                self.assertTrue((root / "done" / f"{first}.json").exists())
                self.assertTrue((root / "done" / f"{second}.json").exists())
            finally:
                manager.stop()

    def test_failed_process_is_marked_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "input.mp4"
            source.write_bytes(b"input")
            manager = self.make_manager(root, succeed=False)
            manager.start()
            try:
                job_id = manager.register_job(source)
                job = self.wait_for_status(manager, job_id, "error")
                self.assertIn("exit code", job["error_message"])
                status = json.loads((root / "jobs" / job_id / "status.json").read_text())
                self.assertEqual(status["status"], "error")
            finally:
                manager.stop()

    def test_non_mp4_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "input.mov"
            source.write_bytes(b"input")
            manager = self.make_manager(root)
            with self.assertRaises(ValueError):
                manager.register_job(source)

    def test_pipeline_log_updates_progress_and_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "lecture.mp4"
            source.write_bytes(b"input")
            manager = self.make_manager(root)
            job_id = manager.register_job(source)
            job_dir = root / "jobs" / job_id
            (job_dir / "pipeline.log").write_text(
                "[pipeline] START extract_audio\n"
                "[pipeline] DONE  extract_audio (1.0s)\n"
                "[pipeline] START transcribe_audio\n",
                encoding="utf-8",
            )
            manager._update_progress_from_log(job_id, job_dir / "pipeline.log")
            job = manager.get_job(job_id)
            self.assertEqual(job["status"], "running")
            self.assertEqual(job["current_stage"], "transcribe_audio")
            self.assertEqual(job["current_stage_label"], "日本語音声を文字起こし中")
            self.assertGreater(job["progress_percent"], 0)
            self.assertLess(job["progress_percent"], 100)


if __name__ == "__main__":
    unittest.main()
