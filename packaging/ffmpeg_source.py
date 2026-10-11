"""Reproducible, source-built FFmpeg staging for native release targets."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
WORK = PACKAGING / ".work"
MANIFEST = PACKAGING / "ffmpeg.json"
LICENSE = "GPL-3.0-or-later"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _checked(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("+", " ".join(map(str, command)), flush=True)
    try:
        return subprocess.run(command, check=True, **kwargs)
    except subprocess.CalledProcessError as error:
        for output in (error.stdout, error.stderr):
            if output:
                print(output, file=sys.stderr)
        raise


def _fetch_sources(manifest: dict, directory: Path) -> list[dict[str, str]]:
    directory.mkdir(parents=True, exist_ok=True)
    pinned = []
    for entry in manifest["sources"]:
        name, url, expected = entry["name"], entry["url"], entry["sha256"]
        if Path(name).name != name or not name:
            raise RuntimeError(f"Unsafe FFmpeg source filename in manifest: {name!r}")
        path = directory / name
        if not path.is_file() or _sha256(path) != expected:
            path.unlink(missing_ok=True)
            request = urllib.request.Request(url, headers={"User-Agent": "TranslateService source builder"})
            partial = path.with_suffix(path.suffix + ".partial")
            try:
                for attempt in range(3):
                    print(f"Downloading FFmpeg source {name} from {url} "
                          f"(attempt {attempt + 1}/3)", flush=True)
                    try:
                        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
                            shutil.copyfileobj(response, output)
                    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                        partial.unlink(missing_ok=True)
                        if isinstance(exc, urllib.error.HTTPError):
                            retryable = exc.code in (408, 429, 500, 502, 503, 504)
                            exc.close()
                        else:
                            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                            retryable = isinstance(reason, (socket.gaierror, TimeoutError, ConnectionError))
                        print(f"Download failed for {name} from {url}: {exc}",
                              file=sys.stderr, flush=True)
                        if not retryable or attempt == 2:
                            raise
                        delay = 5 * (attempt + 1)
                        print(f"Retrying source download in {delay}s.", file=sys.stderr, flush=True)
                        time.sleep(delay)
                    else:
                        actual = _sha256(partial)
                        if actual != expected:
                            raise RuntimeError(f"SHA-256 mismatch for FFmpeg source {name}: {actual}")
                        partial.replace(path)
                        break
            finally:
                partial.unlink(missing_ok=True)
        actual = _sha256(path)
        if actual != expected:
            path.unlink(missing_ok=True)
            raise RuntimeError(f"SHA-256 mismatch for FFmpeg source {name}: {actual}")
        pinned.append({"name": name, "url": url, "sha256": actual})
    return pinned


def _extract_sources(sources: list[dict[str, str]], downloads: Path, destination: Path) -> None:
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True, exist_ok=True)
    for source in sources:
        archive_path = downloads / source["name"]
        if _sha256(archive_path) != source["sha256"]:
            raise RuntimeError(f"Refusing to extract changed source archive: {source['name']}")
        with tarfile.open(archive_path, "r:*") as archive:
            for member in archive.getmembers():
                candidate = PurePosixPath(member.name)
                if candidate.is_absolute() or ".." in candidate.parts:
                    raise RuntimeError(f"Unsafe path in source archive {source['name']}: {member.name}")
                if member.issym() or member.islnk():
                    link = PurePosixPath(member.linkname)
                    if link.is_absolute() or ".." in link.parts:
                        raise RuntimeError(f"Unsafe link in source archive {source['name']}: {member.name}")
            archive.extractall(destination / source["name"].split(".tar", 1)[0], filter="data")


def _msys_path(path: Path) -> str:
    resolved = str(path.resolve())
    if len(resolved) > 2 and resolved[1:3] == ":\\":
        return f"/{resolved[0].lower()}/{resolved[3:].replace(chr(92), '/')}"
    return resolved


def _build(target: str, source_dir: Path, build_dir: Path, prefix: Path) -> None:
    recipe = PACKAGING / "build-ffmpeg.sh"
    env = os.environ.copy()
    env.update({
        "SOURCE_DIR": _msys_path(source_dir) if target == "windows-x64" else str(source_dir),
        "BUILD_DIR": _msys_path(build_dir) if target == "windows-x64" else str(build_dir),
        "PREFIX": _msys_path(prefix) if target == "windows-x64" else str(prefix),
        "TARGET": target,
        "BUILD_JOBS": str(max(2, min(os.cpu_count() or 2, 8))),
    })
    if target == "windows-x64":
        candidates = (
            Path("C:/msys64/usr/bin/bash.exe"),
            Path("C:/tools/msys64/usr/bin/bash.exe"),
        )
        bash = next((str(path) for path in candidates if path.is_file()), None)
        if not bash:
            bash = shutil.which("bash")
        if not bash:
            raise RuntimeError("MSYS2 bash was not found; install the MINGW64 toolchain before building")
        env.update({
            "PATH": "C:/msys64/mingw64/bin;C:/msys64/usr/bin;" + env.get("PATH", ""),
            "PKG_CONFIG": "pkg-config",
        })
        _checked([bash, _msys_path(recipe)], env=env, cwd=ROOT)
    else:
        _checked(["bash", str(recipe)], env=env, cwd=ROOT)


def _license(binary: Path) -> tuple[str, str]:
    version_output = _checked(
        [str(binary), "-hide_banner", "-version"], capture_output=True, text=True
    ).stdout
    license_output = _checked(
        [str(binary), "-hide_banner", "-L"], capture_output=True, text=True
    ).stdout
    sys.path.insert(0, str(ROOT / "source" / "worker"))
    from media_license import validate_ffmpeg_license

    detected = validate_ffmpeg_license(version_output, license_output)
    if detected != LICENSE:
        raise RuntimeError(f"Unexpected FFmpeg license classification: {detected}")
    if "--enable-nonfree" in version_output:
        raise RuntimeError("FFmpeg was configured with --enable-nonfree")
    if "--enable-gpl" not in version_output or "--enable-version3" not in version_output:
        raise RuntimeError("FFmpeg build configuration is missing GPLv3 flags")
    return version_output, license_output


def _verify_native_linkage(binary: Path, target: str, build_dir: Path) -> None:
    if target.startswith("macos"):
        output = _checked(["otool", "-L", str(binary)], capture_output=True, text=True).stdout
        disallowed = [
            line.strip().split(" (", 1)[0] for line in output.splitlines()[1:]
            if not line.strip().startswith(("/System/Library/", "/usr/lib/"))
        ]
        if disallowed:
            raise RuntimeError(f"FFmpeg links non-system macOS libraries: {disallowed}")
    else:
        objdump = shutil.which("x86_64-w64-mingw32-objdump") or shutil.which("objdump")
        if not objdump:
            raise RuntimeError("MINGW64 objdump is required to validate Windows DLL linkage")
        output = _checked([objdump, "-p", str(binary)], capture_output=True, text=True).stdout
        imports = [line.rsplit(" ", 1)[-1].lower() for line in output.splitlines()
                   if "DLL Name:" in line]
        system_dlls = {
            "advapi32.dll", "avicap32.dll", "bcrypt.dll", "comctl32.dll", "comdlg32.dll", "gdi32.dll",
            "kernel32.dll", "msvcrt.dll", "ole32.dll", "oleaut32.dll", "shell32.dll",
            "shlwapi.dll", "user32.dll", "version.dll", "winmm.dll", "ws2_32.dll",
            "d3d11.dll", "dxgi.dll", "dwrite.dll", "mf.dll", "mfplat.dll",
            "avrt.dll", "bcryptprimitives.dll", "d3dcompiler_47.dll", "imm32.dll",
            "mfreadwrite.dll", "ntdll.dll", "secur32.dll", "userenv.dll",
        }
        unexpected = sorted(
            dll for dll in set(imports)
            if dll not in system_dlls and not dll.startswith("api-ms-win-")
        )
        if unexpected:
            raise RuntimeError(f"FFmpeg has non-OS Windows DLL dependencies: {unexpected}")


def _smoke(binary_dir: Path, target: str, build_dir: Path) -> dict:
    suffix = ".exe" if target == "windows-x64" else ""
    ffmpeg = binary_dir / f"ffmpeg{suffix}"
    ffprobe = binary_dir / f"ffprobe{suffix}"
    version_output, license_output = _license(ffmpeg)
    _license(ffprobe)
    _verify_native_linkage(ffmpeg, target, build_dir)
    _verify_native_linkage(ffprobe, target, build_dir)
    encoders = _checked([str(ffmpeg), "-hide_banner", "-encoders"],
                        capture_output=True, text=True).stdout
    filters = _checked([str(ffmpeg), "-hide_banner", "-filters"],
                       capture_output=True, text=True).stdout
    for required in ("libx264", "libmp3lame", " aac "):
        if required not in encoders:
            raise RuntimeError(f"FFmpeg is missing required encoder: {required.strip()}")
    if " subtitles " not in filters:
        raise RuntimeError("FFmpeg is missing the libass subtitles filter")

    test_dir = build_dir / "smoke"
    test_dir.mkdir(parents=True, exist_ok=True)
    ass = test_dir / "fixture.ass"
    fixture = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 320\nPlayResY: 240\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,24,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
        "0,0,1,2,0,2,10,10,20,1\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:00.00,0:00:01.50,Default,,0,0,0,,Synthetic subtitle smoke test\n"
    )
    ass.write_text(fixture, encoding="utf-8")
    video = test_dir / f"subtitle-smoke{'.mp4'}"
    subtitle_path = ass.as_posix().replace(":", r"\:").replace("'", r"\'")
    subtitle_filter = f"subtitles='{subtitle_path}'"
    frame_hashes = []
    for vf in (None, subtitle_filter):
        command = [str(ffmpeg), "-hide_banner", "-loglevel", "error",
                   "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=24:d=1.5"]
        if vf:
            command.extend(["-vf", vf])
        command.extend(["-frames:v", "1", "-f", "framemd5", "-"])
        output = _checked(command, capture_output=True, text=True).stdout
        frames = [line for line in output.splitlines() if line and not line.startswith("#")]
        if len(frames) != 1:
            raise RuntimeError("FFmpeg did not produce exactly one subtitle verification frame")
        frame_hashes.append(frames[0].rsplit(",", 1)[-1].strip())
    if frame_hashes[0] == frame_hashes[1]:
        raise RuntimeError("FFmpeg subtitles filter did not render any text; check OS font support")
    _checked([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=24:d=1.5",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5",
        "-vf", subtitle_filter, "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(video),
    ], capture_output=True, text=True)
    mp3 = test_dir / "audio-smoke.mp3"
    _checked([
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "sine=frequency=880:duration=0.5",
        "-c:a", "libmp3lame", str(mp3),
    ], capture_output=True, text=True)
    probe = _checked([str(ffprobe), "-v", "error", "-show_entries",
                      "stream=codec_name:format=duration", "-of", "json", str(video)],
                     capture_output=True, text=True).stdout.strip()
    report = json.loads(probe)
    codecs = {stream["codec_name"] for stream in report["streams"]}
    duration = float(report["format"]["duration"])
    if (not video.is_file() or not mp3.is_file() or mp3.stat().st_size == 0
            or codecs != {"h264", "aac"} or abs(duration - 1.5) > 0.1):
        raise RuntimeError("FFmpeg synthetic encode/probe smoke test failed")
    return {
        "version_output": version_output.strip(),
        "license_output": license_output.strip(),
        "subtitle_encode_probe": probe,
        "ffmpeg_sha256": _sha256(ffmpeg),
        "ffprobe_sha256": _sha256(ffprobe),
    }


def stage_ffmpeg(target: str, app_version: str) -> dict:
    """Build FFmpeg and return verified bundle and corresponding source archive metadata."""
    if target not in {"windows-x64", "macos-arm64", "macos-x64"}:
        raise ValueError(f"Unsupported FFmpeg build target: {target}")
    host = platform.system(), platform.machine().lower()
    expected_hosts = {
        "windows-x64": ("Windows", {"amd64", "x86_64"}),
        "macos-arm64": ("Darwin", {"arm64", "aarch64"}),
        "macos-x64": ("Darwin", {"x86_64"}),
    }
    system, machines = expected_hosts[target]
    if host[0] != system or host[1] not in machines:
        raise RuntimeError(f"FFmpeg target {target} requires its native build host, got {host}")
    binary_dir = WORK / "ffmpeg" / target
    shutil.rmtree(binary_dir, ignore_errors=True)
    manifest_bytes = MANIFEST.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("license") != LICENSE or manifest.get("nonfree") is not False:
        raise RuntimeError("FFmpeg manifest must permit GPLv3 and prohibit nonfree components")
    if not manifest.get("sources"):
        raise RuntimeError("FFmpeg source manifest is empty")
    downloads = WORK / "ffmpeg-source-downloads"
    sources = _fetch_sources(manifest, downloads)
    target_work = WORK / "ffmpeg-source-build" / target
    shutil.rmtree(target_work, ignore_errors=True)
    source_tree = target_work / "source"
    _extract_sources(sources, downloads, source_tree)
    build_dir = target_work / "build"
    prefix = target_work / "prefix"
    _build(target, downloads, build_dir, prefix)

    binary_dir.mkdir(parents=True)
    suffix = ".exe" if target == "windows-x64" else ""
    for name in ("ffmpeg", "ffprobe"):
        source = prefix / "bin" / f"{name}{suffix}"
        if not source.is_file():
            raise RuntimeError(f"Source build did not produce {source.name}")
        shutil.copy2(source, binary_dir / source.name)
    smoke = _smoke(binary_dir, target, build_dir)

    source_root = WORK / "ffmpeg-source"
    source_root.mkdir(parents=True, exist_ok=True)
    archive_path = source_root / f"TranslateService-{app_version}-{target}-ffmpeg-source.tar.gz"
    archive_path.unlink(missing_ok=True)
    build_info = {
        "schema_version": 1,
        "target": target,
        "app_version": app_version,
        "license": LICENSE,
        "nonfree": False,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "sources": sources,
        "binaries": {name: smoke[f"{name}_sha256"] for name in ("ffmpeg", "ffprobe")},
        "ffmpeg_version": smoke["version_output"],
        "ffmpeg_license": smoke["license_output"],
        "configure_args": (build_dir / "ffmpeg-configure-args.txt").read_text(encoding="utf-8"),
        "subtitle_encode_probe": smoke["subtitle_encode_probe"],
        "platform": platform.platform(),
    }
    with tarfile.open(archive_path, "w:gz", compresslevel=6) as archive:
        archive.add(MANIFEST, arcname="ffmpeg.json")
        archive.add(PACKAGING / "build-ffmpeg.sh", arcname="build-ffmpeg.sh")
        archive.add(PACKAGING / "ffmpeg_source.py", arcname="ffmpeg_source.py")
        for source in sources:
            archive.add(downloads / source["name"], arcname=f"sources/{source['name']}")
        for name in ("ffmpeg-configure-args.txt", "ffmpeg-configure.log",
                     "ffmpeg-build.log", "build.log"):
            path = build_dir / name
            if path.is_file():
                archive.add(path, arcname=f"build/{name}")
        configure_log = build_dir / "ffmpeg" / "ffbuild" / "config.log"
        if configure_log.is_file():
            archive.add(configure_log, arcname="build/ffmpeg-config.log")
        info_path = source_root / f".{archive_path.name}.build-info.json"
        info_path.write_text(json.dumps(build_info, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        archive.add(info_path, arcname="build-info.json")
        info_path.unlink()
    source_archive_sha = _sha256(archive_path)

    notices = WORK / "notices" / "ffmpeg"
    shutil.rmtree(notices, ignore_errors=True)
    notices.mkdir(parents=True, exist_ok=True)
    shutil.copy2(MANIFEST, notices / "source-manifest.json")
    for source in sources:
        with tarfile.open(downloads / source["name"], "r:*") as archive:
            licenses = [member for member in archive.getmembers()
                        if member.isfile() and len(PurePosixPath(member.name).parts) <= 3
                        and (PurePosixPath(member.name).name.lower().startswith(
                            ("copying", "license", "licence", "notice"))
                            or PurePosixPath(member.name).name in ("FTL.TXT", "GPLv2.TXT"))]
            if not licenses:
                raise RuntimeError(f"Missing copyright/license notices: {source['name']}")
            for member in licenses:
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                data = extracted.read()
                if not data:
                    continue
                relative = PurePosixPath(*PurePosixPath(member.name).parts[1:])
                destination = notices / source["name"] / str(relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)

    return {
        "source_archive": str(archive_path.resolve()),
        "source_sha256": source_archive_sha,
        "license": LICENSE,
        "nonfree": False,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "sources": sources,
        "binaries": build_info["binaries"],
        "subtitle_encode_probe": smoke["subtitle_encode_probe"],
    }
