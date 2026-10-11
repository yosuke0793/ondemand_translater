"""Create a corresponding-source archive for the distributed Piper runtime."""

from __future__ import annotations

import json
import hashlib
import posixpath
import tarfile
import urllib.request
from pathlib import Path

from piper_model import MODEL_MANIFEST, load_model_manifest
from rebuild_piper_offline import sha256

PACKAGING = Path(__file__).resolve().parent
ROOT = PACKAGING.parent
WORK = PACKAGING / ".work"
REBUILD_HELPER = PACKAGING / "rebuild_piper_offline.py"


def application_source_files(root: Path = ROOT) -> dict[str, Path]:
    """Return the explicit corresponding-source allowlist for the frozen app."""
    root = Path(root).resolve()
    packaging_files = (
        "BUNDLED",
        "VERSION",
        "THIRD_PARTY_NOTICES.txt",
        "requirements.in",
        "bootstrap.in",
        "ffmpeg.json",
        "pyav.json",
        "piper_model.json",
        "piper_model_license.txt",
        "john_model_license.txt",
        "build.py",
        "release.py",
        "build-macos.sh",
        "build-windows.ps1",
        "installer.iss",
        "build-ffmpeg.sh",
        "ffmpeg_source.py",
        "pyav_source.py",
        "build-pyav-ffmpeg.sh",
        "piper_model.py",
        "piper_source.py",
        "rebuild_piper_offline.py",
        "TranslateService.spec",
        "locks/bootstrap.txt",
        "locks/macos-arm64.txt",
        "locks/macos-x64.txt",
        "locks/windows-x64.txt",
    )
    paths = [root / "LICENSE", root / "README.md"]
    paths.extend(sorted((root / "docs").glob("*.md")))
    paths.extend(sorted((root / "source" / "worker").glob("*.py")))
    paths.extend(sorted((root / "source" / "worker" / "lecture_poc").rglob("*.py")))
    paths.extend(sorted(
        path
        for path in (root / "source" / "worker" / "lecture_poc" / "prompts").rglob("*")
        if path.is_file()
    ))
    paths.append(root / "source" / "worker" / "requirements.txt")
    paths.extend(root / "packaging" / name for name in packaging_files)
    files = {}
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(f"Missing or unsafe corresponding application source: {path}")
        relative = path.relative_to(root).as_posix()
        files[relative] = path
    return dict(sorted(files.items()))


def _fetch(url: str, path: Path, expected_hash: str) -> None:
    if path.is_file() and sha256(path) == expected_hash:
        return
    path.unlink(missing_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as output:
            while block := response.read(1024 * 1024):
                digest.update(block)
                output.write(block)
        if digest.hexdigest() != expected_hash:
            raise ValueError(f"Piper corresponding source SHA-256 mismatch: {path.name}")
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)


