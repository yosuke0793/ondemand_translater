from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

PACKAGING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGING))
import build
import piper_model
import piper_source
import pyav_source
import release
import app_runtime


class PackagingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("bash"), "Bash required")
    def test_managed_ffmpeg_allows_mingw_import_libraries_but_not_static_archives(self):
        directory = PACKAGING / ".work" / "unit-pyav-import-libraries"
        shutil.rmtree(directory, ignore_errors=True)
        library_dir = directory / "lib"
        library_dir.mkdir(parents=True)
        recipe = (PACKAGING / "build-pyav-ffmpeg.sh").read_text()
        gate = recipe[recipe.index('if find "$PREFIX/lib"'):]
        try:
            for name in ("libavcodec.dll.a", "libswscale.dll.a"):
                (library_dir / name).touch()
            env = dict(os.environ, PREFIX=str(directory))
            subprocess.run(["bash", "-c", gate], env=env, check=True, capture_output=True)
            for name in ("libavcodec.a", "libswscale.a"):
                with self.subTest(name=name):
                    static = library_dir / name
                    static.touch()
                    result = subprocess.run(["bash", "-c", gate], env=env, capture_output=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(b"Static FFmpeg", result.stderr)
                    static.unlink()
        finally:
            shutil.rmtree(directory)

    def test_pyav_compiler_options_precede_wheel_build(self):
        from setuptools import Distribution

        command = pyav_source._wheel_command("windows-x64", Path("prefix"), Path("wheels"))
        self.assertLess(command.index("build_ext"), command.index("bdist_wheel"))
        self.assertLess(command.index("--compiler=mingw32"), command.index("bdist_wheel"))
        source_arguments = command[2:]
        ffmpeg_argument = next(arg for arg in source_arguments if arg.startswith("--ffmpeg-dir="))
        source_arguments.remove(ffmpeg_argument)  # Verified PyAV 15.1.0 setup.py behavior.
        distribution = Distribution({"script_args": source_arguments})
        self.assertTrue(distribution.parse_command_line())
        self.assertEqual(distribution.commands, ["build_ext", "bdist_wheel"])
        self.assertEqual(distribution.get_command_obj("build_ext").compiler, "mingw32")

    def test_windows_pyav_compiler_probe_fails_on_extension_load_failure(self):
        directory = PACKAGING / ".work" / "unit-pyav-compiler-probe"
        shutil.rmtree(directory, ignore_errors=True)
        results = [
            subprocess.CompletedProcess([], 0, stdout="x86_64-w64-mingw32\n"),
            subprocess.CompletedProcess([], 0),
            subprocess.CalledProcessError(1, ["python", "-c"], stderr="DLL load failed"),
        ]
        try:
            with patch.object(pyav_source, "_checked", side_effect=results):
                with self.assertRaisesRegex(RuntimeError, "cannot compile/link/load"):
                    pyav_source._verify_windows_compiler(directory, {})
        finally:
            shutil.rmtree(directory)

    def test_frozen_pyav_gate_hides_source_libraries_and_restores_after_failure(self):
        directory = PACKAGING / ".work" / "unit-pyav-frozen-gate"
        shutil.rmtree(directory, ignore_errors=True)
        bundle = directory / "bundle"
        (bundle / "av").mkdir(parents=True)
        (bundle / "av" / "_core.so").touch()
        for library in pyav_source.FFMPEG_LIBRARIES:
            (bundle / f"lib{library}.dylib").touch()
        notices = directory / "notices" / "pyav"
        notices.mkdir(parents=True)
        for name in ("PyAV-LICENSE.txt", "FFmpeg-COPYING.LGPLv2.1.txt", "managed-source-manifest.json"):
            (notices / name).touch()
        source_libdir = directory / "pyav" / "macos-arm64" / "prefix" / "lib"
        source_libdir.mkdir(parents=True)
        test_dir = directory / "smoke"
        test_dir.mkdir()
        decoded = {"pyav_version": "15.1.0",
                   "decoded": {"codec": "h264", "width": 160, "height": 120}}

        def run(command, **kwargs):
            if "--pyav-decode-self-test" in command:
                self.assertFalse(source_libdir.exists())
                self.assertNotIn("DYLD_LIBRARY_PATH", kwargs["env"])
                raise subprocess.CalledProcessError(1, command)
            Path(command[-1]).touch()

        try:
            with patch.object(build, "WORK", directory), \
                 patch.object(build, "run", side_effect=run), \
                 patch.object(build, "executable_dir", return_value=bundle), \
                 patch.object(pyav_source, "verify_installed", return_value={"pyav_version": "15.1.0"}), \
                 patch.object(pyav_source, "decode_smoke", return_value=decoded):
                with self.assertRaises(subprocess.CalledProcessError):
                    build.verify_managed_pyav(
                        bundle, "macos-arm64", test_dir, {"DYLD_LIBRARY_PATH": str(source_libdir)},
                    )
            self.assertTrue(source_libdir.is_dir())
            self.assertFalse((test_dir / "pyav-managed-decode-smoke.mp4").exists())
        finally:
            shutil.rmtree(directory)

    @unittest.skipUnless(shutil.which("bash") and shutil.which("make"), "Bash and make required")
    def test_native_program_targets_use_windows_executable_suffix_only_on_windows(self):
        recipe = (PACKAGING / "build-ffmpeg.sh").read_text()
        build_programs = recipe[recipe.index("PROGRAM_SUFFIX=\n"):]
        for target in ("windows-x64", "macos-arm64", "macos-x64"):
            suffix = ".exe" if target == "windows-x64" else ""
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                source = directory / "ffmpeg"
                source.mkdir()
                (source / "Makefile").write_text(
                    ".PHONY: install\n"
                    f"ffmpeg{suffix} ffprobe{suffix}:\n"
                    "\t@touch $@\n"
                    f"install: ffmpeg{suffix} ffprobe{suffix}\n"
                    "\t@touch installed\n"
                )
                environment = dict(os.environ, BUILD_DIR=str(directory), TARGET=target, BUILD_JOBS="2")
                subprocess.run(
                    [shutil.which("bash"), "-c", "set -euo pipefail\n" + build_programs],
                    env=environment, capture_output=True, text=True, check=True,
                )
                self.assertTrue((source / f"ffmpeg{suffix}").is_file())
                self.assertTrue((source / f"ffprobe{suffix}").is_file())
                self.assertTrue((source / "installed").is_file())

    @unittest.skipUnless(shutil.which("bash"), "Bash is required to exercise the native recipe")
    def test_windows_recipe_uses_available_native_mingw_tools(self):
        recipe = (PACKAGING / "build-ffmpeg.sh").read_text()
        windows = recipe.split('elif [[ "$TARGET" == windows-x64 ]]; then\n', 1)[1]
        setup = windows.split('    export CFLAGS=', 1)[0]
        shell = """
set -euo pipefail
command() {
    if [[ "$1" != -v ]]; then
        builtin command "$@"
        return
    fi
    case "$2" in
        gcc|g++|ar|ranlib|cmake|pkg-config|make|nasm|cygpath) return 0 ;;
        *) return 1 ;;
    esac
}
""" + setup + '\nprintf "%s\\n" "$CC" "$CXX" "$AR" "$RANLIB"\n'
        environment = dict(os.environ)
        environment.pop("CROSS_COMPILE", None)
        result = subprocess.run(
            [shutil.which("bash"), "-c", shell], env=environment,
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(result.stdout.splitlines(), ["gcc", "g++", "ar", "ranlib"])
        self.assertIn('-G "Unix Makefiles"', windows)
        self.assertIn('--cross-prefix="$CROSS"', windows)
        wrapper = (PACKAGING / "build-windows.ps1").read_text()
        self.assertIn("mingw-w64-x86_64-binutils", wrapper)
        self.assertIn('$env:PATH = "C:\\msys64\\mingw64\\bin;C:\\msys64\\usr\\bin;$env:PATH"', wrapper)

    def test_actions_prepares_nasm_before_macos_build(self):
        workflow = (PACKAGING.parent / ".github" / "workflows" / "package-release.yml").read_text()
        prepare = workflow.index("- name: Prepare macOS native build tools")
        build_step = workflow.index("- name: Build and mount-test macOS disk image")
        self.assertLess(prepare, build_step)
        self.assertIn("if: runner.os == 'macOS'", workflow[prepare:build_step])
        self.assertIn("brew install cmake pkg-config nasm", workflow[prepare:build_step])

    @unittest.skipUnless(shutil.which("bash"), "Bash is required to exercise CMake configuration")
    def test_windows_cmake_receives_absolute_tools_and_native_flag_paths(self):
        recipe = (PACKAGING / "build-ffmpeg.sh").read_text()
        windows = recipe.split('elif [[ "$TARGET" == windows-x64 ]]; then\n', 1)[1]
        configure = windows[windows.index('    NATIVE_PREFIX='):windows.index('    cmake --build')]
        shell = r'''
set -euo pipefail
PREFIX="/d/build with spaces/prefix"
BUILD_DIR="/d/build with spaces/build"
CC=gcc CXX=g++ AR=ar RANLIB=ranlib
CFLAGS="-O2 -I$PREFIX/include"
CXXFLAGS="-I$PREFIX/include"
LDFLAGS="-L$PREFIX/lib -static"
command() { printf '/mingw64/bin/%s.exe\n' "$2"; }
cygpath() {
    case "$2" in
        /mingw64/*) printf 'C:/msys64%s\n' "$2" ;;
        /usr/*) printf 'C:/msys64%s\n' "$2" ;;
        /d/*) printf 'D:/%s\n' "${2#/d/}" ;;
        *) return 1 ;;
    esac
}
cmake() {
    printf '%s\n' "$CFLAGS" "$CXXFLAGS" "$LDFLAGS"
    printf '%s\n' "$@"
}
''' + configure
        result = subprocess.run(
            [shutil.which("bash"), "-c", shell], capture_output=True, text=True, check=True,
        )
        lines = result.stdout.splitlines()
        self.assertEqual(lines[:3], [
            "-O2 -ID:/build with spaces/prefix/include",
            "-ID:/build with spaces/prefix/include",
            "-LD:/build with spaces/prefix/lib -static",
        ])
        for key, value in (
            ("CMAKE_C_COMPILER", "C:/msys64/mingw64/bin/gcc.exe"),
            ("CMAKE_CXX_COMPILER", "C:/msys64/mingw64/bin/g++.exe"),
            ("CMAKE_AR", "C:/msys64/mingw64/bin/ar.exe"),
            ("CMAKE_RANLIB", "C:/msys64/mingw64/bin/ranlib.exe"),
            ("CMAKE_MAKE_PROGRAM", "C:/msys64/usr/bin/make.exe"),
            ("CMAKE_INSTALL_PREFIX", "D:/build with spaces/prefix"),
            ("CMAKE_PREFIX_PATH", "D:/build with spaces/prefix"),
        ):
            self.assertIn(f"-D{key}={value}", lines)

    def make_source_archive(self, directory, inputs, target, value, changes=None):
        manifest_bytes = (inputs / "ffmpeg.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        config = "--enable-gpl --enable-version3 --enable-libx264\n"
        media = {
            "license": "GPL-3.0-or-later", "nonfree": False,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "sources": manifest["sources"], "binaries": {"ffmpeg": "a" * 64, "ffprobe": "b" * 64},
        }
        info = {
            **media, "target": target, "app_version": value,
            "ffmpeg_version": "configuration: " + config,
            "ffmpeg_license": "GNU General Public License version 3",
            "configure_args": config,
        }
        contents = {name: (inputs / name).read_bytes()
                    for name in ("ffmpeg.json", "build-ffmpeg.sh", "ffmpeg_source.py")}
        contents.update({
            "build-info.json": json.dumps(info).encode(),
            "build/ffmpeg-configure-args.txt": config.encode(),
            "build/build.log": b"synthetic build log",
            "build/ffmpeg-configure.log": b"synthetic configure log",
            "build/ffmpeg-build.log": b"synthetic make log",
        })
        contents.update({f"sources/{source['name']}": b"synthetic-source"
                         for source in manifest["sources"]})
        if changes:
            contents.update(changes)
        path = directory / f"TranslateService-{value}-{target}-ffmpeg-source.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, data in contents.items():
                if data is None:
                    continue
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        return path, {**media, "source_archive": path.name, "source_sha256": build.sha256(path)}

    def make_source_inputs(self, directory):
        inputs = directory / "inputs"
        inputs.mkdir()
        source = {"name": "ffmpeg-test.tar.xz", "url": "https://example.invalid/source",
                  "sha256": hashlib.sha256(b"synthetic-source").hexdigest()}
        (inputs / "ffmpeg.json").write_text(json.dumps({
            "schema_version": 1, "license": "GPL-3.0-or-later", "nonfree": False,
            "sources": [source],
        }))
        for name in ("build-ffmpeg.sh", "ffmpeg_source.py"):
            shutil.copy2(PACKAGING / name, inputs / name)
        piper = json.loads((PACKAGING / "piper_model.json").read_text())
        synthetic_hash = hashlib.sha256(b"synthetic-piper-source").hexdigest()
        piper["runtime"]["source_archive_sha256"] = synthetic_hash
        piper["runtime"]["embedded_espeak_ng"]["source_archive_sha256"] = synthetic_hash
        (inputs / "piper_model.json").write_text(json.dumps(piper))
        pyav = json.loads((PACKAGING / "pyav.json").read_text())
        pyav_license = b"synthetic PyAV BSD license"
        ffmpeg_license = b"synthetic FFmpeg LGPL license"
        pyav_archive = io.BytesIO()
        with tarfile.open(fileobj=pyav_archive, mode="w:gz") as archive:
            for name, content in (("av-15.1.0/LICENSE.txt", pyav_license),):
                member = tarfile.TarInfo(name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
        ffmpeg_archive = io.BytesIO()
        with tarfile.open(fileobj=ffmpeg_archive, mode="w:xz") as archive:
            member = tarfile.TarInfo("ffmpeg-7.1.1/COPYING.LGPLv2.1")
            member.size = len(ffmpeg_license)
            archive.addfile(member, io.BytesIO(ffmpeg_license))
        pyav["pyav"]["sha256"] = hashlib.sha256(pyav_archive.getvalue()).hexdigest()
        pyav["ffmpeg"]["sha256"] = hashlib.sha256(ffmpeg_archive.getvalue()).hexdigest()
        (inputs / "pyav.json").write_text(json.dumps(pyav))
        for name in ("pyav_source.py", "build-pyav-ffmpeg.sh"):
            shutil.copy2(PACKAGING / name, inputs / name)
        (inputs / ".pyav_sources.json").write_text(json.dumps({
            "pyav": pyav_archive.getvalue().hex(),
            "ffmpeg": ffmpeg_archive.getvalue().hex(),
            "pyav_license": pyav_license.decode(),
            "ffmpeg_license": ffmpeg_license.decode(),
        }))
        return inputs

    def make_pyav_source_archive(self, directory, inputs, target, value):
        manifest_bytes = (inputs / "pyav.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        source_data = json.loads((inputs / ".pyav_sources.json").read_text())
        sources = [
            {key: manifest[kind][key] for key in ("name", "url", "sha256", "license")}
            for kind in ("pyav", "ffmpeg")
        ]
        configure = " ".join(manifest["recipe"]["configure_args"] + ["--arch=x86_64"])
        library_hashes = {
            f"lib{library}.dylib": hashlib.sha256(library.encode()).hexdigest()
            for library in manifest["recipe"]["required_libraries"]
        }
        source_name = f"TranslateService-{value}-{target}-pyav-source.tar.gz"
        record = {
            "schema_version": 1, "target": target, "app_version": value,
            "pyav_version": "15.1.0", "pyav_license": "BSD-3-Clause",
            "ffmpeg_version": "7.1.1", "ffmpeg_license": "LGPL-2.1-or-later",
            "nonfree": False, "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "sources": sources, "configure_args": configure,
            "shared_library_sha256": library_hashes,
            "pyav_wheel_sha256": "a" * 64, "wheel": "av-15.1.0-cp312-test.whl",
            "source_archive": source_name,
        }
        contents = {name: (inputs / name).read_bytes()
                    for name in ("pyav.json", "pyav_source.py", "build-pyav-ffmpeg.sh")}
        contents.update({
            "build-info.json": json.dumps(record).encode(),
            "build/ffmpeg-configure-args.txt": configure.encode(),
            "build/build.log": b"synthetic managed FFmpeg build log",
            "build/ffmpeg-configure.log": b"synthetic managed FFmpeg configure log",
            "build/ffmpeg-build.log": b"synthetic managed FFmpeg make log",
            "build/pyav-build.log": b"synthetic PyAV wheel compile log",
            "sources/av-15.1.0.tar.gz": bytes.fromhex(source_data["pyav"]),
            "sources/ffmpeg-7.1.1.tar.xz": bytes.fromhex(source_data["ffmpeg"]),
            "notices/PyAV-LICENSE.txt": source_data["pyav_license"].encode(),
            "notices/FFmpeg-COPYING.LGPLv2.1.txt": source_data["ffmpeg_license"].encode(),
            "notices/managed-source-manifest.json": manifest_bytes,
        })
        path = directory / source_name
        with tarfile.open(path, "w:gz") as archive:
            for name, data in contents.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        info = {
            **record, "source_archive_sha256": build.sha256(path),
            "decode_smoke": {
                "pyav_version": "15.1.0",
                "ffmpeg_library_versions": {
                    f"lib{name}": [1, 0, 0] for name in manifest["recipe"]["required_libraries"]
                },
                "decoded": {"codec": "h264", "width": 160, "height": 120},
                "frozen_decode": {
                    "pyav_version": "15.1.0",
                    "decoded": {"codec": "h264", "width": 160, "height": 120},
                },
            },
        }
        return path, info

    def make_piper_archive(self, directory, inputs, target, value, changes=None):
        manifest_bytes = (inputs / "piper_model.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        runtime, model = manifest["runtime"], manifest["voice"]
        espeak = runtime["embedded_espeak_ng"]
        sources = [
            {"name": f'piper1-gpl-{runtime["source_revision"]}.tar.gz',
             "url": runtime["source_archive_url"], "sha256": runtime["source_archive_sha256"],
             "license": runtime["license"]},
            {"name": f'espeak-ng-{espeak["source_revision"]}.tar.gz',
             "url": espeak["source_archive_url"], "sha256": espeak["source_archive_sha256"],
             "license": "GPL-3.0-or-later"},
        ]
        application_files = piper_source.application_source_files()
        application_inventory = {
            relative: build.sha256(file) for relative, file in application_files.items()
        }
        record = json.dumps({
            "schema_version": 2,
            "distribution": runtime["distribution"], "version": runtime["version"],
            "license": runtime["license"], "sources": sources,
            "application_files": application_inventory,
            "recipe": {"helper": "packaging/rebuild_piper_offline.py",
                       "build": ["python -m pip wheel --no-deps ."],
                       "build_requirements": ["CPython 3.12"]},
        }).encode()
        contents = {
            "piper-source/piper_model.json": manifest_bytes,
            "piper-source/source-manifest.json": record,
            **{f"piper-source/{source['name']}": b"synthetic-piper-source" for source in sources},
            **{f"piper-source/application/{relative}": file.read_bytes()
               for relative, file in application_files.items()},
            "piper-source/rebuild_piper_offline.py": (PACKAGING / "rebuild_piper_offline.py").read_bytes(),
        }
        if changes:
            contents.update(changes)
        path = directory / f"TranslateService-{value}-{target}-piper-source.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, data in contents.items():
                if data is None:
                    continue
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        return path, {
            "runtime": f'{runtime["distribution"]}=={runtime["version"]}',
            "license": runtime["license"], "source_archive": path.name,
            "source_sha256": build.sha256(path),
            "source_manifest_sha256": hashlib.sha256(record).hexdigest(),
            "model": piper_model.model_metadata(model),
            "additional_models": [
                piper_model.model_metadata(voice)
                for voice in manifest.get("additional_voices", [])
            ],
        }

    def test_piper_release_gate_rejects_altered_sources_and_model_provenance(self):
        directory = PACKAGING / ".work" / "unit-piper-release"
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        try:
            inputs = self.make_source_inputs(directory)
            manifest = json.loads((inputs / "piper_model.json").read_text())
            name = f'piper-source/piper1-gpl-{manifest["runtime"]["source_revision"]}.tar.gz'
            with patch.object(release, "PACKAGING", inputs):
                path, info = self.make_piper_archive(directory, inputs, "macos-arm64", build.version())
                release.verify_piper_source_archive(path, info)
                for changes in ({name: None}, {name: b"altered"},
                                {"piper-source/piper_model.json": b"{}"},
                                {"piper-source/application/source/worker/bundled_worker.py": None},
                                {"piper-source/application/source/worker/standalone_app.py": b"altered"},
                                {"piper-source/rebuild_piper_offline.py": b"altered"},
                                {"piper-source/unexpected.txt": b"unexpected"},
                                {"../unexpected": b"unsafe"}):
                    with self.subTest(changes=changes):
                        path, info = self.make_piper_archive(
                            directory, inputs, "macos-arm64", build.version(), changes,
                        )
                        with self.assertRaises(ValueError):
                            release.verify_piper_source_archive(path, info)
                path, info = self.make_piper_archive(directory, inputs, "macos-arm64", build.version())
                for changes in (
                    {"runtime": "piper-tts==wrong"}, {"model": {}}, {"license": "unknown"},
                    {"additional_models": None}, {"additional_models": []},
                    {"additional_models": [{"name": "en_US-john-medium"}]},
                ):
                    with self.subTest(changes=changes), self.assertRaises(ValueError):
                        release.verify_piper_source_archive(path, {**info, **changes})
                altered = json.loads(json.dumps(info))
                altered["additional_models"][0]["files"]["en_US-john-medium.onnx"] = "0" * 64
                with self.assertRaisesRegex(ValueError, "provenance"):
                    release.verify_piper_source_archive(path, altered)
        finally:
            shutil.rmtree(directory)

    def test_installer_displays_combined_gpl_terms_and_preserves_mit_notice(self):
        directory = PACKAGING / ".work" / "unit-combined-license"
        shutil.rmtree(directory, ignore_errors=True)

        def copy_notices(destination):
            destination.mkdir(parents=True)
            (destination / "COPYING").write_text("GNU GENERAL PUBLIC LICENSE Version 3")

        def copy_pyav_notices(destination):
            destination.mkdir(parents=True)
            (destination / "PyAV-LICENSE.txt").write_text("PyAV BSD license")
            (destination / "FFmpeg-COPYING.LGPLv2.1.txt").write_text("FFmpeg LGPL license")
            (destination / "managed-source-manifest.json").write_text("{}")

        try:
            with patch.object(build, "WORK", directory), \
                 patch.object(build, "copy_piper_notices", side_effect=copy_notices), \
                 patch.object(pyav_source, "copy_notices", side_effect=copy_pyav_notices), \
                 patch.object(build.importlib.metadata, "distributions", return_value=[]):
                build.generate_metadata("windows-x64")
            notices = directory / "notices"
            text = (notices / "TranslateService-BUNDLED-LICENSE.txt").read_text()
            self.assertIn("GPL-3.0-or-later", text)
            self.assertIn("GNU GENERAL PUBLIC LICENSE Version 3", text)
            self.assertIn((PACKAGING.parent / "LICENSE").read_text(), text)
            self.assertEqual((notices / "TranslateService-LICENSE.txt").read_text(),
                             (PACKAGING.parent / "LICENSE").read_text())
            self.assertIn("TranslateService-BUNDLED-LICENSE.txt",
                          (PACKAGING / "installer.iss").read_text())
        finally:
            shutil.rmtree(directory)

    @unittest.skipIf(sys.platform == "win32", "macOS bundle symlink layout")
    def test_model_verification_accepts_macos_resource_symlink(self):
        directory = PACKAGING / ".work" / "unit-piper-resource-link"
        shutil.rmtree(directory, ignore_errors=True)
        model = directory / "Contents" / "Resources" / "piper-model"
        model.mkdir(parents=True)
        frameworks = directory / "Contents" / "Frameworks"
        frameworks.mkdir()
        (frameworks / "piper-model").symlink_to("../Resources/piper-model", target_is_directory=True)
        (model / "model.onnx").write_bytes(b"test-model")
        manifest = {"voice": {"files": [
            {"name": "model.onnx", "sha256": hashlib.sha256(b"test-model").hexdigest()},
        ]}}
        try:
            with patch.object(build, "load_model_manifest", return_value=manifest):
                with self.assertRaisesRegex(RuntimeError, "Missing adjacent executable"):
                    build.verify_bundle(directory, "macos-arm64")
        finally:
            shutil.rmtree(directory)

    def test_model_downloads_use_independent_atomic_temporary_files(self):
        directory = PACKAGING / ".work" / "unit-piper-download"
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir()
        payload = b"verified-model"
        barrier = threading.Barrier(2)

        class Response(io.BytesIO):
            def read(self, size=-1):
                value = super().read(size)
                if not value:
                    barrier.wait(timeout=5)
                return value

        destination = directory / "voice.onnx"
        digest = hashlib.sha256(payload).hexdigest()
        try:
            with patch.object(piper_model.urllib.request, "urlopen",
                              side_effect=lambda *args, **kwargs: Response(payload)):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(piper_model._download, "https://example.invalid",
                                           destination, digest) for _ in range(2)]
                    for future in futures:
                        future.result(timeout=10)
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(list(directory.iterdir()), [destination])
        finally:
            shutil.rmtree(directory)

    def test_distribution_rejects_job_history_logs_and_user_settings(self):
        bundle = PACKAGING / ".work" / "unit-private-data"
        shutil.rmtree(bundle, ignore_errors=True)
        try:
            assets = bundle / "Contents" / "Resources"
            assets.mkdir(parents=True, exist_ok=True)
            (assets / "VERSION").write_text("0.1.0")
            build.verify_no_private_data(bundle)
            for name in ("jobs.db", "jobs.db-wal", "jobs.db-shm", "settings.json",
                         "engineering_terms_custom.csv", "jobs", "logs", "queue", "processing"):
                path = assets / name
                path.touch()
                with self.assertRaisesRegex(RuntimeError, "Forbidden runtime/private"):
                    build.verify_no_private_data(bundle)
                path.unlink()
        finally:
            shutil.rmtree(bundle, ignore_errors=True)

    def test_stage_ffmpeg_passes_native_target_and_application_version(self):
        helper = ModuleType("ffmpeg_source")
        expected = {"source_archive": PACKAGING / ".work" / "example.tar.gz",
                    "source_sha256": "a" * 64,
                    "license": "GPL-3.0-or-later", "nonfree": False}
        helper.stage_ffmpeg = Mock(return_value=expected)
        with patch.dict(sys.modules, {"ffmpeg_source": helper}):
            self.assertIs(build.stage_ffmpeg("macos-arm64"), expected)
        helper.stage_ffmpeg.assert_called_once_with("macos-arm64", build.version())

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
        hooks.collect_data_files = Mock(side_effect=[assets, [
            ("/installed/piper/espeak-ng-data/voices/en", "piper/espeak-ng-data/voices"),
        ]])
        model_dir = PACKAGING / ".work" / "unit-piper-model"
        model_dir.mkdir(parents=True, exist_ok=True)
        for name in (
            "en_US-ljspeech-medium.onnx",
            "en_US-ljspeech-medium.onnx.json",
            "MODEL_CARD",
        ):
            (model_dir / name).write_bytes(b"test")
        try:
            with patch.object(piper_model, "MODEL_STAGE", model_dir), \
                 patch.dict(sys.modules, {"PyInstaller.utils.hooks": hooks}):
                exec(compile((PACKAGING / "TranslateService.spec").read_text(), "spec", "exec"), namespace)
        finally:
            shutil.rmtree(model_dir)
        self.assertEqual(hooks.collect_data_files.call_args_list, [
            unittest.mock.call("faster_whisper"),
            unittest.mock.call("piper", includes=["espeak-ng-data/**/*"]),
        ])
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
                self.assertIn(destination, {
                    ".", "lecture_poc/prompts", "third-party-notices", "faster_whisper/assets",
                    "piper-model", "piper/espeak-ng-data/voices", "docs",
                })
            self.assertIn((str(PACKAGING.parent / "docs"), "docs"), kwargs["datas"])
            self.assertTrue(any(destination == "lecture_poc/prompts"
                                for _, destination in kwargs["datas"]))
        self.assertFalse(calls["EXE"][0][1]["console"])
        self.assertTrue(calls["EXE"][1][1]["console"])
        self.assertNotIn("lecture_poc.__main__", calls["Analysis"][0][1]["hiddenimports"])
        self.assertIn("lecture_poc.__main__", calls["Analysis"][1][1]["hiddenimports"])
        self.assertIn("piper.espeakbridge", calls["Analysis"][1][1]["hiddenimports"])
        for voice in piper_model.supported_voices():
            for asset in piper_model.voice_manifest(voice)["files"]:
                self.assertIn(
                    (str(model_dir / asset["name"]), "piper-model"),
                    calls["Analysis"][1][1]["datas"],
                )

    def test_piper_model_manifest_and_source_archive_pins(self):
        manifest = piper_model.load_model_manifest()
        runtime = manifest["runtime"]
        voice = manifest["voice"]
        self.assertEqual((runtime["distribution"], runtime["version"]), ("piper-tts", "1.3.0"))
        self.assertEqual(runtime["license"], "GPL-3.0-or-later")
        self.assertRegex(runtime["source_archive_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            runtime["embedded_espeak_ng"]["source_revision"],
            "212928b394a96e8fd2096616bfd54e17845c48f6",
        )
        self.assertEqual(voice["name"], "en_US-ljspeech-medium")
        self.assertEqual(voice["license"], "Public Domain")
        self.assertEqual(voice["revision"], "c10ece1aade47bb51c153c893d14e5bf8e5b7117")
        self.assertEqual({item["name"] for item in voice["files"]}, {
            "en_US-ljspeech-medium.onnx",
            "en_US-ljspeech-medium.onnx.json",
            "MODEL_CARD",
            "piper_model_license.txt",
        })
        for asset in voice["files"]:
            self.assertRegex(asset["sha256"], r"^[0-9a-f]{64}$")
        self.assertIn(
            "TranslateService-<version>-<target>-piper-source.tar.gz",
            (PACKAGING / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8"),
        )
        bundle_root = PACKAGING / ".work" / "unit-piper-bundle"
        cache_dir = PACKAGING / ".work" / "unit-piper-cache"
        with patch.object(piper_model, "_is_valid_model_dir", side_effect=lambda path, voice: path in {
            bundle_root / "piper-model", cache_dir,
        }):
            self.assertEqual(piper_model.resolve_model_dir(bundle_root), bundle_root / "piper-model")
            with patch.object(piper_model, "MODEL_STAGE", PACKAGING / ".work" / "missing-piper-model"), \
                 patch.object(piper_model, "default_model_cache", return_value=cache_dir):
                self.assertEqual(piper_model.resolve_model_dir(), cache_dir)
                with patch.object(piper_model, "_is_valid_model_dir", return_value=False), \
                     patch.object(piper_model, "stage_model", return_value=cache_dir) as stage_model:
                    self.assertEqual(piper_model.resolve_model_dir(), cache_dir)
                    stage_model.assert_called_once_with(cache_dir, voice=piper_model.DEFAULT_VOICE)

    def test_john_manifest_pins_original_creator_downloads_and_evidence(self):
        self.assertEqual(piper_model.supported_voices(), (
            "en_US-ljspeech-medium", "en_US-john-medium",
        ))
        john = piper_model.voice_manifest("en_US-john-medium")
        self.assertEqual(john["creator_page"], "https://brycebeattie.com/files/tts/")
        self.assertEqual(john["license"], "Public Domain")
        self.assertEqual(
            {asset["name"]: (asset.get("url"), asset["sha256"]) for asset in john["files"]},
            {
                "en_US-john-medium.onnx": (
                    "https://sfo3.digitaloceanspaces.com/bkmdls/john.onnx",
                    "789c6c875726e627ddee93d51d8727859abe9c091c3d141591f4b83c2072e988",
                ),
                "en_US-john-medium.onnx.json": (
                    "https://sfo3.digitaloceanspaces.com/bkmdls/john.onnx.json",
                    "1d0433ba8a5182d0295dfcb6c036e9fd27b722f4d379a61fb84472d29b08529f",
                ),
                "john_model_license.txt": (None, build.sha256(PACKAGING / "john_model_license.txt")),
            },
        )
        with self.assertRaisesRegex(ValueError, "Unsupported Piper voice"):
            piper_model.voice_manifest("unknown")
        with self.assertRaisesRegex(ValueError, "Unsupported Piper voice"):
            piper_model.resolve_model_paths(voice="unknown")

    def test_selected_john_source_cache_downloads_and_checks_only_exact_assets(self):
        directory = PACKAGING / ".work" / "unit-john-cache"
        shutil.rmtree(directory, ignore_errors=True)
        manifest = piper_model.load_model_manifest()
        john = manifest["additional_voices"][0]
        payloads = {
            asset["url"]: asset["name"].encode()
            for asset in john["files"] if "url" in asset
        }
        for asset in john["files"]:
            if "url" in asset:
                asset["sha256"] = hashlib.sha256(payloads[asset["url"]]).hexdigest()
        try:
            with patch.object(piper_model, "load_model_manifest", return_value=manifest), \
                 patch.object(piper_model, "MODEL_STAGE", directory / "absent-stage"), \
                 patch.object(piper_model, "default_model_cache", return_value=directory), \
                 patch.dict(piper_model.os.environ, {}, clear=True), \
                 patch.object(piper_model.urllib.request, "urlopen",
                              side_effect=lambda url, timeout: io.BytesIO(payloads[url])) as request:
                model, config = piper_model.resolve_model_paths(voice=john["name"])
                self.assertEqual((model.name, config.name), (
                    "en_US-john-medium.onnx", "en_US-john-medium.onnx.json",
                ))
                self.assertEqual(request.call_count, 2)
                self.assertEqual({call.args[0] for call in request.call_args_list}, set(payloads))
                self.assertFalse((directory / "en_US-ljspeech-medium.onnx").exists())
                request.reset_mock()
                piper_model.resolve_model_paths(voice=john["name"])
                request.assert_not_called()
                model.write_bytes(b"tampered-cache")
                piper_model.resolve_model_paths(voice=john["name"])
                self.assertEqual(request.call_count, 1)
                self.assertEqual(model.read_bytes(), payloads[john["files"][0]["url"]])
                model.unlink()
                request.side_effect = lambda url, timeout: io.BytesIO(b"unpinned-download")
                with self.assertRaisesRegex(piper_model.PiperModelError, "SHA-256 mismatch"):
                    piper_model.resolve_model_paths(voice=john["name"])
                self.assertFalse(model.exists())
                self.assertEqual(list(directory.glob("*.partial")), [])
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_bundled_john_never_downloads_or_falls_back_to_female(self):
        root = PACKAGING / ".work" / "unit-john-bundle"
        shutil.rmtree(root, ignore_errors=True)
        directory = root / "piper-model"
        directory.mkdir(parents=True)
        manifest = piper_model.load_model_manifest()
        for voice in [manifest["voice"], *manifest["additional_voices"]]:
            for asset in voice["files"]:
                content = asset["name"].encode()
                (directory / asset["name"]).write_bytes(content)
                asset["sha256"] = hashlib.sha256(content).hexdigest()
        try:
            with patch.object(piper_model, "load_model_manifest", return_value=manifest), \
                 patch.object(piper_model, "stage_model") as download:
                john = piper_model.resolve_model_paths(root, voice="en_US-john-medium")[0]
                for damage in ("missing", "tampered"):
                    with self.subTest(damage=damage):
                        if damage == "missing":
                            john.unlink()
                        else:
                            john.write_bytes(b"tampered")
                        with self.assertRaisesRegex(FileNotFoundError, "missing or damaged"):
                            piper_model.resolve_model_paths(root, voice="en_US-john-medium")
                        self.assertEqual(piper_model.resolve_model_paths(root)[0].name,
                                         "en_US-ljspeech-medium.onnx")
                download.assert_not_called()
                john.write_bytes(john.name.encode())
                (directory / "en_US-ljspeech-medium.onnx").unlink()
                self.assertEqual(piper_model.resolve_model_paths(root, voice="en_US-john-medium")[0], john)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_bundle_requires_both_asset_sets_and_both_offline_smoke_results(self):
        root = PACKAGING / ".work" / "unit-john-build"
        shutil.rmtree(root, ignore_errors=True)
        directory = root / "piper-model"
        directory.mkdir(parents=True)
        for name in ("TranslateService", "TranslateWorker", "ffmpeg", "ffprobe"):
            (root / name).touch()
        manifest = piper_model.load_model_manifest()
        for voice in [manifest["voice"], *manifest["additional_voices"]]:
            for asset in voice["files"]:
                content = asset["name"].encode()
                (directory / asset["name"]).write_bytes(content)
                asset["sha256"] = hashlib.sha256(content).hexdigest()
        results = {name: {"voice": name, "wav_duration_seconds": 0.2}
                   for name in piper_model.supported_voices()}

        def run(command, **kwargs):
            if "--bundle-self-test" in command:
                role = "worker" if Path(command[0]).name == "TranslateWorker" else "gui"
                Path(command[-1]).write_text(json.dumps({
                    "role": role, "status": "ok", "version": build.version(),
                    "tools": {"ffmpeg": {}, "ffprobe": {}},
                    "piper": {"voice": piper_model.DEFAULT_VOICE,
                              "wav_duration_seconds": 0.2, "voices": results},
                }))
            return subprocess.CompletedProcess(command, 0, stdout="--input --outdir", stderr="")

        try:
            with patch.object(build, "load_model_manifest", return_value=manifest), \
                 patch.object(build, "WORK", root / "work"), \
                 patch.object(build, "run", side_effect=run) as command, \
                 patch.object(build, "validate_ffmpeg_license"), \
                 patch.object(build, "verify_media_tools"), \
                 patch.object(build, "verify_managed_pyav"):
                self.assertEqual(len(build.verify_bundle(root, "macos-x64")), 2)
                male_asset = directory / "en_US-john-medium.onnx"
                male_asset.unlink()
                command.reset_mock()
                with self.assertRaisesRegex(RuntimeError, "wrong hash"):
                    build.verify_bundle(root, "macos-x64")
                command.assert_not_called()
                male_asset.write_bytes(b"tampered")
                with self.assertRaisesRegex(RuntimeError, "wrong hash"):
                    build.verify_bundle(root, "macos-x64")
                male_asset.write_bytes(male_asset.name.encode())
                results.pop("en_US-john-medium")
                with self.assertRaisesRegex(RuntimeError, "incomplete"):
                    build.verify_bundle(root, "macos-x64")
                for duration in (0, -1, True, float("nan"), float("inf")):
                    results["en_US-john-medium"] = {
                        "voice": "en_US-john-medium", "wav_duration_seconds": duration,
                    }
                    with self.subTest(duration=duration), self.assertRaisesRegex(RuntimeError, "failed"):
                        build.verify_bundle(root, "macos-x64")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_piper_source_archive_inventories_allowlisted_application_sources(self):
        work = PACKAGING / ".work" / "unit-piper-source-application"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        staged_sources = piper_source.WORK / "piper-source"
        local_source_dir = work / "piper-source"
        local_source_dir.mkdir()
        source_names = [
            f'piper1-gpl-{piper_model.load_model_manifest()["runtime"]["source_revision"]}.tar.gz',
            f'espeak-ng-{piper_model.load_model_manifest()["runtime"]["embedded_espeak_ng"]["source_revision"]}.tar.gz',
        ]
        for name in source_names:
            shutil.copy2(staged_sources / name, local_source_dir / name)
        try:
            with patch.object(piper_source, "WORK", work):
                archive_path = piper_source.stage_piper_source()
            expected = piper_source.application_source_files()
            with tarfile.open(archive_path, "r:gz") as archive:
                names = set(archive.getnames())
                manifest = json.load(archive.extractfile("piper-source/source-manifest.json"))
                inventoried = manifest["application_files"]
                self.assertEqual(set(inventoried), set(expected))
                for relative, path in expected.items():
                    member_name = f"piper-source/application/{relative}"
                    self.assertIn(member_name, names)
                    self.assertEqual(
                        hashlib.sha256(archive.extractfile(member_name).read()).hexdigest(),
                        inventoried[relative],
                    )
                self.assertIn("piper-source/application/LICENSE", names)
                self.assertIn("piper-source/application/README.md", names)
                self.assertIn("piper-source/application/docs/PACKAGING.md", names)
                self.assertIn("piper-source/application/packaging/john_model_license.txt", names)
                self.assertIn("piper-source/application/packaging/pyav.json", names)
                self.assertIn("piper-source/application/packaging/pyav_source.py", names)
                self.assertIn("piper-source/application/packaging/build-pyav-ffmpeg.sh", names)
                self.assertIn("piper-source/rebuild_piper_offline.py", names)
                self.assertNotIn("piper-source/application/source/worker/data/jobs.db", names)
                self.assertNotIn("piper-source/application/source/worker/tests/test_tts.py", names)
                self.assertEqual(manifest["schema_version"], 2)
                self.assertIn("piper_source_changes", manifest["recipe"])
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_piper_rebuild_helper_uses_pinned_local_espeak_sources(self):
        manifest = piper_model.load_model_manifest()
        runtime = manifest["runtime"]
        piper_archive = (
            piper_source.WORK / "piper-source"
            / f'piper1-gpl-{runtime["source_revision"]}.tar.gz'
        )
        espeak_archive = (
            piper_source.WORK / "piper-source"
            / f'espeak-ng-{runtime["embedded_espeak_ng"]["source_revision"]}.tar.gz'
        )
        destination = PACKAGING / ".work" / "unit-piper-offline-rebuild"
        shutil.rmtree(destination, ignore_errors=True)
        try:
            source = piper_source.REBUILD_HELPER
            spec = importlib.util.spec_from_file_location("rebuild_piper_offline_test", source)
            helper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(helper)
            prepared = helper.prepare(
                piper_archive, espeak_archive, piper_model.MODEL_MANIFEST, destination
            )
            cmake = (prepared / "CMakeLists.txt").read_text(encoding="utf-8")
            self.assertIn("SOURCE_DIR ${CMAKE_CURRENT_SOURCE_DIR}/espeak-ng-source", cmake)
            self.assertIn('DOWNLOAD_COMMAND ""', cmake)
            self.assertIn('UPDATE_COMMAND ""', cmake)
            self.assertIn(
                "${CMAKE_CURRENT_SOURCE_DIR}/espeak-ng-source/src/ucd-tools/src/include",
                cmake,
            )
            self.assertNotIn(
                "GIT_REPOSITORY https://github.com/espeak-ng/espeak-ng.git", cmake
            )
            self.assertTrue(
                (prepared / "espeak-ng-source" / "src" / "ucd-tools" / "CMakeLists.txt").is_file()
            )
        finally:
            shutil.rmtree(destination, ignore_errors=True)

    def test_piper_source_notices_copy_pinned_licenses_without_extracting_unsafe_paths(self):
        manifest = piper_model.load_model_manifest()
        runtime = manifest["runtime"]
        work = PACKAGING / ".work" / "unit-piper-source-notices"
        source_dir = work / "piper-source"
        shutil.rmtree(work, ignore_errors=True)
        source_dir.mkdir(parents=True)
        archives = (
            (
                source_dir / f'piper1-gpl-{runtime["source_revision"]}.tar.gz',
                {"piper-root/COPYING": b"piper GPL", "../escape/LICENSE": b"unsafe"},
            ),
            (
                source_dir / f'espeak-ng-{runtime["embedded_espeak_ng"]["source_revision"]}.tar.gz',
                {"espeak-root/COPYING.UCD": b"espeak UCD"},
            ),
        )
        for path, entries in archives:
            with tarfile.open(path, "w:gz") as archive:
                for name, content in entries.items():
                    member = tarfile.TarInfo(name)
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
        notices = work / "notices"
        try:
            with patch.object(piper_source, "WORK", work):
                piper_source.copy_piper_notices(notices / "piper")
            self.assertEqual((notices / "piper" / "COPYING").read_bytes(), b"piper GPL")
            self.assertEqual(
                (notices / "piper" / "espeak-ng" / "COPYING.UCD").read_bytes(),
                b"espeak UCD",
            )
            self.assertFalse((work / "escape" / "LICENSE").exists())
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_worker_piper_smoke_requires_nonempty_wav_across_multiple_chunks(self):
        class Chunk:
            sample_channels = 1
            sample_width = 2
            sample_rate = 22050
            audio_int16_bytes = b"\x00\x00" * 2205

        class Voice:
            @classmethod
            def load(cls, model_path, *, config_path, use_cuda):
                assert (model_path, config_path, use_cuda) == ("voice.onnx", "voice.json", False)
                return cls()

            def synthesize(self, text):
                assert text == "Piper is ready."
                return iter((Chunk(), Chunk()))

        piper = ModuleType("piper")
        piper.PiperVoice = Voice
        with patch.object(app_runtime, "piper_model_paths", return_value=(Path("voice.onnx"), Path("voice.json"))), \
             patch.dict(sys.modules, {"piper": piper}):
            report = app_runtime._piper_smoke_test()
        self.assertEqual(report["voice"], "en_US-ljspeech-medium")
        self.assertAlmostEqual(report["wav_duration_seconds"], 0.2)
        self.assertEqual(set(report["voices"]), set(piper_model.supported_voices()))
        for name, result in report["voices"].items():
            self.assertEqual(result["voice"], name)
            self.assertAlmostEqual(result["wav_duration_seconds"], 0.2)

    def test_source_and_bundle_requirements_use_piper_not_removed_tts(self):
        for path in (
            PACKAGING / "requirements.in",
            PACKAGING / "locks" / "macos-arm64.txt",
            PACKAGING / "locks" / "macos-x64.txt",
            PACKAGING / "locks" / "windows-x64.txt",
            PACKAGING.parent / "source" / "worker" / "requirements.txt",
        ):
            text = path.read_text(encoding="utf-8").lower()
            self.assertIn("piper-tts", text, path.name)
            self.assertNotIn("edge-tts", text, path.name)
            self.assertNotIn("xtts", text, path.name)
        self.assertFalse((PACKAGING.parent / "source" / "worker" / "requirements-xtts.txt").exists())
        for target in ("macos-arm64", "macos-x64", "windows-x64"):
            text = (PACKAGING / "locks" / f"{target}.txt").read_text(encoding="utf-8")
            self.assertIn("torch==2.2.2", text)
            self.assertIn("numpy==1.26.4", text)
            self.assertIn("faster-whisper==1.1.1", text)
            self.assertRegex(
                text,
                r"(?m)^piper-tts==1\.3\.0 \\\n(?: +--hash=sha256:[0-9a-f]{64} \\\n)+",
            )

    def test_ffmpeg_manifest_is_complete_and_pinned(self):
        manifest = json.loads((PACKAGING / "ffmpeg.json").read_text())
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["license"], "GPL-3.0-or-later")
        self.assertIs(manifest["nonfree"], False)
        self.assertEqual(len(manifest["sources"]), 8)
        names = set()
        for source in manifest["sources"]:
            self.assertRegex(source["sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(source["url"].startswith("https://"))
            self.assertNotIn("latest", source["url"])
            self.assertEqual(Path(source["name"]).name, source["name"])
            self.assertNotIn(source["name"], names)
            names.add(source["name"])
        self.assertTrue(any(name.startswith("ffmpeg-") for name in names))
        self.assertTrue(any(name.startswith("x264-") for name in names))

    def test_managed_pyav_sources_are_pinned_shared_lgpl_and_installed_before_freezing(self):
        manifest = pyav_source._manifest()
        self.assertEqual(manifest["pyav"]["version"], "15.1.0")
        self.assertEqual(
            manifest["pyav"]["sha256"],
            "39cda2dc810e11c1938f8cb5759c41d6b630550236b3365790e67a313660ec85",
        )
        self.assertEqual(manifest["pyav"]["license"], "BSD-3-Clause")
        self.assertEqual(manifest["ffmpeg"]["version"], "7.1.1")
        self.assertEqual(manifest["ffmpeg"]["license"], "LGPL-2.1-or-later")
        self.assertEqual(manifest["recipe"]["license"], "LGPL-2.1-or-later")
        self.assertIs(manifest["recipe"]["nonfree"], False)
        configure = manifest["recipe"]["configure_args"]
        for flag in (
            "--disable-gpl", "--disable-nonfree", "--disable-version3",
            "--disable-encoders", "--disable-muxers", "--disable-network",
            "--disable-static", "--enable-shared",
        ):
            self.assertIn(flag, configure)
        for flag in ("--enable-gpl", "--enable-nonfree", "--enable-version3",
                     "--enable-libx264", "--enable-libx265"):
            self.assertNotIn(flag, configure)
        standalone_recipe = (PACKAGING / "build-ffmpeg.sh").read_text()
        self.assertIn("--enable-gpl --enable-version3 --enable-libx264", standalone_recipe)
        for wrapper in ("build-macos.sh", "build-windows.ps1"):
            text = (PACKAGING / wrapper).read_text()
            self.assertLess(text.index("pyav_source.py"), text.index("build.py"))
            self.assertIn("build-install", text)
            self.assertIn("verify", text)
        for target, digest in (
            ("macos-arm64", "66ac8bb5068156c92359e3f0eefa138c177d59d1a2e8a89467881fa7d06aba3b"),
            ("macos-x64", "0d26af46505d0e54fe0f05e7ad089fd0eed8fa04f385f3ab88796f554467bcb9"),
            ("windows-x64", "f262b32327b6bce340cce5d45bbfe3972cb62543a4930460d8564a489f3aea12"),
        ):
            text = (PACKAGING / "locks" / f"{target}.txt").read_text()
            self.assertRegex(text, rf"(?m)^cython==3\.1\.4 \\\n +--hash=sha256:{digest}$")

    def test_source_hygiene_and_gitless_manifest_provenance(self):
        empty = subprocess.CompletedProcess([], 0, stdout=b"")
        with patch.object(build, "subprocess") as subprocess_mock:
            subprocess_mock.run.return_value = empty
            build.verify_source_hygiene()
            subprocess_mock.run.assert_called_once()
            subprocess_mock.run.return_value = subprocess.CompletedProcess(
                [], 0, stdout=b"source/worker/data/jobs.db\0"
            )
            with self.assertRaisesRegex(RuntimeError, "Tracked user/runtime data"):
                build.verify_source_hygiene()

        directory = PACKAGING / ".work" / "unit-gitless-provenance"
        shutil.rmtree(directory, ignore_errors=True)
        root = directory / "application"
        root.mkdir(parents=True)
        source_file = root / "README.md"
        source_file.write_text("archived source tree", encoding="utf-8")
        source_manifest = directory / "source-manifest.json"
        source_manifest.write_text(json.dumps({
            "application_files": {"README.md": build.sha256(source_file)},
        }), encoding="utf-8")
        try:
            with patch.object(build, "ROOT", root), \
                 patch.object(build, "WORK", directory / "work"), \
                 patch.object(build, "application_source_files", return_value={"README.md": source_file}), \
                 patch.dict(os.environ, {"TRANSLATESERVICE_SOURCE_REVISION": ""}):
                revision, provenance = build.source_provenance()
            self.assertEqual(provenance["kind"], "source-manifest-sha256")
            self.assertEqual(revision, f"source-manifest-sha256:{provenance['value']}")
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_source_archive_rejects_wrong_sources_recipe_records_and_unsafe_paths(self):
        directory = PACKAGING / ".work" / "unit-source-archive"
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        try:
            inputs = self.make_source_inputs(directory)
            with patch.object(release, "PACKAGING", inputs):
                for changes in (
                    {"sources/ffmpeg-test.tar.xz": None},
                    {"sources/ffmpeg-test.tar.xz": b"altered-source"},
                    {"ffmpeg.json": b"{}"},
                    {"build-ffmpeg.sh": b"altered-recipe"},
                    {"build-info.json": b"{}"},
                    {"build/ffmpeg-build.log": None},
                    {"build/ffmpeg-configure-args.txt": b"--enable-nonfree"},
                    {"../unexpected": b"unsafe"},
                ):
                    with self.subTest(changes=changes):
                        path, media = self.make_source_archive(
                            directory, inputs, "macos-arm64", build.version(), changes,
                        )
                        with self.assertRaises(ValueError):
                            release.verify_source_archive(path, "macos-arm64", build.version(), media)
                path, media = self.make_source_archive(
                    directory, inputs, "macos-arm64", build.version(),
                )
                release.verify_source_archive(path, "macos-arm64", build.version(), media)
        finally:
            shutil.rmtree(directory)

    def test_managed_pyav_source_archive_rejects_tampered_corresponding_sources(self):
        directory = PACKAGING / ".work" / "unit-pyav-source-gate"
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        try:
            inputs = self.make_source_inputs(directory)
            path, info = self.make_pyav_source_archive(
                directory, inputs, "macos-arm64", build.version(),
            )
            with patch.object(release, "PACKAGING", inputs):
                release.verify_pyav_source_archive(path, "macos-arm64", build.version(), info)
                incomplete_smoke = {**info["decode_smoke"]}
                incomplete_smoke.pop("frozen_decode")
                with self.assertRaisesRegex(ValueError, "decode"):
                    release.verify_pyav_source_archive(
                        path, "macos-arm64", build.version(),
                        {**info, "decode_smoke": incomplete_smoke},
                    )
                with tarfile.open(path, "r:gz") as archive:
                    contents = {
                        member.name: archive.extractfile(member).read()
                        for member in archive.getmembers()
                    }
                contents["sources/av-15.1.0.tar.gz"] += b"tampered"
                with tarfile.open(path, "w:gz") as archive:
                    for name, data in contents.items():
                        member = tarfile.TarInfo(name)
                        member.size = len(data)
                        archive.addfile(member, io.BytesIO(data))
                with self.assertRaisesRegex(ValueError, "source hash mismatch"):
                    release.verify_pyav_source_archive(path, "macos-arm64", build.version(), info)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

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
            self.assertIn("cython==3.1.4", text)
        bootstrap = (PACKAGING / "locks" / "bootstrap.txt").read_text()
        for name in ("pip", "setuptools", "wheel"):
            self.assertRegex(bootstrap, rf"(?m)^{name}==[^\s]+ \\\n +--hash=sha256:")

    def test_release_rejects_tampering_missing_or_extra_assets(self):
        directory = PACKAGING / ".work" / "unit-release-assets"
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        try:
            inputs = self.make_source_inputs(directory)
            value = build.version()
            for target in release.TARGETS:
                prefix = f"TranslateService-{value}-{target}"
                extensions = ("-setup.exe", "-portable.zip") if target == "windows-x64" else (".dmg",)
                for extension in extensions:
                    (directory / f"{prefix}{extension}").write_bytes(b"synthetic-test-artifact")
                source, media = self.make_source_archive(directory, inputs, target, value)
                _, piper_info = self.make_piper_archive(directory, inputs, target, value)
                _, pyav_info = self.make_pyav_source_archive(directory, inputs, target, value)
                info = {"version": value, "target": target, "commit": "test",
                        "source_provenance": {"kind": "explicit-revision", "value": "test"},
                        "ffmpeg_manifest_sha256": media["manifest_sha256"], "ffmpeg": media,
                        "piper": piper_info, "pyav": pyav_info,
                        "self_tests": [{"status": "ok", "role": role, "version": value}
                                       for role in ("gui", "worker")]}
                (directory / f"{prefix}-build-info.json").write_text(json.dumps(info))
                paths = sorted(directory.glob(f"{prefix}*"))
                (directory / f"{prefix}-SHA256SUMS.txt").write_text(
                    "".join(f"{build.sha256(path)}  {path.name}\n" for path in paths)
                )
            with patch.object(release, "ARTIFACTS", directory), patch.object(release, "PACKAGING", inputs):
                self.assertEqual(len(release.validate_assets("v" + value)), 19)
                source_info = directory / f"TranslateService-{value}-windows-x64-build-info.json"
                original = json.loads(source_info.read_text())
                changed = {**original, "ffmpeg": {**original["ffmpeg"], "nonfree": True}}
                source_info.write_text(json.dumps(changed))
                checksum = directory / f"TranslateService-{value}-windows-x64-SHA256SUMS.txt"
                original_checksum = checksum.read_text()
                # Update checksums to isolate the policy gate from the integrity gate.
                checksum.write_text("".join(
                    f"{build.sha256(directory / line.split('  ', 1)[1])}  {line.split('  ', 1)[1]}\n"
                    for line in original_checksum.splitlines()
                ))
                with self.assertRaisesRegex(ValueError, "license confirmation"):
                    release.validate_assets("v" + value)
                source_info.write_text(json.dumps(original))
                checksum.write_text(original_checksum)
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
