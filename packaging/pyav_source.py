"""Build and verify PyAV against the managed LGPL shared FFmpeg runtime."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
WORK = PACKAGING / ".work"
MANIFEST = PACKAGING / "pyav.json"
LICENSE = "LGPL-2.1-or-later"
RECIPE_FILES = ("pyav.json", "pyav_source.py", "build-pyav-ffmpeg.sh")
TARGETS = {
    "windows-x64": ("Windows", {"amd64", "x86_64"}),
    "macos-arm64": ("Darwin", {"arm64", "aarch64"}),
    "macos-x64": ("Darwin", {"x86_64"}),
}
FFMPEG_LIBRARIES = ("avformat", "avcodec", "avdevice", "avutil", "avfilter", "swscale", "swresample")


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        return digest.hexdigest()


def _checked(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("+", " ".join(map(str, command)), flush=True)
    try:
        return subprocess.run(list(map(str, command)), check=True, **kwargs)
    except subprocess.CalledProcessError as error:
        for output in (error.stdout, error.stderr):
            if output:
                print(output, file=sys.stderr)
        raise


def _manifest() -> dict:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise RuntimeError("Unsupported managed PyAV build manifest schema")
    pyav, ffmpeg, recipe = manifest.get("pyav", {}), manifest.get("ffmpeg", {}), manifest.get("recipe", {})
    for source in (pyav, ffmpeg):
        if (not source.get("name") or Path(source["name"]).name != source["name"]
                or not source.get("url", "").startswith("https://")
                or not re.fullmatch(r"[0-9a-f]{64}", source.get("sha256", ""))):
            raise RuntimeError("Invalid managed PyAV source pin")
    if (pyav.get("version"), pyav.get("license")) != ("15.1.0", "BSD-3-Clause"):
        raise RuntimeError("Managed PyAV version/license policy mismatch")
    if (ffmpeg.get("version"), ffmpeg.get("license"), recipe.get("license"),
            recipe.get("nonfree")) != ("7.1.1", LICENSE, LICENSE, False):
        raise RuntimeError("Managed FFmpeg license policy mismatch")
    if tuple(recipe.get("required_libraries", ())) != FFMPEG_LIBRARIES:
        raise RuntimeError("Managed FFmpeg PyAV library list mismatch")
    args = recipe.get("configure_args", [])
    forbidden = ("--enable-gpl", "--enable-nonfree", "--enable-version3",
                 "--enable-libx264", "--enable-libx265")
    if any(arg in args for arg in forbidden) or not all(
        arg in args for arg in ("--disable-gpl", "--disable-nonfree", "--disable-version3",
                                "--disable-encoders", "--disable-muxers", "--enable-shared",
                                "--disable-static")
    ):
        raise RuntimeError("Managed FFmpeg recipe is not shared, decoding-only LGPL")
    return manifest


def _fetch(source: dict, directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / source["name"]
    if not path.is_file() or _sha256(path) != source["sha256"]:
        path.unlink(missing_ok=True)
        partial = path.with_suffix(path.suffix + ".partial")
        try:
            request = urllib.request.Request(source["url"], headers={
                "User-Agent": "TranslateService managed PyAV source builder",
            })
            with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as output:
                shutil.copyfileobj(response, output)
            actual = _sha256(partial)
            if actual != source["sha256"]:
                raise RuntimeError(f"SHA-256 mismatch for managed source {source['name']}: {actual}")
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)
    return {**source, "sha256": _sha256(path)}


def _extract(source: Path, destination: Path) -> None:
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(source, "r:*") as archive:
        for member in archive.getmembers():
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise RuntimeError(f"Unsafe path in managed source archive: {member.name}")
            if member.issym() or member.islnk():
                link = PurePosixPath(member.linkname)
                if link.is_absolute() or ".." in link.parts:
                    raise RuntimeError(f"Unsafe link in managed source archive: {member.name}")
        archive.extractall(destination, filter="data")


def _msys_path(path: Path) -> str:
    resolved = str(path.resolve())
    if len(resolved) > 2 and resolved[1:3] == ":\\":
        return f"/{resolved[0].lower()}/{resolved[3:].replace(chr(92), '/')}"
    return resolved


def _wheel_command(target: str, prefix: Path, wheels: Path) -> list[str]:
    # PyAV removes this custom argument before setuptools parses command options.
    command = [sys.executable, "setup.py", f"--ffmpeg-dir={prefix}", "build_ext"]
    if target == "windows-x64":
        command.append("--compiler=mingw32")
    return command + ["bdist_wheel", "--dist-dir", str(wheels)]


def _verify_windows_compiler(directory: Path, env: dict) -> None:
    """Fail early unless MinGW can build and load an extension in this CPython."""
    directory.mkdir(parents=True)
    try:
        machine = _checked(["gcc", "-dumpmachine"], env=env, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError("Native MINGW64 GCC is required to build PyAV on Windows") from error
    if machine != "x86_64-w64-mingw32":
        raise RuntimeError(f"Windows PyAV requires native x64 MinGW GCC, found {machine}")
    (directory / "probe.c").write_text(
        '#include <Python.h>\n'
        'static PyObject *answer(PyObject *self, PyObject *args) { return PyLong_FromLong(42); }\n'
        'static PyMethodDef methods[] = {{"answer", answer, METH_NOARGS, NULL}, {NULL}};\n'
        'static struct PyModuleDef module = {PyModuleDef_HEAD_INIT, "_compiler_probe", NULL, -1, methods};\n'
        'PyMODINIT_FUNC PyInit__compiler_probe(void) { return PyModule_Create(&module); }\n',
        encoding="utf-8",
    )
    (directory / "setup.py").write_text(
        'from setuptools import Extension, setup\n'
        'setup(name="compiler-probe", ext_modules=[Extension("_compiler_probe", ["probe.c"])])\n',
        encoding="utf-8",
    )
    try:
        _checked([sys.executable, "setup.py", "build_ext", "--compiler=mingw32", "--inplace"],
                 cwd=directory, env=env, capture_output=True, text=True)
        _checked([sys.executable, "-c", "import _compiler_probe; assert _compiler_probe.answer() == 42"],
                 cwd=directory, env=env, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            "MinGW cannot compile/link/load a CPython 3.12 x64 extension. "
            "Check the CPython headers/import library and native MINGW64 compiler/runtime; "
            "MSVC fallback and an upstream PyAV wheel are not permitted."
        ) from error


def _build_shared_ffmpeg(target: str, downloads: Path, build_dir: Path, prefix: Path) -> None:
    recipe = PACKAGING / "build-pyav-ffmpeg.sh"
    env = os.environ.copy()
    env.update({
        "SOURCE_DIR": _msys_path(downloads) if target == "windows-x64" else str(downloads),
        "BUILD_DIR": _msys_path(build_dir) if target == "windows-x64" else str(build_dir),
        "PREFIX": _msys_path(prefix) if target == "windows-x64" else str(prefix),
        "TARGET": target,
        "BUILD_JOBS": str(max(2, min(os.cpu_count() or 2, 8))),
    })
    if target == "windows-x64":
        bash = Path("C:/msys64/usr/bin/bash.exe")
        if not bash.is_file():
            raise RuntimeError("MSYS2 bash was not found at C:/msys64/usr/bin/bash.exe")
        env["PATH"] = "C:/msys64/mingw64/bin;C:/msys64/usr/bin;" + env.get("PATH", "")
        _checked([str(bash), _msys_path(recipe)], env=env, cwd=ROOT)
    else:
        _checked(["bash", str(recipe)], env=env, cwd=ROOT)


def _library_files(prefix: Path, target: str) -> list[Path]:
    directories = (prefix / "bin", prefix / "lib") if target == "windows-x64" else (prefix / "lib",)
    matches = []
    for library in FFMPEG_LIBRARIES:
        candidates = []
        for directory in directories:
            candidates.extend(path for path in directory.glob(f"*{library}*")
                              if path.is_file() and not path.name.endswith((".a", ".dll.a")))
        if not candidates:
            raise RuntimeError(f"Managed FFmpeg shared library is missing: {library}")
        matches.extend(candidates)
    return sorted(set(matches))


def _verify_extension(prefix: Path, target: str) -> list[Path]:
    av_spec = importlib.util.find_spec("av")
    if av_spec is None or not av_spec.submodule_search_locations:
        raise RuntimeError("The managed PyAV wheel is not installed in the build interpreter")
    package = Path(next(iter(av_spec.submodule_search_locations)))
    extensions = sorted(package.rglob("*.pyd" if target == "windows-x64" else "*.so"))
    if not extensions:
        raise RuntimeError("Installed PyAV has no compiled CPython extension")
    if (package.parent / "av.libs").exists():
        raise RuntimeError("PyAV contains an upstream bundled FFmpeg library directory")
    if target == "windows-x64":
        objdump = shutil.which("objdump") or r"C:\msys64\mingw64\bin\objdump.exe"
        if not Path(objdump).is_file() and not shutil.which(objdump):
            raise RuntimeError("MINGW64 objdump is required to verify PyAV's FFmpeg linkage")
        linked = set()
        for extension in extensions:
            output = _checked([objdump, "-p", str(extension)], capture_output=True, text=True).stdout
            linked.update(
                line.rsplit(" ", 1)[-1].lower() for line in output.splitlines()
                if "DLL Name:" in line
            )
        missing = [name for name in FFMPEG_LIBRARIES
                   if not any(name in dependency for dependency in linked)]
        if missing:
            raise RuntimeError(f"PyAV does not dynamically link managed FFmpeg DLLs: {missing}")
        built_dlls = {path.name.lower() for path in (prefix / "bin").glob("*.dll")}
        unresolved = sorted(
            dependency for dependency in linked
            if any(name in dependency for name in FFMPEG_LIBRARIES) and dependency not in built_dlls
        )
        if unresolved:
            raise RuntimeError(f"PyAV FFmpeg imports are not provided by the managed build: {unresolved}")
    else:
        linked = set()
        rpaths = []
        for extension in extensions:
            output = _checked(["otool", "-L", str(extension)], capture_output=True, text=True).stdout
            linked.update(line.strip().split(" (", 1)[0] for line in output.splitlines()[1:])
            load_commands = _checked(["otool", "-l", str(extension)],
                                     capture_output=True, text=True).stdout
            rpaths.extend(
                line.strip().split(" ", 1)[1] for line in load_commands.splitlines()
                if line.strip().startswith("path ")
            )
        missing = [name for name in FFMPEG_LIBRARIES
                   if not any(name in dependency for dependency in linked)]
        if missing:
            raise RuntimeError(f"PyAV does not dynamically link managed FFmpeg dylibs: {missing}")
        managed_libdir = str(prefix / "lib")
        if not any(managed_libdir in dependency for dependency in linked) and not any(
            managed_libdir in path for path in rpaths
        ):
            raise RuntimeError("PyAV FFmpeg dylibs are not resolved through the managed build prefix")
    return extensions


def verify_installed(target: str) -> dict:
    if target not in TARGETS:
        raise ValueError(f"Unsupported PyAV target: {target}")
    manifest = _manifest()
    target_dir = WORK / "pyav" / target
    record_path = target_dir / "build-info.json"
    if not record_path.is_file():
        raise RuntimeError("Managed PyAV wheel provenance is missing; run pyav_source.py install first")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    expected_manifest_hash = _sha256(MANIFEST)
    if record.get("manifest_sha256") != expected_manifest_hash:
        raise RuntimeError("Managed PyAV build manifest changed after wheel installation")
    if record.get("target") != target or record.get("sources") != [
        {key: source[key] for key in ("name", "url", "sha256", "license")}
        for source in (manifest["pyav"], manifest["ffmpeg"])
    ]:
        raise RuntimeError("Managed PyAV installed-wheel provenance does not match source pins")
    prefix = target_dir / "prefix"
    library_files = _library_files(prefix, target)
    hashes = {path.name: _sha256(path) for path in library_files}
    if hashes != record.get("shared_library_sha256"):
        raise RuntimeError("Managed FFmpeg shared-library provenance mismatch")
    extensions = _verify_extension(prefix, target)
    wheel_name = record.get("wheel", "")
    if Path(wheel_name).name != wheel_name or not wheel_name.endswith(".whl"):
        raise RuntimeError("Managed PyAV wheel record has an unsafe filename")
    wheel = target_dir / "wheelhouse" / wheel_name
    if not wheel.is_file():
        raise RuntimeError("Managed PyAV wheel path is outside the dedicated local wheelhouse")
    if record.get("pyav_wheel_sha256") != _sha256(wheel):
        raise RuntimeError("Managed PyAV wheel hash mismatch")
    with zipfile.ZipFile(wheel) as archive:
        for extension in extensions:
            package = next(parent for parent in extension.parents if parent.name == "av")
            member = extension.relative_to(package.parent).as_posix()
            if member not in archive.namelist() or archive.read(member) != extension.read_bytes():
                raise RuntimeError(f"Installed PyAV extension differs from the managed wheel: {member}")
    return record


def copy_notices(destination: Path) -> None:
    """Stage exact license texts from the pinned PyAV and FFmpeg source archives."""
    manifest = _manifest()
    downloads = WORK / "pyav-source-downloads"
    destination.mkdir(parents=True, exist_ok=True)
    entries = (
        (manifest["pyav"], "av-15.1.0/LICENSE.txt", "PyAV-LICENSE.txt"),
        (manifest["ffmpeg"], "ffmpeg-7.1.1/COPYING.LGPLv2.1", "FFmpeg-COPYING.LGPLv2.1.txt"),
    )
    for source, member_name, output_name in entries:
        archive_path = downloads / source["name"]
        if not archive_path.is_file() or _sha256(archive_path) != source["sha256"]:
            raise RuntimeError(f"Missing/changed source required for PyAV notices: {source['name']}")
        with tarfile.open(archive_path, "r:*") as archive:
            member = archive.getmember(member_name)
            stream = archive.extractfile(member)
            if stream is None or member.size == 0:
                raise RuntimeError(f"Missing license text in {source['name']}: {member_name}")
            (destination / output_name).write_bytes(stream.read())
    (destination / "managed-source-manifest.json").write_bytes(MANIFEST.read_bytes())


def decode_smoke(target: str, fixture: Path) -> dict:
    """Decode one H.264 frame through the installed PyAV/managed FFmpeg shared libraries."""
    record = verify_installed(target)
    prefix = WORK / "pyav" / target / "prefix"
    dll_directory = os.add_dll_directory(str(prefix / "bin")) if target == "windows-x64" else None
    if target != "windows-x64":
        library_path = str(prefix / "lib")
        os.environ["DYLD_LIBRARY_PATH"] = os.pathsep.join(
            part for part in (library_path, os.environ.get("DYLD_LIBRARY_PATH", "")) if part
        )
    try:
        import av

        if av.__version__ != "15.1.0":
            raise RuntimeError(f"Wrong PyAV version after managed install: {av.__version__}")
        with av.open(str(fixture)) as container:
            stream = next((item for item in container.streams if item.type == "video"), None)
            if stream is None or stream.codec_context.name != "h264":
                raise RuntimeError("PyAV did not recognize the H.264 decoding smoke fixture")
            frame = next(container.decode(stream), None)
            if frame is None or frame.width != 160 or frame.height != 120:
                raise RuntimeError("PyAV failed to decode an H.264 frame using managed FFmpeg")
    finally:
        if dll_directory is not None:
            dll_directory.close()
    return {
        "pyav_version": av.__version__,
        "ffmpeg_library_versions": {name: list(version) for name, version in av.library_versions.items()},
        "decoded": {"codec": stream.codec_context.name, "width": frame.width, "height": frame.height},
        "wheel_sha256": record["pyav_wheel_sha256"],
    }


def build_and_install(target: str) -> dict:
    if target not in TARGETS:
        raise ValueError(f"Unsupported PyAV target: {target}")
    system, machines = TARGETS[target]
    if platform.system() != system or platform.machine().lower() not in machines:
        raise RuntimeError(f"Managed PyAV target {target} requires its native build host")
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Managed PyAV wheel build requires CPython 3.12")
    manifest = _manifest()
    target_dir = WORK / "pyav" / target
    shutil.rmtree(target_dir, ignore_errors=True)
    target_dir.mkdir(parents=True)
    env = os.environ.copy()
    if target == "windows-x64":
        env["PATH"] = f"C:\\msys64\\mingw64\\bin;{env.get('PATH', '')}"
        env["CC"], env["CXX"] = "gcc", "g++"
        _verify_windows_compiler(target_dir / "compiler-probe", env)
    downloads = WORK / "pyav-source-downloads"
    pyav_source = _fetch(manifest["pyav"], downloads)
    ffmpeg_source = _fetch(manifest["ffmpeg"], downloads)
    sources = [pyav_source, ffmpeg_source]
    extracted = target_dir / "source"
    _extract(downloads / pyav_source["name"], extracted / "pyav")
    build_dir = target_dir / "build"
    prefix = target_dir / "prefix"
    _build_shared_ffmpeg(target, downloads, build_dir, prefix)
    library_files = _library_files(prefix, target)

    source_tree = extracted / "pyav" / "av-15.1.0"
    wheels = target_dir / "wheelhouse"
    wheels.mkdir()
    command = _wheel_command(target, prefix, wheels)
    if target == "windows-x64":
        env["PATH"] = f"{prefix / 'bin'};C:\\msys64\\mingw64\\bin;{env.get('PATH', '')}"
    else:
        env["LDFLAGS"] = (
            env.get("LDFLAGS", "") + f" -Wl,-rpath,{prefix / 'lib'}"
        ).strip()
        env["DYLD_LIBRARY_PATH"] = str(prefix / "lib") + os.pathsep + env.get("DYLD_LIBRARY_PATH", "")
    try:
        wheel_build = _checked(command, cwd=source_tree, env=env, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        (build_dir / "pyav-build.log").write_text(
            (error.stdout or "") + (error.stderr or ""), encoding="utf-8",
        )
        raise
    (build_dir / "pyav-build.log").write_text(
        wheel_build.stdout + wheel_build.stderr, encoding="utf-8",
    )
    wheel_candidates = list(wheels.glob("av-15.1.0-*.whl"))
    if len(wheel_candidates) != 1:
        raise RuntimeError(f"Expected one locally built PyAV wheel, found {len(wheel_candidates)}")
    wheel = wheel_candidates[0]
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if not any(name.startswith("av/") and name.endswith((".pyd", ".so")) for name in names):
            raise RuntimeError("Locally built PyAV wheel does not contain native extensions")
        if not any(name.endswith(".dist-info/METADATA")
                   and b"Name: av" in archive.read(name)
                   and b"Version: 15.1.0" in archive.read(name) for name in names):
            raise RuntimeError("Locally built PyAV wheel metadata mismatch")
    _checked([
        sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps",
        "--no-index", "--no-cache-dir", str(wheel),
    ], env=env)

    sources = [
        {key: source[key] for key in ("name", "url", "sha256", "license")}
        for source in (pyav_source, ffmpeg_source)
    ]
    configure = (build_dir / "ffmpeg-configure-args.txt").read_text(encoding="utf-8")
    if any(flag in configure for flag in ("--enable-gpl", "--enable-nonfree", "--enable-version3")):
        raise RuntimeError("Managed FFmpeg configure log enables a GPL/nonfree licensing option")
    source_archive = target_dir / "source-archive"
    source_archive.mkdir()
    app_version = (PACKAGING / "VERSION").read_text(encoding="utf-8").strip()
    archive_path = source_archive / f"TranslateService-{app_version}-{target}-pyav-source.tar.gz"
    record = {
        "schema_version": 1,
        "target": target,
        "app_version": app_version,
        "pyav_version": manifest["pyav"]["version"],
        "pyav_license": manifest["pyav"]["license"],
        "ffmpeg_version": manifest["ffmpeg"]["version"],
        "ffmpeg_license": LICENSE,
        "nonfree": False,
        "manifest_sha256": _sha256(MANIFEST),
        "sources": sources,
        "configure_args": configure,
        "shared_library_sha256": {path.name: _sha256(path) for path in library_files},
        "pyav_wheel_sha256": _sha256(wheel),
        "wheel": wheel.name,
        "source_archive": archive_path.name,
        "platform": platform.platform(),
    }
    record_path = target_dir / "build-info.json"
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    notices = WORK / "notices" / "pyav"
    shutil.rmtree(notices, ignore_errors=True)
    copy_notices(notices)
    with tarfile.open(archive_path, "w:gz", compresslevel=6) as archive:
        for name in RECIPE_FILES:
            archive.add(PACKAGING / name, arcname=name)
        for source in sources:
            archive.add(downloads / source["name"], arcname=f"sources/{source['name']}")
        archive.add(record_path, arcname="build-info.json")
        for name in ("build.log", "ffmpeg-configure-args.txt", "ffmpeg-configure.log",
                     "ffmpeg-build.log", "pyav-build.log"):
            path = build_dir / name
            if path.is_file():
                archive.add(path, arcname=f"build/{name}")
        for name in (
            "PyAV-LICENSE.txt", "FFmpeg-COPYING.LGPLv2.1.txt", "managed-source-manifest.json",
        ):
            archive.add(notices / name, arcname=f"notices/{name}")
    record["source_archive_sha256"] = _sha256(archive_path)
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    verify_installed(target)
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build-install", "verify"))
    parser.add_argument("--target", choices=tuple(TARGETS), required=True)
    args = parser.parse_args()
    record = build_and_install(args.target) if args.command == "build-install" else verify_installed(args.target)
    print(json.dumps(record, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
