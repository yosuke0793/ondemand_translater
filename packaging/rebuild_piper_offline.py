"""Prepare the pinned Piper/eSpeak sources for a fully offline native rebuild."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from pathlib import Path, PurePosixPath


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def unpack_source(archive_path: Path, destination: Path, expected_hash: str) -> None:
    if sha256(archive_path) != expected_hash:
        raise ValueError(f"Source archive hash mismatch: {archive_path.name}")
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        roots = set()
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or not name.parts or any(part in (".", "..") for part in name.parts):
                raise ValueError(f"Unsafe path in source archive: {member.name}")
            roots.add(name.parts[0])
        if len(roots) != 1:
            raise ValueError(f"Unexpected source archive layout: {archive_path.name}")
        archive.extractall(destination, filter="data")
    extracted_root = destination / next(iter(roots))
    if not extracted_root.is_dir():
        raise ValueError(f"Missing source directory in archive: {archive_path.name}")
    for child in extracted_root.iterdir():
        target = destination / child.name
        if target.exists() or target.is_symlink():
            raise ValueError(f"Unexpected existing extraction path: {target.name}")
        child.rename(target)
    extracted_root.rmdir()


def prepare(piper_archive: Path, espeak_archive: Path, manifest_path: Path, destination: Path) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    runtime = manifest["runtime"]
    if sha256(piper_archive) != runtime["source_archive_sha256"]:
        raise ValueError("Pinned Piper source archive SHA-256 mismatch.")
    espeak = runtime["embedded_espeak_ng"]
    if sha256(espeak_archive) != espeak["source_archive_sha256"]:
        raise ValueError("Pinned eSpeak-NG source archive SHA-256 mismatch.")

    source_root = destination / "piper-src"
    if source_root.exists():
        raise FileExistsError(f"Refusing to replace existing source tree: {source_root}")
    source_root.mkdir(parents=True)
    unpack_source(piper_archive, source_root, runtime["source_archive_sha256"])

    espeak_root = source_root / "espeak-ng-source"
    espeak_root.mkdir()
    unpack_source(espeak_archive, espeak_root, espeak["source_archive_sha256"])

    cmake_path = source_root / "CMakeLists.txt"
    cmake = cmake_path.read_text(encoding="utf-8")
    upstream_git_checkout = (
        "    GIT_REPOSITORY https://github.com/espeak-ng/espeak-ng.git\n"
        f'    GIT_TAG {espeak["source_revision"]}  # 2025-Mar-22\n'
    )
    offline_source = (
        "    SOURCE_DIR ${CMAKE_CURRENT_SOURCE_DIR}/espeak-ng-source\n"
        '    DOWNLOAD_COMMAND ""\n'
        '    UPDATE_COMMAND ""\n'
    )
    if cmake.count(upstream_git_checkout) != 1:
        raise ValueError("Pinned Piper CMake source does not match the expected eSpeak-NG revision.")
    cmake = cmake.replace(upstream_git_checkout, offline_source)

    upstream_ucd_include = (
        "${ESPEAKNG_BUILD_DIR}/src/espeak_ng_external/src/ucd-tools/src/include"
    )
    offline_ucd_include = (
        "${CMAKE_CURRENT_SOURCE_DIR}/espeak-ng-source/src/ucd-tools/src/include"
    )
    if cmake.count(upstream_ucd_include) != 1:
        raise ValueError("Pinned Piper CMake source does not match the expected UCD include path.")
    cmake_path.write_text(cmake.replace(upstream_ucd_include, offline_ucd_include), encoding="utf-8")
    return source_root


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--piper-archive", type=Path, required=True)
    parser.add_argument("--espeak-archive", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=Path("piper_model.json"))
    parser.add_argument("--destination", type=Path, default=Path("offline-rebuild"))
    args = parser.parse_args()
    source = prepare(args.piper_archive, args.espeak_archive, args.manifest, args.destination)
    print(f"Prepared offline Piper source at {source}")
    print("Build requirements: scikit-build, setuptools, wheel, CMake >=3.26,<4, Ninja, C/C++ compiler")
    print("From that directory run: python -m pip wheel --no-build-isolation --no-deps --wheel-dir dist .")


if __name__ == "__main__":
    main()
