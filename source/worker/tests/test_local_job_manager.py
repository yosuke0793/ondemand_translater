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
    def test_delete_selected_jobs_preserves_outputs_and_other_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            manager = LocalJobManager(root / "data")
            source = root / "input.mp4"
            source.write_bytes(b"original")
            first = manager.register_job(source)
            second = manager.register_job(source, output_directory=root / "exports")
            manager.cancel(first)
            first_dir = manager.jobs_dir / first
            output = Path(manager.get_job(first)["output_video"])
            output.write_bytes(b"completed")
            output.with_suffix(".srt").write_bytes(b"subtitle")
            (manager.logs_dir / f"{first}.log").write_bytes(b"log")
            manager.delete_jobs([first])
            self.assertIsNone(manager.get_job(first))
            self.assertIsNotNone(manager.get_job(second))
            self.assertEqual(output.read_bytes(), b"completed")
            self.assertEqual(output.with_suffix(".srt").read_bytes(), b"subtitle")
            self.assertFalse((first_dir / "input.mp4").exists())
            self.assertFalse((manager.logs_dir / f"{first}.log").exists())
            self.assertEqual(source.read_bytes(), b"original")
            with self.assertRaises(RuntimeError):
                manager.delete_jobs([second])
            self.assertIsNotNone(manager.get_job(second))
            manager.cancel(second)
            manager._active_job_id = second
            with self.assertRaises(RuntimeError):
                manager.delete_jobs([second])

    def test_delete_intermediates_preserves_history_logs_outputs_and_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            manager = LocalJobManager(root / "data")
            source = root / "input.mp4"
            source.write_bytes(b"original")
            job_id = manager.register_job(source)
            manager._update_status(job_id, "error")
            job_dir = manager.jobs_dir / job_id
            output = Path(manager.get_job(job_id)["output_video"])
            preserved = [
                output, output.with_suffix(".srt"), output.with_name(f"{output.stem}_ja.srt"),
                job_dir / "pipeline.log", manager.logs_dir / f"{job_id}.log",
                manager.data_dir / "settings.json", manager.data_dir / "engineering_terms_custom.csv",
            ]
            for path in preserved:
                path.write_bytes(b"keep")
            intermediates = [job_dir / "audio.wav", job_dir / "jp.srt", job_dir / "chunks" / "chunk.wav"]
            for path in intermediates:
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(b"intermediate")
            manager.delete_job_intermediates()
            for path in preserved:
                self.assertEqual(path.read_bytes(), b"keep")
            self.assertTrue((job_dir / "status.json").is_file())
            self.assertEqual(manager.get_job(job_id)["status"], "error")
            self.assertEqual(source.read_bytes(), b"original")
            for path in [job_dir / "input.mp4", *intermediates]:
                self.assertFalse(path.exists())
            self.assertEqual(manager.delete_job_intermediates(), 0)

    def test_delete_intermediates_refuses_queued_running_and_active_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            manager = LocalJobManager(root / "data")
            source = root / "input.mp4"
            source.write_bytes(b"original")
            job_id = manager.register_job(source)
            for status in ("queued", "running"):
                manager._update_status(job_id, status)
                with self.assertRaisesRegex(RuntimeError, "処理中"):
                    manager.delete_job_intermediates()
            manager._update_status(job_id, "cancelled")
            manager._active_job_id = job_id
            with self.assertRaises(RuntimeError):
                manager.delete_job_intermediates()
            self.assertTrue((manager.jobs_dir / job_id / "input.mp4").exists())

    def test_delete_intermediates_does_not_follow_symlinks_or_remove_other_jobs_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            manager = LocalJobManager(root / "data")
            source = root / "input.mp4"
            source.write_bytes(b"original")
            first = manager.register_job(source)
            first_dir = manager.jobs_dir / first
            second = manager.register_job(source, output_directory=first_dir / "exports")
            for job_id in (first, second):
                manager._update_status(job_id, "done")
            output = Path(manager.get_job(second)["output_video"])
            output.write_bytes(b"completed")
            outside = root / "external"
            outside.mkdir()
            (outside / "keep.wav").write_bytes(b"outside")
            (first_dir / "external-link").symlink_to(outside, target_is_directory=True)
            manager.delete_job_intermediates()
            self.assertEqual(output.read_bytes(), b"completed")
            self.assertEqual((outside / "keep.wav").read_bytes(), b"outside")

    def test_delete_intermediates_reports_filesystem_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            manager = LocalJobManager(root / "data")
            source = root / "input.mp4"
            source.write_bytes(b"original")
            job_id = manager.register_job(source)
            manager._update_status(job_id, "done")
            with patch.object(Path, "unlink", side_effect=OSError("permission denied")):
                with self.assertRaisesRegex(RuntimeError, "削除に失敗"):
                    manager.delete_job_intermediates()
            self.assertIsNotNone(manager.get_job(job_id))

    def test_new_user_has_no_history_and_reopening_preserves_existing_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            manager = LocalJobManager(root / "existing-user")
            self.assertEqual(manager.get_jobs(), [])
            source = root / "input.mp4"
            source.write_bytes(b"input")
            job_id = manager.register_job(source)
            reopened = LocalJobManager(root / "existing-user")
            self.assertIsNotNone(reopened.get_job(job_id))
            new_user = LocalJobManager(root / "new-user")
            self.assertEqual(new_user.get_jobs(), [])

    def test_voice_dropdown_labels_map_to_azure_names(self) -> None:
        self.assertEqual(VOICE_OPTIONS[DEFAULT_VOICE_LABEL], "en-US-AriaNeural")
        self.assertIn('"en-US-AndrewNeural"', next(iter(VOICE_OPTIONS)))

    def test_command_selects_piper_or_azure_and_rejects_removed_backends(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            manager = LocalJobManager(Path(temporary_dir))
            for backend, voice in (("piper", "en_US-ljspeech-medium"), ("azure", "en-US-AriaNeural")):
                command = manager._default_command({"options": {"tts_backend": backend}}, Path(temporary_dir))
                self.assertEqual(command[command.index("--tts-backend") + 1], backend)
                self.assertEqual(command[command.index("--voice") + 1], voice)
                self.assertNotIn("AZURE_SPEECH_KEY", " ".join(command))
            for backend, voice in (
                ("piper", "en_US-ljspeech-medium"), ("piper", "en_US-john-medium"),
                ("azure", "en-US-AriaNeural"), ("azure", "en-US-AndrewNeural"),
            ):
                command = manager._default_command(
                    {"options": {"tts_backend": backend, "voice": voice}}, Path(temporary_dir),
                )
                self.assertEqual(command[command.index("--voice") + 1], voice)
            for backend in ("edge-tts", "xtts-v2"):
                with self.assertRaises(ValueError):
                    manager._default_command({"options": {"tts_backend": backend}}, Path(temporary_dir))

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
