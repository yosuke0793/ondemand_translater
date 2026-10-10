from __future__ import annotations

import json
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app_runtime
import bundled_worker
from credential_store import CredentialError, CredentialStore, SERVICE_NAME
from lecture_poc.pipeline import get_edge_tts_command
from local_job_manager import LocalJobManager
from standalone_app import StandaloneApp


class MemoryBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[service, username] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.values[service, username]


class CredentialStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = MemoryBackend()
        self.store = CredentialStore(self.backend)

    def test_save_read_delete_use_os_backend_only(self) -> None:
        self.store.save_key("gemini", " test-secret ")
        self.assertEqual(self.backend.values, {(SERVICE_NAME, "GEMINI_API_KEY"): "test-secret"})
        self.assertEqual(self.store.saved_key("gemini"), "test-secret")
        self.store.delete_key("gemini")
        self.store.delete_key("gemini")
        self.assertIsNone(self.store.saved_key("gemini"))

    def test_environment_is_explicitly_prioritized(self) -> None:
        self.store.save_key("gemini", "saved-secret")
        with patch.dict(os.environ, {"GEMINI_API_KEY": " environment-secret "}):
            self.assertEqual(self.store.effective_key("gemini"), "environment-secret")
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            self.assertEqual(self.store.effective_key("gemini"), "saved-secret")

    def test_blank_multiline_and_unknown_provider_rejected(self) -> None:
        for value in ("", " ", "secret\nvalue", "secret\rvalue"):
            with self.assertRaises(ValueError):
                self.store.save_key("gemini", value)
        with self.assertRaises(ValueError):
            self.store.save_key("unknown", "value")

    def test_backend_failure_never_exposes_secret_or_uses_plaintext_fallback(self) -> None:
        backend = Mock()
        backend.get_password.side_effect = OSError("accidentally included secret")
        backend.set_password.side_effect = OSError("accidentally included secret")
        backend.delete_password.side_effect = OSError("accidentally included secret")
        store = CredentialStore(backend)
        for action in (
            lambda: store.saved_key("gemini"),
            lambda: store.save_key("gemini", "secret"),
            lambda: store.delete_key("gemini"),
        ):
            with self.assertRaises(CredentialError) as captured:
                action()
            self.assertNotIn("secret", str(captured.exception))

    def test_resume_and_argos_do_not_access_credentials(self) -> None:
        with patch.object(self.store, "effective_key", side_effect=AssertionError("should not access")):
            self.assertEqual(self.store.worker_credentials("argos"), {})
            self.assertEqual(self.store.worker_credentials("gemini", False), {})

    def test_missing_key_fails_explicitly(self) -> None:
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            with self.assertRaises(CredentialError):
                self.store.worker_credentials("gemini")

    def test_credentials_passed_to_child_not_job_json_or_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "lecture.mp4"
            source.write_bytes(b"input")
            store = self.store
            store.save_key("gemini", "child-secret-do-not-persist")

            def command(job: dict, job_dir: Path) -> list[str]:
                expected_hash = hashlib.sha256(b"child-secret-do-not-persist").hexdigest()
                return [
                    sys.executable, "-c",
                    "import hashlib,os,pathlib,sys; "
                    f"assert hashlib.sha256(os.environ['GEMINI_API_KEY'].encode()).hexdigest()=='{expected_hash}'; "
                    "assert 'DEEPL_API_KEY' not in os.environ; "
                    "pathlib.Path(sys.argv[1]).write_bytes(b'output')",
                    str(job_dir / "final.mp4"),
                ]

            manager = LocalJobManager(
                root / "data", command_builder=command, credential_provider=store.worker_credentials
            )
            job_id = manager.register_job(source, output_directory=root / "exports")
            with patch.dict(os.environ, {"GEMINI_API_KEY": "", "DEEPL_API_KEY": "unrelated-secret"}):
                manager._run_job(manager.queue_dir / f"{job_id}.json")
            self.assertEqual(manager.get_job(job_id)["status"], "done")
            for path in manager.data_dir.rglob("*"):
                if path.is_file():
                    self.assertNotIn(b"child-secret-do-not-persist", path.read_bytes(), str(path))

    def test_saved_key_removal_before_execution_marks_job_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "input.mp4"
            source.write_bytes(b"input")
            manager = LocalJobManager(root / "data", credential_provider=self.store.worker_credentials)
            job_id = manager.register_job(source)
            with patch.dict(os.environ, {"GEMINI_API_KEY": ""}), patch("local_job_manager.subprocess.Popen") as process:
                manager._run_job(manager.queue_dir / f"{job_id}.json")
            process.assert_not_called()
            self.assertEqual(manager.get_job(job_id)["status"], "error")
            self.assertTrue((manager.jobs_dir / job_id / "input.mp4").exists())

    def test_gui_missing_key_does_not_register_job(self) -> None:
        app = Mock()
        app.credentials.worker_credentials.side_effect = CredentialError("APIキーが未設定")
        with patch("standalone_app.messagebox.showerror") as notification:
            StandaloneApp.add_job(app)
        app.manager.register_job.assert_not_called()
        app.configure_api_keys.assert_called_once()
        notification.assert_called_once()


