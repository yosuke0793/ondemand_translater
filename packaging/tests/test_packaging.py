from __future__ import annotations

import ast
import io
import json
import re
import shutil
import subprocess
import sys
import unittest
from types import ModuleType
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

PACKAGING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGING))
import build
import release


class PackagingTests(unittest.TestCase):
    def test_media_check_accepts_no_drawtext_but_requires_subtitles(self):
        with patch.object(build, "run") as run:
            run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout=" ... subtitles V->V Render subtitles\n"),
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0, stdout=json.dumps({
                    "streams": [{"codec_name": "h264"}, {"codec_name": "aac"}]
                })),
            ]
            with patch.object(Path, "unlink"):
                build.verify_media_tools(Path("/bundle"), "macos-x64", Path("/smoke"), {})
            self.assertEqual(run.call_count, 3)
            run.reset_mock()
            run.side_effect = [subprocess.CompletedProcess([], 0, stdout=" ... drawtext V->V Draw text\n")]
            with self.assertRaisesRegex(RuntimeError, "subtitles"):
                build.verify_media_tools(Path("/bundle"), "macos-x64", Path("/smoke"), {})
            self.assertEqual(run.call_count, 1)

    def test_failed_command_exposes_captured_diagnostics_and_still_fails(self):
        error = subprocess.CalledProcessError(
            1, ["worker", "--help"], output="partial help", stderr="UnicodeEncodeError"
        )
        output = io.StringIO()
        with patch.object(build.subprocess, "run", side_effect=error), redirect_stderr(output):
            with self.assertRaises(subprocess.CalledProcessError) as captured:
                build.run(["worker", "--help"], capture_output=True, text=True)
        self.assertIs(captured.exception, error)
        self.assertIn("partial help", output.getvalue())
        self.assertIn("UnicodeEncodeError", output.getvalue())

    def test_version_and_tag_validation(self):
        self.assertEqual(build.version("v" + build.version()), build.version())
        with self.assertRaises(ValueError):
            build.version("v999.0.0")
        with patch.object(Path, "read_text", return_value="1.2.3-beta"):
            with self.assertRaises(ValueError):
                build.version()

    def test_source_and_spec_parse(self):
        for path in list(PACKAGING.glob("*.py")) + list(PACKAGING.glob("*.spec")):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_both_analysis_paths_resources_and_window_modes(self):
        calls = {}

        def record(kind):
            def constructor(*args, **kwargs):
                calls.setdefault(kind, []).append((args, kwargs))
                return type("FrozenNode", (), {
                    "pure": [], "scripts": [], "binaries": [], "datas": []
                })()
            return constructor

        namespace = {"SPECPATH": str(PACKAGING)}
        for name in ("Analysis", "PYZ", "EXE", "COLLECT", "BUNDLE"):
            namespace[name] = record(name)
        assets = [
            ("/installed/faster_whisper/assets/silero_encoder_v5.onnx", "faster_whisper/assets"),
            ("/installed/faster_whisper/assets/silero_decoder_v5.onnx", "faster_whisper/assets"),
        ]
        hooks = ModuleType("PyInstaller.utils.hooks")
        hooks.collect_data_files = Mock(return_value=assets)
        with patch.dict(sys.modules, {"PyInstaller.utils.hooks": hooks}):
            exec(compile((PACKAGING / "TranslateService.spec").read_text(), "spec", "exec"), namespace)
        hooks.collect_data_files.assert_called_once_with("faster_whisper")
        for asset in assets:
            self.assertIn(asset, calls["Analysis"][1][1]["datas"])
        self.assertEqual(len(calls["Analysis"]), 2)
        for _, kwargs in calls["Analysis"]:
            self.assertIn("cv2", kwargs["excludes"])
            self.assertIn("pytesseract", kwargs["excludes"])
            self.assertIn(str(PACKAGING.parent / "source" / "worker"), kwargs["pathex"])
            self.assertIn((str(PACKAGING / "VERSION"), "."), kwargs["datas"])
            self.assertIn((str(PACKAGING / "BUNDLED"), "."), kwargs["datas"])
            for source, destination in kwargs["datas"]:
                self.assertNotIn("/data/", source)
                self.assertNotIn("/.venv/", source)
                self.assertIn(destination, {".", "lecture_poc/prompts", "third-party-notices", "faster_whisper/assets"})
            self.assertTrue(any(destination == "lecture_poc/prompts"
                                for _, destination in kwargs["datas"]))
        self.assertFalse(calls["EXE"][0][1]["console"])
        self.assertTrue(calls["EXE"][1][1]["console"])
        self.assertNotIn("lecture_poc.__main__", calls["Analysis"][0][1]["hiddenimports"])
        self.assertIn("lecture_poc.__main__", calls["Analysis"][1][1]["hiddenimports"])

    def test_ffmpeg_manifest_is_complete_and_pinned(self):
        manifest = json.loads((PACKAGING / "ffmpeg.json").read_text())
        self.assertEqual(set(manifest["targets"]), set(release.TARGETS))
        self.assertNotIn("latest", manifest["base_url"])
        for entries in manifest["targets"].values():
            self.assertEqual(len(entries), 4)
            for digest in entries.values():
                self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_hash_locked_native_dependencies(self):
        for target in release.TARGETS:
            text = (PACKAGING / "locks" / f"{target}.txt").read_text()
            requirements = re.findall(r"^([a-zA-Z0-9_-]+)==([^\s]+) \\\n(.*?)(?=\n[^ \n]|\Z)",
                                      text, re.MULTILINE | re.DOTALL)
            self.assertGreater(len(requirements), 30)
            for name, _, block in requirements:
                self.assertIn("--hash=sha256:", block, name)
            self.assertIn("torch==2.2.2", text)
            self.assertIn("pyinstaller==6.16.0", text)
            self.assertIn("keyring==25.6.0", text)
        bootstrap = (PACKAGING / "locks" / "bootstrap.txt").read_text()
        for name in ("pip", "setuptools", "wheel"):
            self.assertRegex(bootstrap, rf"(?m)^{name}==[^\s]+ \\\n +--hash=sha256:")

    def test_release_rejects_tampering_missing_or_extra_assets(self):
        directory = PACKAGING / ".work" / "unit-release-assets"
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        try:
            value = build.version()
            for target in release.TARGETS:
                prefix = f"TranslateService-{value}-{target}"
                extensions = ("-setup.exe", "-portable.zip") if target == "windows-x64" else (".dmg",)
                for extension in extensions:
                    (directory / f"{prefix}{extension}").write_bytes(b"synthetic-test-artifact")
                info = {"version": value, "target": target, "commit": "test",
                        "self_tests": [{"status": "ok", "role": role, "version": value}
                                       for role in ("gui", "worker")]}
                (directory / f"{prefix}-build-info.json").write_text(json.dumps(info))
                paths = sorted(directory.glob(f"{prefix}*"))
                (directory / f"{prefix}-SHA256SUMS.txt").write_text(
                    "".join(f"{build.sha256(path)}  {path.name}\n" for path in paths)
                )
            with patch.object(release, "ARTIFACTS", directory):
                self.assertEqual(len(release.validate_assets("v" + value)), 10)
                extra = directory / "unapproved.txt"
                extra.write_text("not a distribution asset")
                with self.assertRaises(ValueError):
                    release.validate_assets("v" + value)
                extra.unlink()
                artifact = next(directory.glob("*.dmg"))
                artifact.write_bytes(b"tampered")
                with self.assertRaises(ValueError):
                    release.validate_assets("v" + value)
        finally:
            shutil.rmtree(directory)


if __name__ == "__main__":
    unittest.main()
