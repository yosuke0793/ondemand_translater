from __future__ import annotations

import ast
import json
import re
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGING))
import build
import release


class PackagingTests(unittest.TestCase):
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
        exec(compile((PACKAGING / "TranslateService.spec").read_text(), "spec", "exec"), namespace)
        self.assertEqual(len(calls["Analysis"]), 2)
        for _, kwargs in calls["Analysis"]:
            self.assertIn(str(PACKAGING.parent / "source" / "worker"), kwargs["pathex"])
            self.assertIn((str(PACKAGING / "VERSION"), "."), kwargs["datas"])
            self.assertIn((str(PACKAGING / "BUNDLED"), "."), kwargs["datas"])
            for source, destination in kwargs["datas"]:
                self.assertNotIn("/data/", source)
                self.assertNotIn("/.venv/", source)
                self.assertIn(destination, {".", "lecture_poc/prompts", "third-party-notices"})
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
