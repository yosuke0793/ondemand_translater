"""Reject media binaries that do not match the distribution's license policy."""

from __future__ import annotations

import re


def validate_ffmpeg_license(version_output: str, license_output: str) -> str:
    if re.search(r"--enable-nonfree(?:\s|$|[\"'])", version_output) or re.search(
        r"nonfree parts|not legally redistributable|unredistributable",
        license_output, re.IGNORECASE,
    ):
        raise RuntimeError("FFmpeg/ffprobeのnonfree構成は配布できません。再配布可能な同梱版へ置き換えてください。")
    if "--enable-gpl" not in version_output or "--enable-version3" not in version_output:
        raise RuntimeError("FFmpeg/ffprobeが配布用のGPLv3構成と一致しません。")
    if "GNU General Public License" not in license_output or not re.search(
        r"version\s+3(?:\b|,)", license_output, re.IGNORECASE,
    ):
        raise RuntimeError("FFmpeg/ffprobeのGPLv3ライセンス表示を確認できません。")
    return "GPL-3.0-or-later"
