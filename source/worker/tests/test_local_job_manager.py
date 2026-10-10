from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from local_job_manager import LocalJobManager
from standalone_app import (
    DEFAULT_VOICE_LABEL,
    DEFAULT_GLOSSARY_PATH,
    VOICE_OPTIONS,
    load_preferences,
    merge_glossary_terms,
    read_glossary_csv,
    save_preferences,
    write_glossary_csv,
)


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

    def test_glossary_csv_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            glossary_path = Path(temporary_dir) / "terms.csv"
            terms = ["有限要素法", "ヤコビアン", "Δ"]
            write_glossary_csv(glossary_path, terms)
            self.assertEqual(read_glossary_csv(glossary_path), terms)

    def test_default_engineering_glossary_has_expected_categories(self) -> None:
        terms = read_glossary_csv(DEFAULT_GLOSSARY_PATH)
        self.assertGreaterEqual(len(terms), 800)

    def test_glossary_terms_are_accumulated_without_duplicates(self) -> None:
        self.assertEqual(
            merge_glossary_terms(["行列", "ベクトル"], ["ベクトル", "ヤコビアン", "ヤコビアン"]),
            ["行列", "ベクトル", "ヤコビアン"],
        )

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
                + ("pathlib.Path(sys.argv[2]).write_text('1\\n00:00:00,000 --> 00:00:01,000\\nEnglish\\n'); " if succeed else "")
                + ("pathlib.Path(sys.argv[3]).write_text('1\\n00:00:00,000 --> 00:00:01,000\\n日本語\\n'); " if succeed else "")
                + ("sys.exit(0)" if succeed else "sys.exit(1)")
            )
            return [
                sys.executable,
                "-c",
                script,
                str(job_dir / "final.mp4"),
                str(job_dir / "en_aligned.srt"),
                str(job_dir / "jp.srt"),
            ]

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
                self.assertTrue(Path(manager.get_job(first)["output_video"]).exists())
                self.assertTrue((output_dir / "input_translated.mp4").exists())
                self.assertTrue((output_dir / "input_translated.srt").exists())
                self.assertTrue((output_dir / "input_translated_ja.srt").exists())
                self.assertTrue((root / "done" / f"{first}.json").exists())
                self.assertTrue((root / "done" / f"{second}.json").exists())
            finally:
                manager.stop()

    def test_success_removes_job_videos_after_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "lecture.mp4"
            source.write_bytes(b"input")
            manager = self.make_manager(root / "data")
            output_dir = root / "exports"
            job_id = manager.register_job(source, output_directory=output_dir)
            job_dir = manager.jobs_dir / job_id
            for name in ("final.mp4", "final_sub.mp4", "final_soft_sub.mp4", "video_overlay.mp4"):
                (job_dir / name).write_bytes(b"intermediate")
            nested_dir = job_dir / "nested"
            nested_dir.mkdir()
            (nested_dir / "intermediate.MP4").write_bytes(b"intermediate")
            (nested_dir / "final_soft_sub.mkv").write_bytes(b"intermediate")
            (job_dir / "audio.wav").write_bytes(b"audio")
            unrelated_video = output_dir / "unrelated.mp4"
            unrelated_video.write_bytes(b"unrelated")
            other_job = manager.register_job(source, output_directory=output_dir)

            manager._run_job(manager.queue_dir / f"{job_id}.json")

            job = manager.get_job(job_id)
            self.assertEqual(job["status"], "done")
            self.assertEqual(job["progress_percent"], 100)
            self.assertEqual(Path(job["output_video"]).read_bytes(), b"intermediate")
            self.assertEqual(list(job_dir.rglob("*.mp4")), [])
            self.assertEqual(list(nested_dir.iterdir()), [])
            self.assertEqual(source.read_bytes(), b"input")
            self.assertEqual(unrelated_video.read_bytes(), b"unrelated")
            self.assertTrue((manager.jobs_dir / other_job / "input.mp4").exists())
            self.assertTrue((output_dir / "lecture_translated.srt").exists())
            self.assertTrue((output_dir / "lecture_translated_ja.srt").exists())
            self.assertTrue((job_dir / "audio.wav").exists())
            self.assertTrue((job_dir / "status.json").exists())
            self.assertTrue((manager.logs_dir / f"{job_id}.log").exists())

    def test_success_preserves_output_inside_job_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "lecture.mp4"
            source.write_bytes(b"input")
            manager = self.make_manager(root / "data")
            job_id = manager.register_job(source)
            job_dir = manager.jobs_dir / job_id

            manager._run_job(manager.queue_dir / f"{job_id}.json")

            job = manager.get_job(job_id)
            self.assertEqual(job["status"], "done")
            self.assertEqual(list(job_dir.glob("*.mp4")), [Path(job["output_video"])])
            self.assertEqual(Path(job["output_video"]).read_bytes(), b"video")
            self.assertTrue((job_dir / "lecture_translated.srt").exists())
            self.assertTrue((job_dir / "lecture_translated_ja.srt").exists())

    def test_export_failure_preserves_job_videos(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "lecture.mp4"
            source.write_bytes(b"input")
            manager = self.make_manager(root / "data")
            output_dir = root / "exports"
            job_id = manager.register_job(source, output_directory=output_dir)
            job_dir = manager.jobs_dir / job_id
            (output_dir / "lecture_translated.srt" / "en_aligned.srt").mkdir(parents=True)

            manager._run_job(manager.queue_dir / f"{job_id}.json")

            self.assertEqual(manager.get_job(job_id)["status"], "error")
            self.assertTrue((job_dir / "input.mp4").exists())
            self.assertTrue((job_dir / "output.mp4").exists())
            self.assertTrue((manager.error_dir / f"{job_id}.json").exists())

    def test_cleanup_failure_is_reported_as_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "lecture.mp4"
            source.write_bytes(b"input")
            manager = self.make_manager(root / "data")
            job_id = manager.register_job(source, output_directory=root / "exports")
            with patch.object(Path, "unlink", side_effect=PermissionError("cleanup denied")):
                manager._run_job(manager.queue_dir / f"{job_id}.json")

            job = manager.get_job(job_id)
            self.assertEqual(job["status"], "error")
            self.assertIn("cleanup denied", job["error_message"])
            self.assertTrue(Path(job["output_video"]).exists())
            self.assertTrue((manager.error_dir / f"{job_id}.json").exists())
            self.assertFalse((manager.done_dir / f"{job_id}.json").exists())

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
                self.assertTrue((root / "jobs" / job_id / "input.mp4").exists())
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

    def test_resume_inputs_are_staged_in_job_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "input.mp4"
            japanese_srt = root / "japanese.srt"
            english_srt = root / "english.srt"
            source.write_bytes(b"input")
            japanese_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n日本語\n", encoding="utf-8")
            english_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nEnglish\n", encoding="utf-8")
            manager = self.make_manager(root)

            japanese_job = manager.register_job(source, japanese_srt=japanese_srt)
            english_job = manager.register_job(source, english_srt=english_srt)
            japanese_and_english_job = manager.register_job(
                source,
                japanese_srt=japanese_srt,
                english_srt=english_srt,
            )

            japanese_dir = root / "jobs" / japanese_job
            english_dir = root / "jobs" / english_job
            both_dir = root / "jobs" / japanese_and_english_job
            self.assertEqual((japanese_dir / "jp.srt").read_text(encoding="utf-8"), japanese_srt.read_text(encoding="utf-8"))
            self.assertFalse((japanese_dir / "en.srt").exists())
            self.assertEqual((english_dir / "en.srt").read_text(encoding="utf-8"), english_srt.read_text(encoding="utf-8"))
            self.assertEqual((english_dir / "jp.srt").read_text(encoding="utf-8"), "")
            self.assertTrue((both_dir / "jp.srt").exists())
            self.assertTrue((both_dir / "en.srt").exists())

    def test_resume_jobs_do_not_use_force(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "input.mp4"
            english_srt = root / "english.srt"
            source.write_bytes(b"input")
            english_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nEnglish\n", encoding="utf-8")
            manager = LocalJobManager(root)

            job_id = manager.register_job(source, english_srt=english_srt)
            job = json.loads((root / "queue" / f"{job_id}.json").read_text(encoding="utf-8"))
            command = manager._default_command(job, root / "jobs" / job_id)
            self.assertNotIn("--force", command)

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
