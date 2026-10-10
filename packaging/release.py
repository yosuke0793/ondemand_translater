"""Validate native release assets, then create a draft using the configured gh CLI."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from build import ARTIFACTS, sha256, version

TARGETS = ("windows-x64", "macos-arm64", "macos-x64")


def validate_assets(tag: str) -> list[Path]:
    value = version(tag)
    expected = set()
    commits = set()
    for target in TARGETS:
        prefix = f"TranslateService-{value}-{target}"
        names = {f"{prefix}-build-info.json"}
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
