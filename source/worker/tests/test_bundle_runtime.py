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
from types import SimpleNamespace
from unittest.mock import Mock, patch
from contextlib import redirect_stderr

import app_runtime
import bundled_worker
from media_license import validate_ffmpeg_license
from credential_store import CredentialError, CredentialStore, SERVICE_NAME
from lecture_poc.security import redact_secrets
from local_job_manager import LocalJobManager
from standalone_app import StandaloneApp, TTS_BACKENDS, PIPER_VOICE_OPTIONS, VOICE_OPTIONS, load_preferences


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
    def test_azure_key_and_subscription_headers_are_redacted(self) -> None:
        for value in (
            "AZURE_SPEECH_KEY=sentinel-secret",
            "Ocp-Apim-Subscription-Key: sentinel-secret",
            '{"Ocp-Apim-Subscription-Key": "sentinel-secret"}',
            '{"AZURE_SPEECH_KEY": "sentinel-secret"}',
        ):
            with self.subTest(value=value):
                self.assertNotIn("sentinel-secret", redact_secrets(value))

    def test_gui_cleanup_confirmation_and_errors(self) -> None:
        app = Mock()
        app.manager.delete_job_intermediates.return_value = 3
        with patch("standalone_app.messagebox.askyesno", return_value=False):
            StandaloneApp.delete_job_intermediates(app)
        app.manager.delete_job_intermediates.assert_not_called()
        with patch("standalone_app.messagebox.askyesno", return_value=True), patch(
            "standalone_app.messagebox.showinfo"
        ) as success:
            StandaloneApp.delete_job_intermediates(app)
            success.assert_called_once()
        app.manager.delete_job_intermediates.side_effect = RuntimeError("処理中")
        with patch("standalone_app.messagebox.askyesno", return_value=True), patch(
            "standalone_app.messagebox.showerror"
        ) as error, patch("standalone_app.messagebox.showinfo") as success:
            StandaloneApp.delete_job_intermediates(app)
            error.assert_called_once()
            success.assert_not_called()

    def test_startup_api_dialog_defaults_to_shown_and_can_be_disabled(self) -> None:
        app = Mock()
        app.hide_api_keys_on_startup.get.return_value = False
        StandaloneApp._show_startup_api_keys(app)
        app.configure_api_keys.assert_called_once_with()
        app.configure_api_keys.reset_mock()
        app.hide_api_keys_on_startup.get.return_value = True
        StandaloneApp._show_startup_api_keys(app)
        app.configure_api_keys.assert_not_called()

    def test_startup_preference_survives_other_preference_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            app = Mock()
            app.preferences_path = Path(temporary_dir) / "settings.json"
            app.output_dir.get.return_value = "/exports"
            app.voice_choice.get.return_value = ""
            app.selected_tts_backend.return_value = "piper"
            app.selected_voice.return_value = "en_US-ljspeech-medium"
            app.azure_speech_region.get.return_value = "japaneast"
            app.hide_api_keys_on_startup.get.return_value = True
            StandaloneApp._save_preferences(app)
            self.assertEqual(load_preferences(app.preferences_path)["hide_api_keys_on_startup"], "true")
            app.output_dir.get.return_value = "/new-exports"
            StandaloneApp._save_preferences(app)
            self.assertEqual(load_preferences(app.preferences_path)["hide_api_keys_on_startup"], "true")
            app.hide_api_keys_on_startup.get.return_value = False
            StandaloneApp._save_preferences(app)
            self.assertEqual(load_preferences(app.preferences_path)["hide_api_keys_on_startup"], "false")

    def test_startup_preference_failure_notifies_and_restores_checkbox(self) -> None:
        app = Mock()
        app.hide_api_keys_on_startup.get.return_value = True
        app._save_preferences.side_effect = OSError("read-only settings")
        with patch("standalone_app.messagebox.showerror") as notification:
            StandaloneApp._save_api_startup_preference(app)
        app.hide_api_keys_on_startup.set.assert_called_once_with(False)
        notification.assert_called_once()

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

    def test_piper_never_reads_speech_key_and_azure_requires_key_and_region(self) -> None:
        with patch.object(self.store, "effective_key", side_effect=AssertionError("no key needed")):
            self.assertEqual(self.store.speech_credentials("piper"), {})
        with patch.dict(os.environ, {"AZURE_SPEECH_KEY": "", "AZURE_SPEECH_REGION": ""}):
            with self.assertRaises(CredentialError):
                self.store.speech_credentials("azure", "japaneast")
            self.store.save_key("azure", "speech-secret")
            for region in ("", "https://example.org", "japaneast/other", "../invalid"):
                with self.subTest(region=region), self.assertRaises(CredentialError):
                    self.store.speech_credentials("azure", region)
            self.assertEqual(self.store.speech_credentials("azure", "JapanEast"), {
                "AZURE_SPEECH_KEY": "speech-secret", "AZURE_SPEECH_REGION": "japaneast",
            })
        with patch.dict(os.environ, {"AZURE_SPEECH_KEY": "env-speech", "AZURE_SPEECH_REGION": "westus"}):
            self.assertEqual(self.store.speech_credentials("azure", "japaneast"), {
                "AZURE_SPEECH_KEY": "env-speech", "AZURE_SPEECH_REGION": "westus",
            })
        for backend in ("edge-tts", "xtts-v2"):
            with self.assertRaises(ValueError):
                self.store.speech_credentials(backend)

    def test_backend_switch_updates_voice_choices_and_preferences(self) -> None:
        app = Mock()
        app.selected_tts_backend.return_value = "piper"
        StandaloneApp._change_tts_backend(app)
        app.voice_selector.configure.assert_called_with(values=list(PIPER_VOICE_OPTIONS))
        app.voice_choice.set.assert_called_with(next(iter(PIPER_VOICE_OPTIONS)))
        app.selected_tts_backend.return_value = "azure"
        StandaloneApp._change_tts_backend(app)
        app.voice_selector.configure.assert_called_with(values=list(VOICE_OPTIONS))
        app.tts_choice.get.return_value = next(iter(TTS_BACKENDS))
        self.assertEqual(StandaloneApp.selected_tts_backend(app), "piper")
        app.tts_choice.get.return_value = "invalid"
        with self.assertRaises(ValueError):
            StandaloneApp.selected_tts_backend(app)

    def test_male_and_female_gui_choices_are_resolved_and_persisted(self) -> None:
        for backend, options, voices in (
            ("piper", PIPER_VOICE_OPTIONS, {"en_US-ljspeech-medium", "en_US-john-medium"}),
            ("azure", VOICE_OPTIONS, {"en-US-AriaNeural", "en-US-AndrewNeural"}),
        ):
            self.assertTrue(voices <= set(options.values()))
            for voice in voices:
                label = next(label for label, name in options.items() if name == voice)
                app = Mock()
                app.selected_tts_backend.return_value = backend
                app.voice_choice.get.return_value = label
                app.selected_voice.side_effect = lambda: StandaloneApp.selected_voice(app)
                app.output_dir.get.return_value = "/exports"
                app.azure_speech_region.get.return_value = "japaneast"
                app.hide_api_keys_on_startup.get.return_value = True
                with self.subTest(backend=backend, voice=voice), tempfile.TemporaryDirectory() as directory:
                    app.preferences_path = Path(directory) / "settings.json"
                    self.assertEqual(StandaloneApp.selected_voice(app), voice)
                    StandaloneApp._save_preferences(app)
                    saved = load_preferences(app.preferences_path)
                    self.assertEqual(saved["tts_backend"], backend)
                    self.assertEqual(saved["voice"], voice)
                    self.assertIn("女性" if voice in ("en_US-ljspeech-medium", "en-US-AriaNeural") else "男性", label)

    def test_speech_region_is_nonsecret_setting_and_save_failure_restores_value(self) -> None:
        app = Mock()
        app.azure_speech_region.get.return_value = "westus"
        StandaloneApp.save_azure_speech_region(app, " JapanEast ")
        app.azure_speech_region.set.assert_called_with("japaneast")
        app._save_preferences.side_effect = OSError("read only")
        with self.assertRaises(OSError):
            StandaloneApp.save_azure_speech_region(app, "eastus")
        app.azure_speech_region.set.assert_called_with("westus")

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

    def test_speech_credentials_are_isolated_per_job_and_never_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "lecture.mp4"
            source.write_bytes(b"input")
            self.store.save_key("azure", "speech-secret-do-not-persist")
            digest = hashlib.sha256(b"speech-secret-do-not-persist").hexdigest()

            def command(job: dict, job_dir: Path) -> list[str]:
                assertion = (
                    f"assert hashlib.sha256(os.environ['AZURE_SPEECH_KEY'].encode()).hexdigest() == '{digest}'; "
                    "assert os.environ['AZURE_SPEECH_REGION']=='japaneast'; "
                    if job["options"]["tts_backend"] == "azure" else
                    "assert 'AZURE_SPEECH_KEY' not in os.environ; assert 'AZURE_SPEECH_REGION' not in os.environ; "
                )
                return [sys.executable, "-c",
                        "import os,hashlib,pathlib,sys; " + assertion +
                        "pathlib.Path(sys.argv[1]).write_bytes(b'output')",
                        str(job_dir / "final.mp4")]

            manager = LocalJobManager(
                root / "data", command_builder=command,
                credential_provider=self.store.worker_credentials,
                speech_credential_provider=self.store.speech_credentials,
            )
            with patch.dict(os.environ, {"AZURE_SPEECH_KEY": "", "AZURE_SPEECH_REGION": ""}):
                for backend in ("azure", "piper"):
                    job_id = manager.register_job(source, options={
                        "translation_backend": "argos", "tts_backend": backend,
                        "azure_speech_region": "japaneast",
                    })
                    manager._run_job(manager.queue_dir / f"{job_id}.json")
                    self.assertEqual(manager.get_job(job_id)["status"], "done")
            for path in manager.data_dir.rglob("*"):
                if path.is_file():
                    self.assertNotIn(b"speech-secret-do-not-persist", path.read_bytes())

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

    def test_gui_azure_confirmation_controls_job_registration(self) -> None:
        app = Mock()
        app.translation_backend.get.return_value = "gemini"
        app.english_srt_path.get.return_value = ""
        app.japanese_srt_path.get.return_value = ""
        app.selected_tts_backend.return_value = "azure"
        app.selected_voice.return_value = "en-US-AriaNeural"
        app.azure_speech_region.get.return_value = "japaneast"
        app.video_path.get.return_value = "/input.mp4"
        app.output_dir.get.return_value = "/exports"
        app.transcription_preset.get.return_value = "balanced"
        app.glossary_terms.return_value = []
        with patch("standalone_app.messagebox.askyesno", return_value=False):
            StandaloneApp.add_job(app)
        app.manager.register_job.assert_not_called()
        with patch("standalone_app.messagebox.askyesno", return_value=True):
            StandaloneApp.add_job(app)
        app.credentials.speech_credentials.assert_called_with("azure", "japaneast")
        options = app.manager.register_job.call_args.args[2]
        self.assertEqual(options["tts_backend"], "azure")
        self.assertEqual(options["azure_speech_region"], "japaneast")
        self.assertNotIn("AZURE_SPEECH_KEY", options)

    def test_gui_piper_does_not_show_speech_charge_confirmation(self) -> None:
        app = Mock()
        app.translation_backend.get.return_value = "argos"
        app.english_srt_path.get.return_value = ""
        app.japanese_srt_path.get.return_value = ""
        app.selected_tts_backend.return_value = "piper"
        app.selected_voice.return_value = "en_US-ljspeech-medium"
        app.azure_speech_region.get.return_value = ""
        app.video_path.get.return_value = "/input.mp4"
        app.output_dir.get.return_value = "/exports"
        app.transcription_preset.get.return_value = "balanced"
        app.glossary_terms.return_value = []
        with patch("standalone_app.messagebox.askyesno") as confirmation:
            StandaloneApp.add_job(app)
        confirmation.assert_not_called()
        self.assertEqual(app.manager.register_job.call_args.args[2]["tts_backend"], "piper")


