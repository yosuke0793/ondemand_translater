"""Validate native release assets, then create a draft using the configured gh CLI."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shlex
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

from build import ARTIFACTS, PACKAGING, sha256, validate_ffmpeg_license, version
from piper_source import application_source_files
from piper_model import model_metadata

TARGETS = ("windows-x64", "macos-arm64", "macos-x64")


def _sha256_stream(stream) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


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
                digest = _sha256_stream(stream)
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
                digest = _sha256_stream(stream)
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


def verify_pyav_source_archive(path: Path, target: str, value: str, info: dict) -> None:
    manifest_bytes = (PACKAGING / "pyav.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    pyav, ffmpeg, recipe = manifest["pyav"], manifest["ffmpeg"], manifest["recipe"]
    expected_sources = [
        {key: pyav[key] for key in ("name", "url", "sha256", "license")},
        {key: ffmpeg[key] for key in ("name", "url", "sha256", "license")},
    ]
    if (
        (manifest.get("schema_version"), pyav.get("version"), pyav.get("license"),
         ffmpeg.get("version"), ffmpeg.get("license"), recipe.get("license"),
         recipe.get("nonfree"))
        != (1, "15.1.0", "BSD-3-Clause", "7.1.1", "LGPL-2.1-or-later",
            "LGPL-2.1-or-later", False)
    ):
        raise ValueError("Managed PyAV/FFmpeg license manifest mismatch")
    if info.get("manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest():
        raise ValueError("Managed PyAV source manifest hash mismatch")
    if info.get("sources") != expected_sources:
        raise ValueError("Managed PyAV source records do not match pinned source manifest")
    with tarfile.open(path, "r:gz") as archive:
        members = {}
        for member in archive:
            name = PurePosixPath(member.name)
            if (not member.isfile() or name.is_absolute() or ".." in name.parts
                    or member.name in members):
                raise ValueError(f"Unsafe/duplicate managed PyAV source member: {member.name}")
            members[member.name] = member

        def read(name: str, limit: int = 1024 * 1024) -> bytes:
            member = members.get(name)
            if member is None or member.size > limit:
                raise ValueError(f"Missing/oversized managed PyAV source record: {name}")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError(f"Unreadable managed PyAV source record: {name}")
            with stream:
                return stream.read(limit + 1)

        for name in ("pyav.json", "pyav_source.py", "build-pyav-ffmpeg.sh"):
            if read(name) != (PACKAGING / name).read_bytes():
                raise ValueError(f"Managed PyAV recipe/manifest mismatch: {name}")
        expected_names = {f"sources/{source['name']}" for source in expected_sources}
        for source in expected_sources:
            source_name = f"sources/{source['name']}"
            member = members.get(source_name)
            if member is None:
                raise ValueError(f"Missing managed corresponding source: {source['name']}")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError(f"Unreadable managed corresponding source: {source['name']}")
            with stream:
                if _sha256_stream(stream) != source["sha256"]:
                    raise ValueError(f"Managed corresponding source hash mismatch: {source['name']}")
        record_bytes = read("build-info.json")
        build_record = json.loads(record_bytes)
        for key, expected in (
            ("schema_version", 1), ("target", target), ("app_version", value),
            ("pyav_version", "15.1.0"), ("pyav_license", "BSD-3-Clause"),
            ("ffmpeg_version", "7.1.1"), ("ffmpeg_license", "LGPL-2.1-or-later"),
            ("nonfree", False), ("manifest_sha256", info["manifest_sha256"]),
            ("sources", expected_sources), ("source_archive", path.name),
            ("configure_args", info.get("configure_args")),
            ("shared_library_sha256", info.get("shared_library_sha256")),
            ("pyav_wheel_sha256", info.get("pyav_wheel_sha256")),
        ):
            if build_record.get(key) != expected:
                raise ValueError(f"Managed PyAV source build record mismatch: {key}")
        configure = read("build/ffmpeg-configure-args.txt").decode("utf-8")
        if configure != build_record.get("configure_args"):
            raise ValueError("Managed PyAV FFmpeg configure log mismatch")
        args = shlex.split(configure)
        if any(arg in args for arg in (
            "--enable-gpl", "--enable-nonfree", "--enable-version3",
            "--enable-libx264", "--enable-libx265",
        )):
            raise ValueError("Managed PyAV FFmpeg configure log enables GPL/nonfree components")
        if "--enable-shared" not in args or "--disable-static" not in args:
            raise ValueError("Managed PyAV FFmpeg must use shared libraries only")
        for required in recipe["configure_args"]:
            if required not in args:
                raise ValueError(f"Managed PyAV FFmpeg configure log missing {required}")
        libraries = build_record.get("shared_library_sha256", {})
        required_libraries = recipe["required_libraries"]
        if any(not any(name in filename.lower() for filename in libraries)
               for name in required_libraries):
            raise ValueError("Managed PyAV build record omits a required shared FFmpeg library")
        if any(not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in libraries.values()):
            raise ValueError("Managed PyAV shared-library hash record is malformed")
        if not re.fullmatch(r"[0-9a-f]{64}", build_record.get("pyav_wheel_sha256", "")):
            raise ValueError("Managed PyAV wheel hash record is malformed")
        for name, expected_member in (
            ("notices/PyAV-LICENSE.txt", ("sources/av-15.1.0.tar.gz", "av-15.1.0/LICENSE.txt")),
            ("notices/FFmpeg-COPYING.LGPLv2.1.txt",
             ("sources/ffmpeg-7.1.1.tar.xz", "ffmpeg-7.1.1/COPYING.LGPLv2.1")),
        ):
            source_stream = archive.extractfile(members[expected_member[0]])
            if source_stream is None:
                raise ValueError(f"Missing source for managed PyAV notice: {expected_member[0]}")
            with source_stream:
                with tarfile.open(fileobj=io.BytesIO(source_stream.read()), mode="r:*") as source_archive:
                    expected_license = source_archive.extractfile(expected_member[1])
                    if expected_license is None or read(name) != expected_license.read():
                        raise ValueError(f"Managed PyAV license notice mismatch: {name}")
        if read("notices/managed-source-manifest.json") != manifest_bytes:
            raise ValueError("Managed PyAV notice manifest mismatch")
        expected_names.update({
            "build-info.json", "build/ffmpeg-configure-args.txt", "build/build.log",
            "build/ffmpeg-configure.log", "build/ffmpeg-build.log", "build/pyav-build.log",
            "notices/PyAV-LICENSE.txt", "notices/FFmpeg-COPYING.LGPLv2.1.txt",
            "notices/managed-source-manifest.json",
        })
        if set(members) != expected_names | {
            "pyav.json", "pyav_source.py", "build-pyav-ffmpeg.sh",
        }:
            raise ValueError(f"Unexpected/missing managed PyAV source archive contents: {target}")
        for name in ("build/build.log", "build/ffmpeg-configure-args.txt",
                     "build/ffmpeg-configure.log", "build/ffmpeg-build.log", "build/pyav-build.log"):
            if name not in members or members[name].size == 0:
                raise ValueError(f"Missing managed PyAV build record: {name}")
    smoke = info.get("decode_smoke", {})
    if (
        smoke.get("pyav_version") != "15.1.0"
        or smoke.get("decoded") != {"codec": "h264", "width": 160, "height": 120}
        or smoke.get("frozen_decode") != {
            "pyav_version": "15.1.0", "decoded": {"codec": "h264", "width": 160, "height": 120},
        }
        or any(f"lib{name}" not in smoke.get("ffmpeg_library_versions", {})
               for name in recipe["required_libraries"])
    ):
        raise ValueError("Missing successful managed PyAV/FFmpeg decode smoke test")


def validate_assets(tag: str) -> list[Path]:
    value = version(tag)
    expected = set()
    commits = set()
    for target in TARGETS:
        prefix = f"TranslateService-{value}-{target}"
        names = {f"{prefix}-build-info.json"}
        names.add(f"{prefix}-ffmpeg-source.tar.gz")
        names.add(f"{prefix}-piper-source.tar.gz")
        names.add(f"{prefix}-pyav-source.tar.gz")
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
        pyav = info.get("pyav", {})
        pyav_source_name = f"{prefix}-pyav-source.tar.gz"
        if (
            pyav.get("source_archive"), pyav.get("pyav_version"), pyav.get("pyav_license"),
            pyav.get("ffmpeg_version"), pyav.get("ffmpeg_license"), pyav.get("nonfree"),
        ) != (pyav_source_name, "15.1.0", "BSD-3-Clause", "7.1.1", "LGPL-2.1-or-later", False):
            raise ValueError(f"Missing managed PyAV/FFmpeg license confirmation: {target}")
        if sha256(ARTIFACTS / pyav_source_name) != pyav.get("source_archive_sha256"):
            raise ValueError(f"Managed PyAV corresponding-source hash mismatch: {target}")
        verify_pyav_source_archive(ARTIFACTS / pyav_source_name, target, value, pyav)
        provenance = info.get("source_provenance", {})
        expected_revision = (
            f"source-manifest-sha256:{provenance.get('value')}"
            if provenance.get("kind") == "source-manifest-sha256"
            else provenance.get("value")
        )
        if not info.get("commit") or expected_revision != info.get("commit"):
            raise ValueError(f"Missing explicit source build provenance: {target}")
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
