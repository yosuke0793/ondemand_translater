"""Validate native release assets, then create a draft using the configured gh CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

from build import ARTIFACTS, PACKAGING, sha256, validate_ffmpeg_license, version
from piper_source import application_source_files
from piper_model import model_metadata

TARGETS = ("windows-x64", "macos-arm64", "macos-x64")


def verify_piper_source_archive(path: Path, info: dict) -> None:
    manifest_bytes = (PACKAGING / "piper_model.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    runtime, model = manifest["runtime"], manifest["voice"]
    expected_model = model_metadata(model)
    expected_additional = [model_metadata(model)
                           for model in manifest.get("additional_voices", [])]
    if (info.get("runtime"), info.get("license"), info.get("model"),
            info.get("additional_models")) != (
        f'{runtime["distribution"]}=={runtime["version"]}', "GPL-3.0-or-later",
        expected_model, expected_additional,
    ):
        raise ValueError("Piper runtime/model provenance mismatch")
    espeak = runtime["embedded_espeak_ng"]
    expected_sources = [
        {"name": f'piper1-gpl-{runtime["source_revision"]}.tar.gz',
         "url": runtime["source_archive_url"], "sha256": runtime["source_archive_sha256"],
         "license": runtime["license"]},
        {"name": f'espeak-ng-{espeak["source_revision"]}.tar.gz',
         "url": espeak["source_archive_url"], "sha256": espeak["source_archive_sha256"],
         "license": "GPL-3.0-or-later"},
    ]
    with tarfile.open(path, "r:gz") as archive:
        members = {}
        for member in archive:
            name = PurePosixPath(member.name)
            if (not member.isfile() or name.is_absolute() or ".." in name.parts
                    or member.name in members):
                raise ValueError(f"Unsafe/duplicate Piper source member: {member.name}")
            members[member.name] = member

        def read(name: str) -> bytes:
            member = members.get(name)
            if member is None or member.size > 1024 * 1024:
                raise ValueError(f"Missing/oversized Piper source record: {name}")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError(f"Unreadable Piper source record: {name}")
            with stream:
                return stream.read()

        if read("piper-source/piper_model.json") != manifest_bytes:
            raise ValueError("Archived Piper model/source pins mismatch")
        record = read("piper-source/source-manifest.json")
        if hashlib.sha256(record).hexdigest() != info.get("source_manifest_sha256"):
            raise ValueError("Piper source-manifest hash mismatch")
        source_manifest = json.loads(record)
        for key, expected in (
            ("distribution", runtime["distribution"]), ("version", runtime["version"]),
            ("license", runtime["license"]), ("sources", expected_sources),
        ):
            if source_manifest.get(key) != expected:
                raise ValueError(f"Piper source record mismatch: {key}")
        recipe = source_manifest.get("recipe", {})
        if (source_manifest.get("schema_version") != 2
                or recipe.get("helper") != "packaging/rebuild_piper_offline.py"
                or not recipe.get("build") or not recipe.get("build_requirements")):
            raise ValueError("Missing Piper offline source build recipe")
        files = application_source_files()
        expected_inventory = {relative: sha256(file) for relative, file in files.items()}
        if source_manifest.get("application_files") != expected_inventory:
            raise ValueError("Piper combined application source inventory mismatch")
        expected_app_names = {f"piper-source/application/{relative}" for relative in files}
        if {name for name in members if name.startswith("piper-source/application/")} != expected_app_names:
            raise ValueError("Incomplete Piper combined application sources")
        for relative, expected_hash in expected_inventory.items():
            data = read(f"piper-source/application/{relative}")
            if hashlib.sha256(data).hexdigest() != expected_hash:
                raise ValueError(f"Piper application source hash mismatch: {relative}")
        helper = read("piper-source/rebuild_piper_offline.py")
        if hashlib.sha256(helper).hexdigest() != expected_inventory["packaging/rebuild_piper_offline.py"]:
            raise ValueError("Piper offline rebuild helper mismatch")
        expected_names = {f"piper-source/{source['name']}" for source in expected_sources}
        expected_records = {
            "piper-source/piper_model.json", "piper-source/source-manifest.json",
            "piper-source/rebuild_piper_offline.py",
        }
        if set(members) != expected_app_names | expected_names | expected_records:
            raise ValueError("Unexpected/missing Piper source archive contents")
        if {name for name in members if name.endswith(".tar.gz")} != expected_names:
            raise ValueError("Incomplete Piper/eSpeak corresponding sources")
        for source in expected_sources:
            stream = archive.extractfile(members[f"piper-source/{source['name']}"])
            if stream is None:
                raise ValueError(f"Unreadable Piper source: {source['name']}")
            with stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != source["sha256"]:
                raise ValueError(f"Piper corresponding source hash mismatch: {source['name']}")


def verify_source_archive(path: Path, target: str, value: str, media: dict) -> None:
    manifest_bytes = (PACKAGING / "ffmpeg.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    sources = manifest["sources"]
    if (manifest.get("license"), manifest.get("nonfree")) != ("GPL-3.0-or-later", False):
        raise ValueError("Tracked FFmpeg source manifest violates license policy")
    if media.get("manifest_sha256") != manifest_hash or media.get("sources") != sources:
        raise ValueError(f"Corresponding-source manifest mismatch: {target}")
    binaries = media.get("binaries", {})
    if set(binaries) != {"ffmpeg", "ffprobe"} or any(
        not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
        for digest in binaries.values()
    ):
        raise ValueError(f"Missing FFmpeg binary hashes: {target}")
    with tarfile.open(path, "r:gz") as archive:
        members = {}
        for member in archive:
            name = PurePosixPath(member.name)
            if (not member.isfile() or name.is_absolute() or ".." in name.parts
                    or member.name in members):
                raise ValueError(f"Unsafe/duplicate corresponding-source member: {member.name}")
            members[member.name] = member

        def read(name: str, limit: int = 1024 * 1024) -> bytes:
            member = members.get(name)
            if member is None or member.size > limit:
                raise ValueError(f"Missing/oversized corresponding-source record: {name}")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError(f"Unreadable corresponding-source record: {name}")
            with stream:
                return stream.read(limit + 1)

        for name in ("ffmpeg.json", "build-ffmpeg.sh", "ffmpeg_source.py"):
            if read(name) != (PACKAGING / name).read_bytes():
                raise ValueError(f"Corresponding-source recipe/manifest mismatch: {name}")
        expected_sources = {f"sources/{source['name']}" for source in sources}
        if {name for name in members if name.startswith("sources/")} != expected_sources:
            raise ValueError(f"Incomplete corresponding-source archive: {target}")
        for source in sources:
            stream = archive.extractfile(members[f"sources/{source['name']}"])
            if stream is None:
                raise ValueError(f"Unreadable corresponding source: {source['name']}")
            with stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != source["sha256"]:
                raise ValueError(f"Corresponding source hash mismatch: {source['name']}")
        info = json.loads(read("build-info.json"))
        for key, expected in (
            ("target", target), ("app_version", value), ("license", "GPL-3.0-or-later"),
            ("nonfree", False), ("manifest_sha256", manifest_hash),
            ("sources", sources), ("binaries", binaries),
        ):
            if info.get(key) != expected:
                raise ValueError(f"Corresponding-source build record mismatch: {key}")
        validate_ffmpeg_license(info["ffmpeg_version"], info["ffmpeg_license"])
        config = read("build/ffmpeg-configure-args.txt").decode("utf-8")
        if (config != info.get("configure_args") or "--enable-nonfree" in config
                or "--enable-gpl" not in config or "--enable-version3" not in config):
            raise ValueError(f"Corresponding-source configure record mismatch: {target}")
        for name in ("build/build.log", "build/ffmpeg-configure.log", "build/ffmpeg-build.log"):
            if name not in members or members[name].size == 0:
                raise ValueError(f"Missing corresponding-source build log: {name}")


def validate_assets(tag: str) -> list[Path]:
    value = version(tag)
    expected = set()
    commits = set()
    for target in TARGETS:
        prefix = f"TranslateService-{value}-{target}"
        names = {f"{prefix}-build-info.json"}
        names.add(f"{prefix}-ffmpeg-source.tar.gz")
        names.add(f"{prefix}-piper-source.tar.gz")
        names.update({f"{prefix}-setup.exe", f"{prefix}-portable.zip"}
                     if target == "windows-x64" else {f"{prefix}.dmg"})
        checksum = ARTIFACTS / f"{prefix}-SHA256SUMS.txt"
        listed = set()
        for line in checksum.read_text(encoding="utf-8").splitlines():
            digest, name = line.split("  ", 1)
            if name not in names or name in listed:
                raise ValueError(f"Unexpected/duplicate checksum entry: {name}")
            if sha256(ARTIFACTS / name) != digest:
                raise ValueError(f"Checksum mismatch: {name}")
            listed.add(name)
        if listed != names:
            raise ValueError(f"Incomplete checksum set for {target}")
        info = json.loads((ARTIFACTS / f"{prefix}-build-info.json").read_text(encoding="utf-8"))
        if (info["version"], info["target"]) != (value, target):
            raise ValueError(f"Asset target/version mismatch: {target}")
        media = info.get("ffmpeg", {})
        source_name = f"{prefix}-ffmpeg-source.tar.gz"
        if (media.get("source_archive"), media.get("license"), media.get("nonfree")) != (
            source_name, "GPL-3.0-or-later", False,
        ):
            raise ValueError(f"Missing corresponding-source/license confirmation: {target}")
        if sha256(ARTIFACTS / source_name) != media.get("source_sha256"):
            raise ValueError(f"Corresponding-source hash mismatch: {target}")
        if info.get("ffmpeg_manifest_sha256") != media.get("manifest_sha256"):
            raise ValueError(f"Build FFmpeg manifest mismatch: {target}")
        verify_source_archive(ARTIFACTS / source_name, target, value, media)
        piper = info.get("piper", {})
        piper_source_name = f"{prefix}-piper-source.tar.gz"
        if piper.get("source_archive") != piper_source_name:
            raise ValueError(f"Missing Piper corresponding-source confirmation: {target}")
        if sha256(ARTIFACTS / piper_source_name) != piper.get("source_sha256"):
            raise ValueError(f"Piper corresponding-source hash mismatch: {target}")
        verify_piper_source_archive(ARTIFACTS / piper_source_name, piper)
        commits.add(info["commit"])
        roles = {report["role"] for report in info["self_tests"]
                 if report["status"] == "ok" and report["version"] == value}
        if roles != {"gui", "worker"}:
            raise ValueError(f"Missing successful GUI/worker self-tests: {target}")
        expected.update(names | {checksum.name})
    if len(commits) != 1:
        raise ValueError("Native build artifacts do not share one source revision")
    actual = {path.name for path in ARTIFACTS.iterdir() if path.is_file()}
    if actual != expected:
        raise ValueError(f"Release directory contains missing/unexpected assets: {actual ^ expected}")
    return [ARTIFACTS / name for name in sorted(expected)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    assets = validate_assets(args.tag)
    if args.check_only:
        print(f"Verified {len(assets)} release files for {args.tag}")
        return
    # Deliberately fail if a release already exists; do not mutate a published release.
    existing = subprocess.run(["gh", "release", "view", args.tag],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if existing.returncode == 0:
        raise RuntimeError("Release already exists; review/recover it manually instead of overwriting")
    subprocess.run(
        ["gh", "release", "create", args.tag, "--verify-tag", "--draft",
         "--title", f"TranslateService {args.tag}",
         "--notes", "DRAFT: unsigned native packages. Review signing, clean-machine smoke tests, "
         "and GPL corresponding-source/license obligations in docs/PACKAGING.md before publication. "
         "No unattended production publication is performed.",
         *map(str, assets)], check=True,
    )


if __name__ == "__main__":
    main()