class BundleRuntimeTests(unittest.TestCase):
    def test_runtime_forwards_selected_piper_voice_to_model_resolver(self) -> None:
        for voice in ("en_US-ljspeech-medium", "en_US-john-medium"):
            model_paths = (Path(f"{voice}.onnx"), Path(f"{voice}.onnx.json"))
            resolver = Mock(return_value=model_paths)
            with self.subTest(voice=voice), patch.dict(
                sys.modules, {"piper_model": SimpleNamespace(resolve_model_paths=resolver)},
            ), patch.object(app_runtime, "is_bundled", return_value=True), patch.object(
                app_runtime, "resource_root", return_value=Path("/bundled"),
            ):
                self.assertEqual(app_runtime.piper_model_paths(voice), model_paths)
                resolver.assert_called_once_with(Path("/bundled"), voice=voice)

    def test_distribution_license_gate_rejects_nonfree_and_unverified_binaries(self) -> None:
        version = "ffmpeg version test\nconfiguration: --enable-gpl --enable-version3\n"
        license_text = "GNU General Public License; either version 3 of the License, or any later version."
        self.assertEqual(validate_ffmpeg_license(version, license_text), "GPL-3.0-or-later")
        for config, text in (
            (version + "--enable-nonfree", license_text),
            (version, "This version has nonfree parts. Therefore it is not legally redistributable."),
            ("ffmpeg version test", license_text),
            (version, "GNU Lesser General Public License"),
        ):
            with self.assertRaises(RuntimeError):
                validate_ffmpeg_license(config, text)

    def test_bundle_explicitly_rejects_ocr_options(self) -> None:
        for option in ("--video-text-overlay", "--video-text-overlay-fps=2"):
            output = io.StringIO()
            with patch.object(sys, "argv", ["TranslateWorker", option]), \
                    patch.object(bundled_worker, "is_bundled", return_value=True), \
                    patch.object(bundled_worker, "configure_runtime"), \
                    patch.object(bundled_worker.multiprocessing, "freeze_support"), \
                    redirect_stderr(output):
                self.assertEqual(bundled_worker.main(), 1)
            self.assertIn("OCR", output.getvalue())
            self.assertIn("対応していません", output.getvalue())

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
                [], 0, stdout="ffmpeg test version\nconfiguration: --enable-gpl --enable-version3\n"
                "GNU General Public License; either version 3 of the License.\n", stderr=""
            )), patch.object(sys, "stdout", None):
                self.assertEqual(app_runtime.bundle_self_test("gui", report), 0)
            payload = json.loads(report.read_text())
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["version"], "0.1.0")

    def test_bundle_self_test_reports_nonfree_tool_as_error(self) -> None:
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
            ), patch("credential_store.secure_backend", return_value=MemoryBackend()), patch.object(
                app_runtime.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, stdout="ffmpeg version test\nconfiguration: --enable-nonfree\n", stderr=""
                )
            ):
                self.assertEqual(app_runtime.bundle_self_test("gui", report), 1)
            self.assertIn("nonfree", json.loads(report.read_text())["error"])

    def test_source_version_is_read_from_packaging_version(self) -> None:
        self.assertTrue(app_runtime.app_version())

    def test_worker_self_test_checks_bundled_tts_and_reports_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            prompts = root / "lecture_poc" / "prompts"
            prompts.mkdir(parents=True)
            for name in ("engineering_terms.csv", "whisper_initial_prompt.txt", "jp_clean_ollama.txt"):
                (prompts / name).touch()
            for name in ("silero_encoder_v5.onnx", "silero_decoder_v5.onnx"):
                (root / name).touch()
            vad = Mock()
            vad.get_assets_path.return_value = str(root)
            report = root / "report.json"
            with patch.object(app_runtime, "configure_runtime"), patch.object(
                app_runtime, "is_bundled", return_value=True
            ), patch.object(app_runtime, "resource_root", return_value=root), patch.object(
                app_runtime.importlib, "import_module", return_value=vad
            ), patch.object(app_runtime, "_piper_smoke_test", side_effect=RuntimeError(
                "Piper offline synthesis failed"
            )) as synthesis, patch.object(app_runtime.subprocess, "run") as command:
                self.assertEqual(app_runtime.bundle_self_test("worker", report), 1)
                synthesis.assert_called_once_with()
                command.assert_not_called()
                vad.get_vad_model.assert_called_once_with()
            self.assertEqual(json.loads(report.read_text())["status"], "error")
            self.assertIn("Piper", json.loads(report.read_text())["error"])

    def test_worker_self_test_rejects_missing_vad_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            prompts = root / "lecture_poc" / "prompts"
            prompts.mkdir(parents=True)
            for name in ("engineering_terms.csv", "whisper_initial_prompt.txt", "jp_clean_ollama.txt"):
                (prompts / name).touch()
            vad = Mock()
            vad.get_assets_path.return_value = str(root)
            report = root / "report.json"
            with patch.object(app_runtime, "configure_runtime"), patch.object(
                app_runtime, "is_bundled", return_value=True
            ), patch.object(app_runtime, "resource_root", return_value=root), patch.object(
                app_runtime.importlib, "import_module", return_value=vad
            ):
                self.assertEqual(app_runtime.bundle_self_test("worker", report), 1)
            payload = json.loads(report.read_text())
            self.assertIn("silero_encoder_v5.onnx", payload["error"])
            (root / "silero_encoder_v5.onnx").touch()
            with patch.object(app_runtime, "configure_runtime"), patch.object(
                app_runtime, "is_bundled", return_value=True
            ), patch.object(app_runtime, "resource_root", return_value=root), patch.object(
                app_runtime.importlib, "import_module", return_value=vad
            ):
                self.assertEqual(app_runtime.bundle_self_test("worker", report), 1)
            self.assertIn("silero_decoder_v5.onnx", json.loads(report.read_text())["error"])
            vad.get_vad_model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