def stage_piper_source() -> Path:
    manifest = load_model_manifest()
    runtime = manifest["runtime"]
    source_dir = WORK / "piper-source"
    source_dir.mkdir(parents=True, exist_ok=True)
    sources = [
        {
            "name": f'piper1-gpl-{runtime["source_revision"]}.tar.gz',
            "url": runtime["source_archive_url"],
            "sha256": runtime["source_archive_sha256"],
            "license": runtime["license"],
        },
        {
            "name": f'espeak-ng-{runtime["embedded_espeak_ng"]["source_revision"]}.tar.gz',
            "url": runtime["embedded_espeak_ng"]["source_archive_url"],
            "sha256": runtime["embedded_espeak_ng"]["source_archive_sha256"],
            "license": "GPL-3.0-or-later",
        },
    ]
    for source in sources:
        _fetch(source["url"], source_dir / source["name"], source["sha256"])
    application_files = application_source_files()
    application_hashes = {
        relative: sha256(path)
        for relative, path in application_files.items()
    }
    (source_dir / "source-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "distribution": runtime["distribution"],
                "version": runtime["version"],
                "license": runtime["license"],
                "sources": sources,
                "application_files": application_hashes,
                "recipe": {
                    "helper": "packaging/rebuild_piper_offline.py",
                    "preparation": (
                        "From the piper-source directory, run "
                        "python rebuild_piper_offline.py with --piper-archive and "
                        "--espeak-archive set to the exact nested archive names listed in sources, "
                        "--manifest piper_model.json, and --destination offline-rebuild. The helper "
                        "verifies both archive hashes, extracts the original trees, and applies only "
                        "the two exact local-source substitutions below."
                    ),
                    "piper_source_changes": [
                        (
                            "In the pinned Piper CMakeLists.txt, replace GIT_REPOSITORY/GIT_TAG "
                            "for ExternalProject_Add(espeak_ng_external) with SOURCE_DIR "
                            "${CMAKE_CURRENT_SOURCE_DIR}/espeak-ng-source, DOWNLOAD_COMMAND \"\", "
                            "and UPDATE_COMMAND \"\"."
                        ),
                        (
                            "Replace the UCD include argument based on "
                            "${ESPEAKNG_BUILD_DIR}/src/espeak_ng_external with "
                            "${CMAKE_CURRENT_SOURCE_DIR}/espeak-ng-source so the existing "
                            "CMAKE_C_FLAGS references the extracted local UCD headers."
                        ),
                    ],
                    "build": [
                        "Use CPython 3.12 with scikit-build, setuptools, wheel, CMake >=3.26,<4, Ninja >=1,<2, and a C/C++ compiler already installed; do not install build requirements from the network.",
                        (
                            "Preparation command: python rebuild_piper_offline.py "
                            f'--piper-archive {sources[0]["name"]} '
                            f'--espeak-archive {sources[1]["name"]} '
                            "--manifest piper_model.json --destination offline-rebuild"
                        ),
                        "From offline-rebuild/piper-src run: python -m pip wheel --no-build-isolation --no-deps --wheel-dir dist .",
                        "No source fetch is needed: the eSpeak-NG ExternalProject uses the extracted local SOURCE_DIR and empty DOWNLOAD_COMMAND/UPDATE_COMMAND.",
                    ],
                    "application_build": [
                        "The application/ directory contains the corresponding application source, platform build wrappers, PyInstaller spec, native dependency locks and FFmpeg/Piper recipes.",
                        "Use native CPython 3.12 and the platform C/C++ compiler; packaging/VERSION records the distributed application version.",
                        "macOS: from application/, run PYTHON312=/absolute/path/to/python3.12 bash packaging/build-macos.sh.",
                        "Windows: from application/, run packaging/build-windows.ps1 -Python312 C:\\absolute\\path\\to\\python.exe.",
                        "These full application wrappers acquire locked Python dependencies, pinned FFmpeg sources and the public-domain voice assets. The Piper/eSpeak preparation above is offline after build tools are installed.",
                        "For a locally rebuilt Piper engine, install its generated wheel into the dedicated packaging/.venv-build after the locked dependency installation and before packaging/build.py build.",
                        "The original root MIT copyright/license notices are retained. The Piper-enabled combined frozen distribution is GPL-3.0-or-later.",
                    ],
                    "build_requirements": [
                        "scikit-build",
                        "setuptools",
                        "wheel",
                        "CMake >=3.26,<4",
                        "Ninja >=1,<2",
                        "a C/C++ compiler",
                    ],
                    "native_dependency": (
                        "The package CMakeLists.txt statically links espeak-ng as "
                        "BUILD_SHARED_LIBS=OFF and copies the generated espeak-ng-data into "
                        "src/piper/espeak-ng-data before packaging."
                    ),
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    destination = WORK / "piper-source.tar.gz"
    destination.unlink(missing_ok=True)
    with tarfile.open(destination, "w:gz") as archive:
        root_files = (
            MODEL_MANIFEST,
            *(source_dir / item["name"] for item in sources),
            source_dir / "source-manifest.json",
            REBUILD_HELPER,
        )
        for path in root_files:
            _add_file(archive, path, f"piper-source/{path.name}")
        for relative, path in application_files.items():
            _add_file(archive, path, f"piper-source/application/{relative}")
    return destination


def _add_file(archive: tarfile.TarFile, path: Path, archive_name: str) -> None:
    info = archive.gettarinfo(str(path), arcname=archive_name)
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    with path.open("rb") as stream:
        archive.addfile(info, stream)


def copy_piper_notices(destination: Path) -> None:
    """Copy license texts from the exact staged source revisions into bundle notices."""
    manifest = load_model_manifest()
    runtime = manifest["runtime"]
    source_archives = (
        (
            WORK / "piper-source" / f'piper1-gpl-{runtime["source_revision"]}.tar.gz',
            "piper",
        ),
        (
            WORK / "piper-source" / f'espeak-ng-{runtime["embedded_espeak_ng"]["source_revision"]}.tar.gz',
            "espeak-ng",
        ),
    )
    for archive_path, component in source_archives:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                parts = member.name.split("/")
                if (
                    not member.isfile()
                    or any(part in ("", ".", "..") for part in parts)
                    or not posixpath.basename(member.name).lower().startswith(
                        ("copying", "license", "notice")
                    )
                ):
                    continue
                relative = Path(*parts[1:])
                if not relative.parts:
                    continue
                source = archive.extractfile(member)
                if source is None:
                    continue
                target = destination / (Path("espeak-ng") if component == "espeak-ng" else Path()) / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read())
    model_license = destination / "piper-model-license.txt"
    model_license.parent.mkdir(parents=True, exist_ok=True)
    model_license.write_bytes((PACKAGING / "piper_model_license.txt").read_bytes())
