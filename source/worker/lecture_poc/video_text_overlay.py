from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


_JP_CHAR_RE = re.compile(r"[ぁ-んァ-ン一-龥々ー]")
_MATH_ONLY_RE = re.compile(r"^[0-9A-Za-z\s\+\-\*/=\^_\(\)\[\]\{\}\|\\<>:;,\.]+$")


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def _is_formula_or_symbol(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return True
    if _MATH_ONLY_RE.fullmatch(normalized):
        return True

    total = len(normalized)
    jp_count = len(_JP_CHAR_RE.findall(normalized))
    symbol_count = len(re.findall(r"[^0-9A-Za-zぁ-んァ-ン一-龥々ー\s]", normalized))
    jp_ratio = jp_count / total if total else 0.0
    symbol_ratio = symbol_count / total if total else 1.0

    # Exclude non-Japanese or highly symbolic snippets.
    if jp_ratio < 0.4:
        return True
    if symbol_ratio > 0.5:
        return True
    return False


def _iou(a: dict[str, Any], b: dict[str, Any]) -> float:
    ax1, ay1 = int(a["x"]), int(a["y"])
    ax2, ay2 = ax1 + int(a["w"]), ay1 + int(a["h"])
    bx1, by1 = int(b["x"]), int(b["y"])
    bx2, by2 = bx1 + int(b["w"]), by1 + int(b["h"])

    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0

    area_a = max(1, (ax2 - ax1) * (ay2 - ay1))
    area_b = max(1, (bx2 - bx1) * (by2 - by1))
    return inter / float(area_a + area_b - inter)


def _escape_drawtext_text(text: str) -> str:
    escaped = text
    escaped = escaped.replace("\\", r"\\")
    escaped = escaped.replace(":", r"\:")
    escaped = escaped.replace("'", r"\'")
    escaped = escaped.replace("%", r"\%")
    escaped = escaped.replace("\n", r"\n")
    return escaped


def _escape_drawtext_fontvalue(value: str) -> str:
    escaped = value
    escaped = escaped.replace("\\", r"\\")
    escaped = escaped.replace(":", r"\:")
    escaped = escaped.replace("'", r"\'")
    return escaped


def _resolve_font_file(font_family: str) -> str | None:
    try:
        proc = subprocess.run(
            ["fc-match", "-f", "%{file}\n", font_family],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except Exception:
        return None

    candidate = proc.stdout.strip()
    if candidate and Path(candidate).exists():
        return candidate
    return None


def detect_video_text_regions(
    input_video: Path,
    output_json: Path,
    sample_fps: float,
    min_chars: int,
    min_confidence: float,
) -> list[dict[str, Any]]:
    try:
        import cv2
    except ImportError as exc:
        raise ModuleNotFoundError("opencv-python-headless が必要です") from exc

    try:
        import pytesseract
    except ImportError as exc:
        raise ModuleNotFoundError("pytesseract が必要です") from exc

    try:
        _ = pytesseract.get_tesseract_version()
    except Exception as exc:
        raise FileNotFoundError(
            "tesseract-ocr が見つかりません。`sudo apt install -y tesseract-ocr tesseract-ocr-jpn` を実行してください。"
        ) from exc

    cap = cv2.VideoCapture(str(input_video))
    if not cap.isOpened():
        raise RuntimeError(f"動画を開けません: {input_video}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, int(round(fps / max(sample_fps, 0.1))))

    tracks: list[dict[str, Any]] = []
    frame_index = -1

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame_index += 1
        if frame_index % step != 0:
            continue

        timestamp_sec = frame_index / fps
        data = pytesseract.image_to_data(
            frame,
            lang="jpn",
            output_type=pytesseract.Output.DICT,
            config="--psm 6",
        )

        frame_regions: list[dict[str, Any]] = []
        n = len(data.get("text", []))
        for i in range(n):
            raw_text = str(data["text"][i] or "")
            text = _normalize_text(raw_text)
            if len(text) < min_chars:
                continue

            try:
                conf = float(data["conf"][i])
            except Exception:
                conf = -1.0
            if conf < min_confidence:
                continue
            if _is_formula_or_symbol(text):
                continue

            x = int(data["left"][i])
            y = int(data["top"][i])
            w = int(data["width"][i])
            h = int(data["height"][i])
            if w <= 2 or h <= 2:
                continue

            frame_regions.append(
                {
                    "text": text,
                    "x": x,
                    "y": y,
                    "w": w,
                    "h": h,
                    "confidence": conf,
                    "t": timestamp_sec,
                }
            )

        for region in frame_regions:
            matched = None
            for track in tracks:
                if track["text"] != region["text"]:
                    continue
                if region["t"] - track["last_seen"] > 1.5:
                    continue
                if _iou(track, region) < 0.3:
                    continue
                matched = track
                break

            if matched is None:
                tracks.append(
                    {
                        "text": region["text"],
                        "x": float(region["x"]),
                        "y": float(region["y"]),
                        "w": float(region["w"]),
                        "h": float(region["h"]),
                        "confidence": float(region["confidence"]),
                        "start_sec": float(region["t"]),
                        "end_sec": float(region["t"]),
                        "last_seen": float(region["t"]),
                        "samples": 1,
                    }
                )
                continue

            samples = int(matched["samples"]) + 1
            matched["x"] = (matched["x"] * matched["samples"] + region["x"]) / samples
            matched["y"] = (matched["y"] * matched["samples"] + region["y"]) / samples
            matched["w"] = (matched["w"] * matched["samples"] + region["w"]) / samples
            matched["h"] = (matched["h"] * matched["samples"] + region["h"]) / samples
            matched["confidence"] = max(float(matched["confidence"]), float(region["confidence"]))
            matched["end_sec"] = float(region["t"])
            matched["last_seen"] = float(region["t"])
            matched["samples"] = samples

    cap.release()

    regions: list[dict[str, Any]] = []
    for track in tracks:
        if track["samples"] < 2:
            continue
        if track["end_sec"] - track["start_sec"] < 0.4:
            continue
        regions.append(
            {
                "text": track["text"],
                "x": int(round(track["x"])),
                "y": int(round(track["y"])),
                "w": int(round(track["w"])),
                "h": int(round(track["h"])),
                "start_sec": round(float(track["start_sec"]), 3),
                "end_sec": round(float(track["end_sec"]), 3),
                "confidence": round(float(track["confidence"]), 2),
                "samples": int(track["samples"]),
            }
        )

    regions.sort(key=lambda item: (item["start_sec"], item["y"], item["x"]))

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps({"source": str(input_video), "regions": regions}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return regions


def overlay_translated_regions(
    input_video: Path,
    output_video: Path,
    regions: list[dict[str, Any]],
    translations: dict[str, str],
    font_family: str,
    font_color: str = "white",
) -> None:
    if not regions:
        output_video.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(input_video, output_video)
        return

    draw_filters: list[str] = []
    font_file = _resolve_font_file(font_family)
    font_expr = None
    if font_file:
        font_expr = f"fontfile='{_escape_drawtext_fontvalue(font_file)}'"
    else:
        font_expr = f"font='{_escape_drawtext_fontvalue(font_family)}'"

    for region in regions:
        source_text = str(region["text"])
        translated = _normalize_text(translations.get(source_text, source_text))
        if not translated:
            continue

        x = int(region["x"])
        y = int(region["y"])
        w = max(8, int(region["w"]))
        h = max(8, int(region["h"]))
        start_sec = float(region["start_sec"])
        end_sec = float(region["end_sec"])
        fontsize = max(12, int(h * 0.85))

        enable = f"between(t,{start_sec:.3f},{end_sec:.3f})"
        draw_filters.append(
            f"drawbox=x={x}:y={y}:w={w}:h={h}:color=black@1.0:t=fill:enable='{enable}'"
        )
        draw_filters.append(
            "drawtext="
            f"text='{_escape_drawtext_text(translated)}':"
            f"x={x + 4}:y={y + max(1, int(h * 0.08))}:"
            f"{font_expr}:fontsize={fontsize}:fontcolor={font_color}:"
            "line_spacing=2:"
            f"enable='{enable}'"
        )

    if not draw_filters:
        output_video.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(input_video, output_video)
        return

    output_video.parent.mkdir(parents=True, exist_ok=True)
    filter_chain = ",".join(draw_filters)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_video),
            "-vf",
            filter_chain,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(output_video),
        ],
        check=True,
    )