class BundleRuntimeTests(unittest.TestCase):
    def test_windows_worker_help_writes_utf8_to_redirected_legacy_streams(self) -> None:
        output = io.BytesIO()
        errors = io.BytesIO()
        stdout = io.TextIOWrapper(output, encoding="cp1252")
        stderr = io.TextIOWrapper(errors, encoding="cp1252")
        with patch.object(sys, "platform", "win32"), patch.object(sys, "argv", ["TranslateWorker", "--help"]), \
                patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr), \
                patch.object(bundled_worker.multiprocessing, "freeze_support"), \
                patch.object(bundled_worker, "configure_runtime"):
            with self.assertRaises(SystemExit) as captured:
                bundled_worker.main()
            self.assertEqual(captured.exception.code, 0)
            stdout.flush()
            self.assertEqual(stderr.encoding, "utf-8")
        help_text = output.getvalue().decode("utf-8")
        self.assertIn("--input", help_text)
        self.assertIn("入力する講義動画", help_text)
        stdout.close()
        stderr.close()

    def test_legacy_frozen_gui_is_not_a_self_contained_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            with patch.object(sys, "frozen", True, create=True), patch.object(
                app_runtime, "resource_root", return_value=root
            ):
                self.assertFalse(app_runtime.is_bundled())
                with patch.dict(os.environ, {"TRANSLATE_WORKER_PYTHON": "/venv/python"}):
                    self.assertEqual(app_runtime.worker_command(), ["/venv/python", "-m", "lecture_poc"])
                (root / "BUNDLED").touch()
                self.assertTrue(app_runtime.is_bundled())
            with patch.object(sys, "frozen", False, create=True), patch.object(
                app_runtime, "resource_root", return_value=root
            ):
                self.assertFalse(app_runtime.is_bundled())

    def test_frozen_tts_does_not_use_system_edge_tts_or_python_module_flags(self) -> None:
        with patch.object(sys, "frozen", True, create=True), patch("shutil.which") as lookup:
            self.assertEqual(get_edge_tts_command(), [sys.executable, "--edge-tts"])
            lookup.assert_not_called()
        with patch.object(sys, "frozen", False, create=True), patch("shutil.which", return_value="/bin/edge-tts"):
            self.assertEqual(get_edge_tts_command(), ["/bin/edge-tts"])

    def test_worker_dispatches_bundled_edge_tts_with_original_cli_arguments(self) -> None:
        arguments = ["TranslateWorker", "--edge-tts", "--voice", "en-US-AriaNeural", "--help"]
        with patch.object(sys, "argv", arguments), patch.object(
            bundled_worker, "configure_runtime"
        ), patch.object(bundled_worker.multiprocessing, "freeze_support"), patch.object(
            bundled_worker.runpy, "run_module"
        ) as dispatch:
            self.assertEqual(bundled_worker.main(), 0)
            dispatch.assert_called_once_with("edge_tts", run_name="__main__")
            self.assertEqual(sys.argv, ["TranslateWorker", "--voice", "en-US-AriaNeural", "--help"])

    def test_source_command_retains_existing_python_override(self) -> None:
        with patch.object(app_runtime, "is_bundled", return_value=False), patch.dict(
            os.environ, {"TRANSLATE_WORKER_PYTHON": "/explicit/python"}
        ):
            self.assertEqual(app_runtime.worker_command(), ["/explicit/python", "-m", "lecture_poc"])

    def test_bundle_ignores_system_python_and_resolves_adjacent_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            for platform, name in (("win32", "TranslateWorker.exe"), ("darwin", "TranslateWorker")):
                worker = root / name
                worker.touch()
                with patch.object(app_runtime, "is_bundled", return_value=True), patch.object(
                    app_runtime, "executable_directory", return_value=root
                ), patch.object(sys, "platform", platform), patch.dict(
                    os.environ, {"TRANSLATE_WORKER_PYTHON": "/old/python"}
                ):
                    self.assertEqual(app_runtime.worker_command(), [str(worker)])

    def test_missing_bundle_worker_and_tools_fail_without_system_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            with patch.object(app_runtime, "is_bundled", return_value=True), patch.object(
                app_runtime, "executable_directory", return_value=Path(temporary_dir)
            ):
                with self.assertRaises(FileNotFoundError):
                    app_runtime.worker_command()
                with self.assertRaises(FileNotFoundError):
                    app_runtime.configure_runtime()

    def test_bundle_worker_env_isolated_and_contains_tool_path(self) -> None:
        with patch.object(app_runtime, "is_bundled", return_value=True), patch.object(
            app_runtime, "executable_directory", return_value=Path("/bundle")
        ), patch.dict(os.environ, {
            "PYTHONHOME": "/old", "PYTHONPATH": "/old", "TRANSLATE_PYTHON": "/old",
            "TRANSLATE_WORKER_PYTHON": "/old", "PATH": "/system",
        }):
            environment = app_runtime.worker_environment()
            for name in ("PYTHONHOME", "PYTHONPATH", "TRANSLATE_PYTHON", "TRANSLATE_WORKER_PYTHON"):
                self.assertNotIn(name, environment)
            self.assertEqual(environment["PYINSTALLER_RESET_ENVIRONMENT"], "1")
            self.assertEqual(environment["PATH"], "/bundle" + os.pathsep + "/system")
            self.assertEqual(os.environ["PYTHONHOME"], "/old")

    def test_self_test_produces_report_even_without_windowed_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            prompts = root / "lecture_poc" / "prompts"
            prompts.mkdir(parents=True)
            for name in ("engineering_terms.csv", "whisper_initial_prompt.txt", "jp_clean_ollama.txt"):
                (prompts / name).touch()
            report = root / "report.json"
            with patch.object(app_runtime, "configure_runtime"), patch.object(
                app_runtime, "is_bundled", return_value=True
            ), patch.object(app_runtime, "resource_root", return_value=root), patch.object(
                app_runtime, "worker_command", return_value=["worker"]
            ), patch.object(app_runtime, "app_version", return_value="0.1.0"), patch(
                "credential_store.secure_backend", return_value=MemoryBackend()
            ), patch.object(app_runtime.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, stdout="ffmpeg test version\n"
            )), patch.object(sys, "stdout", None):
                self.assertEqual(app_runtime.bundle_self_test("gui", report), 0)
            payload = json.loads(report.read_text())
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["version"], "0.1.0")

    def test_source_version_is_read_from_packaging_version(self) -> None:
        self.assertTrue(app_runtime.app_version())

    def test_worker_self_test_checks_bundled_tts_and_reports_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            prompts = root / "lecture_poc" / "prompts"
            prompts.mkdir(parents=True)
            for name in ("engineering_terms.csv", "whisper_initial_prompt.txt", "jp_clean_ollama.txt"):
                (prompts / name).touch()
            report = root / "report.json"
            with patch.object(app_runtime, "configure_runtime"), patch.object(
                app_runtime, "is_bundled", return_value=True
            ), patch.object(app_runtime, "resource_root", return_value=root), patch.object(
                app_runtime.importlib, "import_module"
            ), patch.object(app_runtime.subprocess, "run", side_effect=subprocess.CalledProcessError(
                1, [sys.executable, "--edge-tts", "--help"]
            )) as command:
                self.assertEqual(app_runtime.bundle_self_test("worker", report), 1)
                command.assert_called_once_with(
                    [sys.executable, "--edge-tts", "--help"],
                    capture_output=True, text=True, check=True, timeout=60,
                )
            self.assertEqual(json.loads(report.read_text())["status"], "error")


if __name__ == "__main__":
    unittest.main()
