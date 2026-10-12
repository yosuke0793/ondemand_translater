"""Native packaging orchestration; all generated files stay under packaging/."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
WORK = PACKAGING / ".work"
ARTIFACTS = PACKAGING / "artifacts"
sys.path.insert(0, str(ROOT / "source" / "worker"))
sys.path.insert(0, str(PACKAGING))
from media_license import validate_ffmpeg_license
from piper_model import (
    DEFAULT_VOICE, MODEL_MANIFEST, MODEL_STAGE, load_model_manifest,
    model_metadata, stage_model, supported_voices,
)
from piper_source import application_source_files, copy_piper_notices, stage_piper_source


def version(expected_tag: str | None = None) -> str:
    value = (PACKAGING / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value):
        raise ValueError("packaging/VERSION must contain a numeric MAJOR.MINOR.PATCH")
    if any(int(part) > 65535 for part in value.split(".")):
        raise ValueError("Windows version components must be <= 65535")
    if expected_tag is not None and expected_tag != f"v{value}":
        raise ValueError(f"Tag {expected_tag!r} does not match v{value}")
    return value


def native_target() -> str:
    system = {"Windows": "windows", "Darwin": "macos"}.get(platform.system())
    arch = {"AMD64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(
        platform.machine()
    )
    target = f"{system}-{arch}"
    if target not in ("windows-x64", "macos-x64", "macos-arm64"):
        raise RuntimeError(f"Unsupported native build platform: {platform.system()} {platform.machine()}")
    return target


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("+", " ".join(map(str, command)), flush=True)
    try:
        return subprocess.run(list(map(str, command)), check=True, **kwargs)
    except subprocess.CalledProcessError as exc:
        for label, output in (("stdout", exc.stdout), ("stderr", exc.stderr)):
            if output:
                print(f"Failed command {label}:\n{output}", file=sys.stderr, flush=True)
        raise


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        return digest.hexdigest()


def verify_source_hygiene() -> None:
    """Reject accidentally tracked user/runtime data without requiring Git in source archives."""
    inventory = application_source_files()
    unsafe = [
        path for path in inventory
        if path.startswith("source/worker/data/") or path == "source/worker/data"
    ]
    if unsafe:
        raise RuntimeError(f"Runtime data must not enter the corresponding-source inventory: {unsafe}")
    if not (ROOT / ".git").exists():
        return
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--cached", "-z", "--", "source/worker/data"],
        check=True, capture_output=True,
    )
    tracked = [entry.decode("utf-8", errors="replace")
               for entry in result.stdout.split(b"\0") if entry]
    if tracked:
        raise RuntimeError(
            "Tracked user/runtime data under source/worker/data is forbidden: "
            + ", ".join(tracked)
        )


def source_provenance() -> tuple[str, dict]:
    """Return a shared revision identity for a Git checkout or verified source archive."""
    explicit = os.environ.get("TRANSLATESERVICE_SOURCE_REVISION")
    if explicit:
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", explicit):
            raise RuntimeError("TRANSLATESERVICE_SOURCE_REVISION must be a Git-style hex revision")
        return explicit.lower(), {"kind": "explicit-revision", "value": explicit.lower()}
    if (ROOT / ".git").exists():
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD"],
            check=True, capture_output=True, text=True,
        )
        revision = result.stdout.strip()
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", revision):
            raise RuntimeError(f"Git returned an invalid source revision: {revision!r}")
        return revision.lower(), {"kind": "git-commit", "value": revision.lower()}
    candidates = (
        WORK / "piper-source" / "source-manifest.json",
        ROOT.parent / "source-manifest.json",
    )
    manifest_path = next((path for path in candidates if path.is_file()), None)
    if manifest_path is None:
        raise RuntimeError(
            "A Git checkout or corresponding-source source-manifest.json is required "
            "to record build provenance"
        )
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    expected_inventory = {relative: sha256(path) for relative, path in application_source_files().items()}
    if manifest.get("application_files") != expected_inventory:
        raise RuntimeError("Source-manifest application inventory does not match this source tree")
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    revision = f"source-manifest-sha256:{digest}"
    return revision, {"kind": "source-manifest-sha256", "value": digest}


def stage_ffmpeg(target: str) -> dict:
    from ffmpeg_source import stage_ffmpeg as build_media

    return build_media(target, version())


def generate_metadata(target: str) -> None:
    value = version()
    generated = WORK / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    (generated / "bundle_version.py").write_text(
        f'__version__ = VERSION = "{value}"\n', encoding="utf-8"
    )
    (generated / "version.json").write_text(
        json.dumps({"version": value, "target": target}) + "\n", encoding="utf-8"
    )
    parts = tuple(int(part) for part in value.split(".")) + (0,)
    (generated / "windows-version.txt").write_text(
        "VSVersionInfo(\n"
        f" ffi=FixedFileInfo(filevers={parts!r}, prodvers={parts!r}, "
        "mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0,0)),\n"
        " kids=[StringFileInfo([StringTable('040904B0', [\n"
        " StringStruct('CompanyName', 'Yosuke YAMANAKA'),\n"
        " StringStruct('ProductName', 'TranslateService'),\n"
        f" StringStruct('FileVersion', '{value}'),\n"
        f" StringStruct('ProductVersion', '{value}')])]),\n"
        " VarFileInfo([VarStruct('Translation', [1033, 1200])])])\n",
        encoding="utf-8",
    )
    notices = WORK / "notices"
    notices.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "LICENSE", notices / "TranslateService-LICENSE.txt")
    shutil.copy2(PACKAGING / "THIRD_PARTY_NOTICES.txt", notices / "THIRD_PARTY_NOTICES.txt")
    shutil.copy2(PACKAGING / "locks" / f"{target}.txt", notices / "dependency-lock.txt")
    shutil.copy2(MODEL_MANIFEST, notices / "piper_model.json")
    copy_piper_notices(notices / "piper")
    from pyav_source import copy_notices as copy_pyav_notices

    copy_pyav_notices(notices / "pyav")
    (notices / "TranslateService-BUNDLED-LICENSE.txt").write_text(
        "TranslateService Piper-enabled frozen distribution\n"
        "The combined distribution is provided under GPL-3.0-or-later.\n"
        "Original MIT notices for application components are retained below.\n"
        "See THIRD_PARTY_NOTICES.txt and the matching Piper, FFmpeg and PyAV source archives.\n\n"
        + (notices / "piper" / "COPYING").read_text(encoding="utf-8")
        + "\n\nOriginal application component license:\n"
        + (ROOT / "LICENSE").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    distributions = []
    for distribution in sorted(importlib.metadata.distributions(), key=lambda item: item.name.lower()):
        distributions.append({"name": distribution.name, "version": distribution.version,
                              "license": distribution.metadata.get("License-Expression")
                              or distribution.metadata.get("License", "See package metadata")})
        for file in distribution.files or []:
            # Wheel-provided license texts only, never installed package source or user data.
            if ".." not in file.parts and any(part.lower().startswith(("license", "copying", "notice", "authors"))
                   for part in file.parts) and file.suffix.lower() not in (".py", ".pyc"):
                source = Path(distribution.locate_file(file))
                if source.is_file():
                    destination = notices / "python" / distribution.name / Path(*file.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
    (notices / "python-distributions.json").write_text(
        json.dumps(distributions, indent=2) + "\n", encoding="utf-8"
    )
    copy_supplemental_notices(notices)


def verify_supplemental_notices(notices: Path) -> None:
    manifest_path = PACKAGING / "licenses" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (notices / "supplemental-license-manifest.json").read_bytes() != manifest_path.read_bytes():
        raise RuntimeError("Supplemental dependency license manifest mismatch")
    installed = {item["name"].lower().replace("_", "-"): item["version"]
                 for item in json.loads((notices / "python-distributions.json").read_text(encoding="utf-8"))}
    for name, entry in manifest.items():
        if installed.get(name) != entry["version"]:
            raise RuntimeError(f"Supplemental license version mismatch: {name}")
        for filename, url, digest in entry["files"]:
            if Path(filename).name != filename or not filename:
                raise RuntimeError(f"Unsafe supplemental license filename: {filename}")
            path = notices / "python" / name / filename
            if not path.is_file() or sha256(path) != digest:
                raise RuntimeError(f"Missing or changed supplemental license: {name}/{filename} ({url})")


def copy_supplemental_notices(notices: Path) -> None:
    root = PACKAGING / "licenses"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for name, entry in manifest.items():
        for filename, url, digest in entry["files"]:
            if Path(filename).name != filename or not filename:
                raise RuntimeError(f"Unsafe supplemental license filename: {filename}")
            source = root / filename
            if not source.is_file() or sha256(source) != digest:
                raise RuntimeError(f"Missing or changed pinned license: {filename} ({url})")
            destination = notices / "python" / name / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    shutil.copy2(root / "manifest.json", notices / "supplemental-license-manifest.json")
    verify_supplemental_notices(notices)


def executable_dir(bundle: Path) -> Path:
    return bundle / "Contents" / "MacOS" if bundle.suffix == ".app" else bundle


def verify_media_tools(binary_dir: Path, target: str, test_dir: Path, environment: dict) -> None:
    suffix = ".exe" if target.startswith("windows") else ""
    ffmpeg = str(binary_dir / f"ffmpeg{suffix}")
    filters = run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    for name in ("subtitles",):
        if not re.search(rf"\s{re.escape(name)}\s", filters):
            raise RuntimeError(f"FFmpeg missing required filter: {name}")
    fixture = test_dir / "synthetic.mp4"
    # No TTS service, credentials, downloaded model or user media is needed.
    run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=blue:s=160x120:r=10",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
         "-t", "0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-movflags", "+faststart", str(fixture)],
        cwd=test_dir, env=environment, capture_output=True, text=True, timeout=60)
    probe = run([str(binary_dir / f"ffprobe{suffix}"), "-v", "error", "-show_streams",
                 "-of", "json", str(fixture)],
                cwd=test_dir, env=environment, capture_output=True, text=True, timeout=60)
    codecs = {stream["codec_name"] for stream in json.loads(probe.stdout)["streams"]}
    if not {"h264", "aac"}.issubset(codecs):
        raise RuntimeError(f"Unexpected synthetic fixture codecs: {codecs}")
    fixture.unlink()


def verify_no_private_data(bundle: Path) -> None:
    forbidden = {
        ".env", ".venv", "jobs.db", "jobs.db-wal", "jobs.db-shm",
        "settings.json", "engineering_terms_custom.csv", "uploads", "jobs",
        "logs", "queue", "processing",
    }
    for path in bundle.rglob("*"):
        if path.name in forbidden:
            raise RuntimeError(f"Forbidden runtime/private file in bundle: {path.relative_to(bundle)}")


def verify_bundle(bundle: Path, target: str) -> list[dict]:
    bundle = bundle.resolve()
    verify_no_private_data(bundle)
    manifest = load_model_manifest()
    expected_models = [manifest["voice"], *manifest.get("additional_voices", [])]
    model_directories = sorted({path.resolve() for path in bundle.rglob("piper-model") if path.is_dir()})
    if len(model_directories) != 1:
        raise RuntimeError(f"Expected one bundled offline Piper model directory, found {len(model_directories)}")
    model_directory = model_directories[0]
    for asset in (asset for model in expected_models for asset in model["files"]):
        model_asset = model_directory / asset["name"]
        if not model_asset.is_file() or sha256(model_asset) != asset["sha256"]:
            raise RuntimeError(f"Bundled Piper model asset is missing or has the wrong hash: {asset['name']}")
    binary_dir = executable_dir(bundle)
    suffix = ".exe" if target.startswith("windows") else ""
    for name in ("TranslateService", "TranslateWorker", "ffmpeg", "ffprobe"):
        if not (binary_dir / f"{name}{suffix}").is_file():
            raise RuntimeError(f"Missing adjacent executable: {name}")
    notice_roots = {path.parent.resolve() for path in bundle.rglob("python-distributions.json")
                    if path.parent.name == "third-party-notices"}
    if len(notice_roots) != 1:
        raise RuntimeError("Expected one bundled dependency notice directory")
    verify_supplemental_notices(notice_roots.pop())
    # Run outside the checkout and remove interpreter/tool overrides.
    test_dir = WORK / "smoke" / target
    test_dir.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    for key in ("PYTHONHOME", "PYTHONPATH", "TRANSLATE_PYTHON", "TRANSLATE_WORKER_PYTHON", "DISPLAY"):
        environment.pop(key, None)
    system_path = str(Path(environment.get("SystemRoot", "C:/Windows")) / "System32") \
        if target.startswith("windows") else "/usr/bin:/bin"
    environment.update(
        PATH=str(binary_dir) + os.pathsep + system_path,
        PYINSTALLER_RESET_ENVIRONMENT="1",
        XDG_CACHE_HOME=str(test_dir / "cache"),
        TMPDIR=str(test_dir), TMP=str(test_dir), TEMP=str(test_dir),
    )
    reports = []
    for name in ("ffmpeg", "ffprobe"):
        binary = str(binary_dir / f"{name}{suffix}")
        config = run([binary, "-version"], cwd=test_dir, env=environment, capture_output=True, text=True)
        license_result = run([binary, "-L"], cwd=test_dir, env=environment, capture_output=True, text=True)
        validate_ffmpeg_license(
            config.stdout + config.stderr, license_result.stdout + license_result.stderr,
        )
    for name, role in (("TranslateService", "gui"), ("TranslateWorker", "worker")):
        report = test_dir / f"{role}.json"
        report.unlink(missing_ok=True)
        try:
            run([str(binary_dir / f"{name}{suffix}"), "--bundle-self-test", str(report)],
                cwd=test_dir, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=180)
        except subprocess.SubprocessError:
            if report.is_file():
                print(f"Failed {role} bundle self-test report:\n{report.read_text(encoding='utf-8')}",
                      file=sys.stderr, flush=True)
            else:
                print(f"Failed {role} bundle self-test produced no report: {report}",
                      file=sys.stderr, flush=True)
            raise
        payload = json.loads(report.read_text(encoding="utf-8"))
        if (payload.get("status"), payload.get("role"), payload.get("version")) != ("ok", role, version()):
            raise RuntimeError(f"Invalid frozen self-test report: {payload}")
        if set(payload.get("tools", {})) != {"ffmpeg", "ffprobe"}:
            raise RuntimeError(f"Missing frozen tool results: {payload}")
        if role == "worker":
            piper = payload.get("piper", {})
            if (
                piper.get("voice") != DEFAULT_VOICE
                or not isinstance(piper.get("wav_duration_seconds"), (float, int))
                or isinstance(piper["wav_duration_seconds"], bool)
                or not 0 < piper["wav_duration_seconds"] < float("inf")
            ):
                raise RuntimeError(f"Frozen Piper offline synthesis failed: {payload}")
            voices = piper.get("voices", {})
            if not isinstance(voices, dict) or set(voices) != set(supported_voices()):
                raise RuntimeError(f"Frozen Piper offline voice results are incomplete: {payload}")
            for voice, result in voices.items():
                if (not isinstance(result, dict) or result.get("voice") != voice
                        or not isinstance(result.get("wav_duration_seconds"), (float, int))
                        or isinstance(result["wav_duration_seconds"], bool)
                        or not 0 < result["wav_duration_seconds"] < float("inf")):
                    raise RuntimeError(f"Frozen Piper offline synthesis failed for {voice}: {payload}")
        reports.append(payload)
    worker_help = run([str(binary_dir / f"TranslateWorker{suffix}"), "--help"],
                      cwd=test_dir, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=60)
    if "--input" not in worker_help.stdout or "--outdir" not in worker_help.stdout:
        raise RuntimeError("Frozen worker did not expose the lecture_poc CLI")
    verify_media_tools(binary_dir, target, test_dir, environment)
    verify_managed_pyav(bundle, target, test_dir, environment)
    return reports


def verify_managed_pyav(bundle: Path, target: str, test_dir: Path, environment: dict) -> dict:
    """Check PyAV provenance, bundled shared-library closure, and a real H.264 decode."""
    from pyav_source import FFMPEG_LIBRARIES, decode_smoke, verify_installed

    record = verify_installed(target)
    paths = [path for path in bundle.rglob("*") if path.is_file()]
    if not any(
        path.suffix.lower() in (".pyd", ".so")
        and ("av" in path.parts or path.name.startswith("_core"))
        for path in paths
    ):
        raise RuntimeError("Frozen bundle is missing PyAV's compiled extension")
    missing = [
        library for library in FFMPEG_LIBRARIES
        if not any(
            library in path.name.lower()
            and (path.suffix.lower() in (".dylib", ".dll") or ".dylib" in path.name.lower())
            for path in paths
        )
    ]
    if missing:
        raise RuntimeError(f"Frozen bundle is missing managed FFmpeg shared libraries: {missing}")
    for name in ("PyAV-LICENSE.txt", "FFmpeg-COPYING.LGPLv2.1.txt", "managed-source-manifest.json"):
        if not (WORK / "notices" / "pyav" / name).is_file():
            raise RuntimeError(f"Missing managed PyAV/FFmpeg notice: {name}")
    fixture = test_dir / "pyav-managed-decode-smoke.mp4"
    suffix = ".exe" if target.startswith("windows") else ""
    run([
        str(executable_dir(bundle) / f"ffmpeg{suffix}"), "-nostdin", "-hide_banner",
        "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        "color=c=blue:s=160x120:r=10:d=0.5", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", str(fixture),
    ], cwd=test_dir, env=environment, capture_output=True, text=True, timeout=60)
    try:
        result = decode_smoke(target, fixture)
        frozen_environment = dict(environment)
        for key in ("DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH", "LD_LIBRARY_PATH"):
            frozen_environment.pop(key, None)
        frozen_report = test_dir / "pyav-frozen-decode.json"
        frozen_report.unlink(missing_ok=True)
        source_libdir = WORK / "pyav" / target / "prefix" / "lib"
        hidden_libdir = source_libdir.with_name("lib.frozen-smoke-unavailable")
        hide_source_libraries = target.startswith("macos")
        if hide_source_libraries:
            if hidden_libdir.exists():
                raise RuntimeError(f"Unexpected prior frozen-smoke directory: {hidden_libdir}")
            source_libdir.rename(hidden_libdir)
        try:
            run([
                str(executable_dir(bundle) / f"TranslateWorker{suffix}"),
                "--pyav-decode-self-test", str(fixture), str(frozen_report),
            ], cwd=test_dir, env=frozen_environment, capture_output=True, text=True, timeout=60)
        finally:
            if hide_source_libraries:
                hidden_libdir.rename(source_libdir)
        frozen = json.loads(frozen_report.read_text(encoding="utf-8"))
        if frozen != {"pyav_version": "15.1.0",
                      "decoded": {"codec": "h264", "width": 160, "height": 120}}:
            raise RuntimeError("Frozen PyAV decode did not use the expected managed build")
        result["frozen_decode"] = frozen
    finally:
        fixture.unlink(missing_ok=True)
    report_path = WORK / "pyav" / target / "decode-smoke.json"
    report_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if record.get("pyav_version") not in (None, "15.1.0"):
        raise RuntimeError("Managed PyAV source build version changed")
    return result


def create_zip(bundle: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, str(path.relative_to(bundle.parent)))


def create_dmg(bundle: Path, destination: Path, target: str) -> None:
    image_root = WORK / "dmg-root"
    shutil.rmtree(image_root, ignore_errors=True)
    image_root.mkdir()
    run(["ditto", str(bundle), str(image_root / bundle.name)])
    (image_root / "Applications").symlink_to("/Applications")
    command = ["hdiutil", "create", "-volname", f"TranslateService {version()}",
               "-srcfolder", str(image_root), "-ov", "-format", "UDZO", str(destination)]
    for attempt in range(3):
        try:
            result = run(command, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            diagnostics = f"{exc.stdout or ''}\n{exc.stderr or ''}"
            if "resource busy" not in diagnostics.lower() or attempt == 2:
                raise
            delay = 5 * (attempt + 1)
            print(f"hdiutil create reported Resource busy; retrying in {delay}s "
                  f"(attempt {attempt + 2}/3).", file=sys.stderr, flush=True)
            time.sleep(delay)
        else:
            if result.stdout:
                print(result.stdout, end="", flush=True)
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr, flush=True)
            break
    mount = WORK / "dmg-mount"
    mount.mkdir(exist_ok=True)
    run(["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mount), str(destination)])
    try:
        verify_bundle(mount / bundle.name, target)
        if not (mount / "Applications").is_symlink():
            raise RuntimeError("DMG is missing the /Applications link")
    finally:
        run(["hdiutil", "detach", str(mount)])


def build(target: str) -> None:
    verify_source_hygiene()
    if sys.version_info[:2] != (3, 12) or sys.prefix == sys.base_prefix:
        raise RuntimeError("Build requires the dedicated CPython 3.12 virtual environment")
    expected = PACKAGING / ".venv-build"
    if Path(sys.prefix).resolve() != expected.resolve():
        raise RuntimeError(f"Use the clean build environment at {expected}")
    for name in ("standalone_app.py", "bundled_worker.py", "app_runtime.py", "credential_store.py"):
        if not (ROOT / "source" / "worker" / name).is_file():
            raise RuntimeError(f"Missing source entrypoint/runtime: {name}")
    from pyav_source import verify_installed as verify_pyav_install

    pyav_info = verify_pyav_install(target)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    # Remove stale output before any native step can fail.
    for path in ARTIFACTS.iterdir():
        if path.is_file():
            path.unlink()
    shutil.rmtree(WORK / "notices", ignore_errors=True)
    media_info = stage_ffmpeg(target)
    stage_model(MODEL_STAGE)
    model_manifest = load_model_manifest()
    piper_source = stage_piper_source()
    piper_source_name = f"TranslateService-{version()}-{target}-piper-source.tar.gz"
    pyav_source_name = f"TranslateService-{version()}-{target}-pyav-source.tar.gz"
    pyav_archive = WORK / "pyav" / target / "source-archive" / pyav_source_name
    if not pyav_archive.is_file() or sha256(pyav_archive) != pyav_info.get("source_archive_sha256"):
        raise RuntimeError("Missing or changed managed PyAV/FFmpeg corresponding-source archive")
    generate_metadata(target)
    source_archive = Path(media_info["source_archive"])
    source_name = f"TranslateService-{version()}-{target}-ffmpeg-source.tar.gz"
    if source_archive.name != source_name or not source_archive.is_file():
        raise RuntimeError("Missing corresponding FFmpeg source archive")
    if sha256(source_archive) != media_info["source_sha256"]:
        raise RuntimeError("Corresponding FFmpeg source archive hash mismatch")
    if (media_info["license"], media_info["nonfree"]) != ("GPL-3.0-or-later", False):
        raise RuntimeError("FFmpeg source build license policy mismatch")
    shutil.copy2(source_archive, ARTIFACTS / source_name)
    if not piper_source.is_file():
        raise RuntimeError("Missing Piper/eSpeak-NG corresponding source archive")
    piper_source_sha256 = sha256(piper_source)
    shutil.copy2(piper_source, ARTIFACTS / piper_source_name)
    shutil.copy2(pyav_archive, ARTIFACTS / pyav_source_name)
    pyav_info["source_archive"] = pyav_source_name
    pyav_info["source_archive_sha256"] = sha256(ARTIFACTS / pyav_source_name)
    build_environment = os.environ.copy()
    pyav_prefix = WORK / "pyav" / target / "prefix"
    if target == "windows-x64":
        build_environment["PATH"] = (
            str(pyav_prefix / "bin") + os.pathsep + build_environment.get("PATH", "")
        )
    else:
        build_environment["DYLD_LIBRARY_PATH"] = os.pathsep.join(
            part for part in (
                str(pyav_prefix / "lib"), build_environment.get("DYLD_LIBRARY_PATH", ""),
            ) if part
        )
    run([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
         "--workpath", str(WORK / "pyinstaller"), "--distpath", str(WORK / "dist"),
         str(PACKAGING / "TranslateService.spec")], cwd=ROOT, env=build_environment)
    bundle = WORK / "dist" / ("TranslateService.app" if target.startswith("macos") else "TranslateService")
    binary_dir = executable_dir(bundle)
    for binary in (WORK / "ffmpeg" / target).iterdir():
        shutil.copy2(binary, binary_dir / binary.name)
    if target.startswith("macos"):
        run(["codesign", "--force", "--deep", "--sign", "-", str(bundle)])
        run(["codesign", "--verify", "--deep", "--strict", str(bundle)])
    reports = verify_bundle(bundle, target)
    prefix = f"TranslateService-{version()}-{target}"
    if target.startswith("windows"):
        portable = ARTIFACTS / f"{prefix}-portable.zip"
        create_zip(bundle, portable)
        extracted = WORK / "portable-smoke"
        shutil.rmtree(extracted, ignore_errors=True)
        with zipfile.ZipFile(portable) as archive:
            archive.extractall(extracted)
        verify_bundle(extracted / bundle.name, target)
        compiler = os.environ.get("ISCC") or shutil.which("ISCC") or \
            r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
        run([compiler, f"/DAppVersion={version()}", f"/DBundleDir={bundle}",
             f"/DOutputDir={ARTIFACTS}", str(PACKAGING / "installer.iss")])
    else:
        create_dmg(bundle, ARTIFACTS / f"{prefix}.dmg", target)
    revision, provenance = source_provenance()
    (ARTIFACTS / f"{prefix}-build-info.json").write_text(
        json.dumps({"version": version(), "target": target, "commit": revision,
                    "source_provenance": provenance,
                    "python": sys.version, "self_tests": reports,
                    "dependency_lock_sha256": sha256(PACKAGING / "locks" / f"{target}.txt"),
                    "ffmpeg_manifest_sha256": sha256(PACKAGING / "ffmpeg.json"),
                    "pyav": {**{key: pyav_info[key] for key in (
                        "pyav_version", "pyav_license", "ffmpeg_version", "ffmpeg_license",
                        "nonfree", "manifest_sha256", "sources", "configure_args",
                        "shared_library_sha256", "pyav_wheel_sha256", "source_archive",
                        "source_archive_sha256",
                    )}, "decode_smoke": json.loads(
                        (WORK / "pyav" / target / "decode-smoke.json").read_text(encoding="utf-8")
                    )},
                    "piper": {
                        "runtime": f'{model_manifest["runtime"]["distribution"]}=='
                        f'{model_manifest["runtime"]["version"]}',
                        "license": model_manifest["runtime"]["license"],
                        "source_archive": piper_source_name,
                        "source_sha256": piper_source_sha256,
                        "source_manifest_sha256": sha256(
                            WORK / "piper-source" / "source-manifest.json"
                        ),
                        "model": model_metadata(model_manifest["voice"]),
                        "additional_models": [
                            model_metadata(model)
                            for model in model_manifest.get("additional_voices", [])
                        ],
                    },
                    "ffmpeg": {**media_info, "source_archive": source_name},
                    "signed_for_distribution": False}, indent=2) + "\n", encoding="utf-8"
    )
    checksum_file = ARTIFACTS / f"{prefix}-SHA256SUMS.txt"
    checksum_file.write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in sorted(ARTIFACTS.iterdir()) if path.is_file()),
        encoding="utf-8",
    )
    print(f"Verified native artifacts: {ARTIFACTS}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "stage-ffmpeg", "build", "verify"))
    parser.add_argument("--expected-tag")
    parser.add_argument("--target", choices=("windows-x64", "macos-x64", "macos-arm64"))
    parser.add_argument("--bundle", type=Path)
    args = parser.parse_args()
    value = version(args.expected_tag)
    if args.command == "validate":
        verify_source_hygiene()
        print(value)
        return
    target = native_target()
    if args.target and args.target != target:
        raise RuntimeError(f"Native architecture mismatch: requested {args.target}, running {target}")
    WORK.mkdir(parents=True, exist_ok=True)
    if args.command == "stage-ffmpeg":
        stage_ffmpeg(target)
    elif args.command == "verify":
        if args.bundle is None:
            parser.error("verify requires --bundle")
        print(json.dumps(verify_bundle(args.bundle, target)))
    else:
        build(target)


if __name__ == "__main__":
    main()
