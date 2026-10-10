from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter, sleep

from .jp_cleaner import clean_jp_srt
from .srt_utils import (
    SRTBlock,
    extract_srt_entries,
    format_timestamp,
    parse_srt_block,
    render_srt_block,
    split_srt_blocks,
    translate_srt_text_lines,
)
from .video_text_overlay import detect_video_text_regions, overlay_translated_regions


@dataclass(frozen=True)
class PipelineConfig:
    input_video: Path
    outdir: Path
    model_size: str | None = None
    whisper_prompt_file: Path | None = None
    transcription_preset: str = "balanced"
    voice: str = "en-US-AriaNeural"
    tts_volume: str = "+100%"
    chunk_seconds: float = 600.0
    chunk_overlap_seconds: float = 1.5
    transcription_workers: int = 1
    transcription_cpu_threads: int = 0
    transcription_max_pending_chunks: int = 0
    transcription_device: str = "auto"
    transcription_compute_type: str | None = None
    tts_workers: int = 1
    tts_timing_mode: str = "no-overlap"
    tts_backend: str = "edge-tts"
    subs_sync_mode: str = "tts-aligned"
    lowpass_cutoff_hz: float = 6500.0
    jp_clean_mode: str = "rule"
    ollama_model: str = "qwen2.5:7b-instruct"
    translation_backend: str = "gemini"
    deepl_api_key: str | None = None
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.1-flash-lite"
    burn_subs: bool = False
    embed_subs: bool = False
    embed_subs_container: str = "mp4"
    embed_subs_format: str = "srt"
    burn_subs_in_band: bool = False
    burn_subs_band_ratio: float = 0.0
    video_text_overlay: bool = False
    video_text_overlay_fps: float = 2.0
    video_text_overlay_min_chars: int = 3
    video_text_overlay_min_confidence: float = 70.0
    video_text_overlay_font: str = "Noto Sans"
    force: bool = False


@dataclass(frozen=True)
class TranscriptionSettings:
    model_size: str
    compute_type: str
    beam_size: int
    best_of: int
    condition_on_previous_text: bool
    temperature: float


@dataclass(frozen=True)
class TranscriptionChunk:
    index: int
    core_start: float
    core_end: float
    chunk_start: float
    chunk_end: float
    chunk_duration: float
    chunk_path: Path


TRANSCRIPTION_PRESETS: dict[str, TranscriptionSettings] = {
    "high-accuracy": TranscriptionSettings(
        model_size="large-v3",
        compute_type="float32",
        beam_size=10,
        best_of=5,
        condition_on_previous_text=True,
        temperature=0.0,
    ),
    "balanced": TranscriptionSettings(
        model_size="medium",
        compute_type="int8",
        beam_size=5,
        best_of=3,
        condition_on_previous_text=True,
        temperature=0.0,
    ),
    "fast": TranscriptionSettings(
        model_size="small",
        compute_type="int8",
        beam_size=1,
        best_of=1,
        condition_on_previous_text=False,
        temperature=0.0,
    ),
}

DEEPL_BATCH_MAX_TEXTS = 40
DEEPL_BATCH_MAX_CHARS = 20_000
GEMINI_BATCH_MAX_TEXTS_DEFAULT = 120
GEMINI_BATCH_MAX_CHARS_DEFAULT = 48_000
GEMINI_ONE_SHOT_MAX_TEXTS_DEFAULT = 100


def read_positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value <= 0:
        return default
    return value


GEMINI_BATCH_MAX_TEXTS = read_positive_int_env(
    "GEMINI_BATCH_MAX_TEXTS",
    GEMINI_BATCH_MAX_TEXTS_DEFAULT,
)
GEMINI_BATCH_MAX_CHARS = read_positive_int_env(
    "GEMINI_BATCH_MAX_CHARS",
    GEMINI_BATCH_MAX_CHARS_DEFAULT,
)
GEMINI_ONE_SHOT_MAX_TEXTS = read_positive_int_env(
    "GEMINI_ONE_SHOT_MAX_TEXTS",
    GEMINI_ONE_SHOT_MAX_TEXTS_DEFAULT,
)

XTTS_MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"
XTTS_REFERENCE_SAMPLE_SECONDS = 12.0
XTTS_REFERENCE_MIN_SECONDS = 6.0


def read_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default

# Default fallback order tries higher-capability Gemini models first.
GEMINI_FALLBACK_MODELS_DEFAULT = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
]
GEMINI_DISCOVER_MODELS_DEFAULT = True

_GEMINI_AVAILABLE_MODELS_CACHE: set[str] | None = None


class PipelineStepError(RuntimeError):
    def __init__(self, step: str, message: str):
        super().__init__(message)
        self.step = step


PIPELINE_LOG_PATH: Path | None = None
DEFAULT_WHISPER_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "whisper_initial_prompt.txt"
WHISPER_INITIAL_PROMPT_MAX_CHARS = 480


def log_pipeline(message: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"[pipeline {timestamp}] {message}"
    print(line, flush=True)
    if PIPELINE_LOG_PATH is not None:
        with PIPELINE_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def run_pipeline_stage(stage_name: str, func, *args) -> None:
    started_at = perf_counter()
    log_pipeline(f"START {stage_name}")
    try:
        func(*args)
    except Exception:
        elapsed = perf_counter() - started_at
        log_pipeline(f"FAIL  {stage_name} ({elapsed:.1f}s)")
        raise
    elapsed = perf_counter() - started_at
    log_pipeline(f"DONE  {stage_name} ({elapsed:.1f}s)")


def run_pipeline(config: PipelineConfig) -> None:
    global PIPELINE_LOG_PATH

    if not config.input_video.exists():
        raise FileNotFoundError(f"Input video not found: {config.input_video}")

    ensure_ffmpeg()

    config.outdir.mkdir(parents=True, exist_ok=True)
    PIPELINE_LOG_PATH = config.outdir / "pipeline.log"
    PIPELINE_LOG_PATH.write_text("", encoding="utf-8")
    log_pipeline(f"INPUT {config.input_video}")
    log_pipeline(f"OUTDIR {config.outdir}")
    log_pipeline(f"LOGFILE {PIPELINE_LOG_PATH}")

    audio_wav = config.outdir / "audio.wav"
    jp_srt = config.outdir / "jp.srt"
    jp_clean_srt = config.outdir / "jp_clean.srt"
    en_srt = config.outdir / "en.srt"
    en_aligned_srt = config.outdir / "en_aligned.srt"
    en_txt = config.outdir / "en.txt"
    en_mp3 = config.outdir / "en.mp3"
    overlay_report_json = config.outdir / "video_text_regions.json"
    overlay_video = config.outdir / "video_overlay.mp4"
    final_mp4 = config.outdir / "final.mp4"
    final_sub_mp4 = config.outdir / "final_sub.mp4"
    final_soft_sub = config.outdir / f"final_soft_sub.{config.embed_subs_container}"

    run_pipeline_stage("extract_audio", extract_audio, config.input_video, audio_wav, config.lowpass_cutoff_hz, config.force)
    run_pipeline_stage("transcribe_audio", transcribe_audio, audio_wav, jp_srt, config)
    run_pipeline_stage(
        "clean_jp_srt",
        clean_jp_srt,
        jp_srt,
        jp_clean_srt,
        config.jp_clean_mode,
        config.ollama_model,
        config.force,
    )
    run_pipeline_stage(
        "translate_jp_srt",
        translate_jp_srt,
        jp_clean_srt,
        en_srt,
        config.force,
        config.translation_backend,
        config.deepl_api_key,
        config.gemini_api_key,
        config.gemini_model,
    )
    run_pipeline_stage("write_en_txt", write_en_txt, en_srt, en_txt, config.force)

    generate_force = config.force
    if (
        config.subs_sync_mode == "tts-aligned"
        and config.tts_timing_mode == "no-overlap"
        and not en_aligned_srt.exists()
    ):
        generate_force = True

    run_pipeline_stage(
        "generate_en_mp3",
        generate_en_mp3,
        en_srt,
        jp_clean_srt,
        config.input_video,
        en_mp3,
        config.voice,
        config.tts_volume,
        generate_force,
        config.tts_workers,
        config.tts_timing_mode,
        config.tts_backend,
        config.subs_sync_mode,
        en_aligned_srt,
    )

    subtitle_for_video = en_srt
    if config.subs_sync_mode == "tts-aligned" and config.tts_timing_mode == "no-overlap":
        if en_aligned_srt.exists():
            subtitle_for_video = en_aligned_srt
            log_pipeline("INFO  subtitle_timing source=en_aligned.srt")
        else:
            log_pipeline("WARN  subtitle_timing requested tts-aligned but en_aligned.srt was not found")
    else:
        log_pipeline("INFO  subtitle_timing source=en.srt")

    mux_input_video = config.input_video
    if config.video_text_overlay:
        run_pipeline_stage(
            "overlay_video_text",
            overlay_video_text,
            config.input_video,
            overlay_video,
            overlay_report_json,
            config.translation_backend,
            config.deepl_api_key,
            config.gemini_api_key,
            config.gemini_model,
            config.video_text_overlay_fps,
            config.video_text_overlay_min_chars,
            config.video_text_overlay_min_confidence,
            config.video_text_overlay_font,
            config.force,
        )
        mux_input_video = overlay_video

    run_pipeline_stage("mux_final_video", mux_final_video, mux_input_video, en_mp3, final_mp4, config.force)

    if config.burn_subs:
        run_pipeline_stage(
            "burn_subtitles",
            burn_subtitles,
            final_mp4,
            subtitle_for_video,
            final_sub_mp4,
            config.burn_subs_in_band,
            config.burn_subs_band_ratio,
            config.force,
        )
    elif config.embed_subs:
        run_pipeline_stage(
            "embed_subtitles",
            embed_subtitles,
            final_mp4,
            subtitle_for_video,
            final_soft_sub,
            config.embed_subs_container,
            config.embed_subs_format,
            final_sub_mp4,
            config.burn_subs_in_band,
            config.burn_subs_band_ratio,
            config.force,
        )

    log_pipeline("COMPLETE all stages finished")


def ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise FileNotFoundError(
            "ffmpeg が見つかりません。Ubuntu では `sudo apt update && sudo apt install -y ffmpeg`、"
            "macOS では `brew install ffmpeg` を実行してください。"
        )


def ensure_ffmpeg_subtitles_filter() -> None:
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise FileNotFoundError("ffmpeg のフィルタ一覧を取得できませんでした。") from exc

    filters_text = f"{result.stdout}\n{result.stderr}"
    if " subtitles " not in filters_text and not filters_text.strip().endswith(" subtitles"):
        raise FileNotFoundError(
            "現在の ffmpeg は字幕焼き込み用の subtitles フィルタをサポートしていません。"
            " libass 対応の ffmpeg をインストールして再実行してください。"
        )


def ffmpeg_has_encoder(encoder_name: str) -> bool:
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False

    encoders_text = f"{result.stdout}\n{result.stderr}"
    return encoder_name in encoders_text


def build_burn_subtitles_video_encode_args() -> list[str]:
    # Prefer NVENC on NVIDIA hosts for faster subtitle burn-in while keeping quality.
    if ffmpeg_has_encoder("h264_nvenc"):
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p5",
            "-rc",
            "vbr_hq",
            "-cq",
            "19",
            "-b:v",
            "0",
            "-pix_fmt",
            "yuv420p",
        ]

    return [
        "-c:v",
        "libx264",
        "-crf",
        "20",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
    ]


def build_en_mp3_metadata(
    en_srt: Path,
    jp_reference_srt: Path,
    input_video: Path,
    tts_backend: str,
    voice: str,
    tts_volume: str,
    tts_timing_mode: str,
    subs_sync_mode: str,
) -> dict[str, str]:
    en_srt_text = en_srt.read_text(encoding="utf-8")
    jp_srt_text = jp_reference_srt.read_text(encoding="utf-8")
    input_stat = input_video.stat()
    return {
        "tts_backend": tts_backend,
        "voice": voice,
        "tts_volume": tts_volume,
        "tts_timing_mode": tts_timing_mode,
        "subs_sync_mode": subs_sync_mode,
        "input_video_path": str(input_video.resolve()),
        "input_video_size": str(input_stat.st_size),
        "input_video_mtime_ns": str(input_stat.st_mtime_ns),
        "en_srt_sha256": hashlib.sha256(en_srt_text.encode("utf-8")).hexdigest(),
        "jp_srt_sha256": hashlib.sha256(jp_srt_text.encode("utf-8")).hexdigest(),
    }


def write_en_mp3_metadata(metadata_path: Path, metadata: dict[str, str]) -> None:
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def should_regenerate_en_mp3(
    en_mp3: Path,
    metadata_path: Path,
    expected_metadata: dict[str, str],
    input_video: Path,
    force: bool,
) -> bool:
    if force or not en_mp3.exists():
        return True

    try:
        existing_metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else None
    except json.JSONDecodeError:
        existing_metadata = None

    if existing_metadata != expected_metadata:
        print("[音声合成] 既存 en.mp3 の設定が変わったため、再生成します")
        return True

    try:
        en_duration = get_media_duration_seconds(en_mp3)
        video_duration = get_media_duration_seconds(input_video)
    except PipelineStepError:
        return True

    if en_duration + 0.5 < video_duration:
        print("[音声合成] 既存 en.mp3 が動画より短いため、タイムコード同期版を再生成します")
        return True
    print(f"[skip] en audio already exists: {en_mp3}")
    return False


def generate_tts_mp3_from_segments(
    en_srt: Path,
    input_video: Path,
    en_mp3: Path,
    tts_workers: int,
    tts_timing_mode: str,
    subs_sync_mode: str,
    aligned_srt_output: Path | None,
    synthesize_text_segment,
) -> None:
    raw_entries = extract_srt_entries(en_srt.read_text(encoding="utf-8"))
    entries = [entry for entry in raw_entries if entry.text.strip()]
    video_duration = get_media_duration_seconds(input_video)

    if not entries:
        generate_silence_mp3(en_mp3, video_duration)
        return

    if len(entries) != len(raw_entries):
        print(f"[音声合成] 空字幕行を除外: {len(raw_entries) - len(entries)}")

    segment_dir = en_mp3.parent / ".tts_segments"
    if segment_dir.exists():
        shutil.rmtree(segment_dir)
    segment_dir.mkdir(parents=True, exist_ok=True)

    resolved_tts_workers = resolve_tts_workers(tts_workers)

    try:
        unique_texts: list[str] = []
        segment_path_by_text: dict[str, Path] = {}
        segment_text_file_by_text: dict[str, Path] = {}
        for entry in entries:
            text_key = entry.text.strip()
            if text_key not in segment_path_by_text:
                segment_path = segment_dir / f"segment_unique_{len(unique_texts) + 1:05d}.mp3"
                text_file = segment_dir / f"segment_unique_{len(unique_texts) + 1:05d}.txt"
                segment_path_by_text[text_key] = segment_path
                text_file.write_text(text_key + "\n", encoding="utf-8")
                segment_text_file_by_text[text_key] = text_file
                unique_texts.append(text_key)

        print(
            "[音声合成] "
            f"subtitles={len(entries)}, unique_lines={len(unique_texts)}, tts_workers={resolved_tts_workers}"
        )

        if resolved_tts_workers == 1:
            for i, text_value in enumerate(unique_texts, start=1):
                synthesize_text_segment(
                    text_value,
                    segment_path_by_text[text_value],
                    segment_text_file_by_text[text_value],
                )
                if i < len(unique_texts):
                    sleep(0.15)
        else:
            with ThreadPoolExecutor(max_workers=resolved_tts_workers) as executor:
                futures = [
                    executor.submit(
                        synthesize_text_segment,
                        text_value,
                        segment_path_by_text[text_value],
                        segment_text_file_by_text[text_value],
                    )
                    for text_value in unique_texts
                ]
                for future in as_completed(futures):
                    future.result()

        segment_duration_by_text: dict[str, float] = {}
        probe_workers = max(1, min(8, resolved_tts_workers * 2, len(unique_texts)))
        if probe_workers == 1:
            for text_key in unique_texts:
                segment_duration_by_text[text_key] = get_media_duration_seconds(segment_path_by_text[text_key])
        else:
            with ThreadPoolExecutor(max_workers=probe_workers) as executor:
                future_by_text = {
                    executor.submit(get_media_duration_seconds, segment_path_by_text[text_key]): text_key
                    for text_key in unique_texts
                }
                for future in as_completed(future_by_text):
                    text_key = future_by_text[future]
                    segment_duration_by_text[text_key] = future.result()

        start_times_ms, adjusted_count, max_shift_seconds = build_tts_start_times(
            entries,
            segment_duration_by_text,
            tts_timing_mode,
        )
        if tts_timing_mode == "no-overlap":
            print(
                "[音声合成] "
                f"timing_mode=no-overlap, adjusted={adjusted_count}, max_shift={max_shift_seconds:.2f}s"
            )
            log_pipeline(
                f"INFO  tts_timing adjusted={adjusted_count}, max_shift={max_shift_seconds:.2f}s"
            )

        if (
            tts_timing_mode == "no-overlap"
            and subs_sync_mode == "tts-aligned"
            and aligned_srt_output is not None
        ):
            aligned_srt_text, first_shift_seconds, last_shift_seconds = build_aligned_srt_text(
                en_srt.read_text(encoding="utf-8"),
                entries,
                start_times_ms,
                segment_duration_by_text,
            )
            aligned_srt_output.write_text(aligned_srt_text, encoding="utf-8")
            print(f"[音声合成] 同期字幕を作成: {aligned_srt_output}")
            log_pipeline(
                "INFO  aligned_subtitles written "
                f"path={aligned_srt_output.name}, first_shift={first_shift_seconds:.2f}s, "
                f"last_shift={last_shift_seconds:.2f}s"
            )

        segment_files: list[tuple[int, Path]] = []
        for entry, start_ms in zip(entries, start_times_ms):
            text_key = entry.text.strip()
            segment_files.append((start_ms, segment_path_by_text[text_key]))

        command: list[str] = [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-t",
            f"{video_duration:.3f}",
            "-i",
            "anullsrc=r=24000:cl=mono",
        ]
        for _, segment_path in segment_files:
            command.extend(["-i", str(segment_path)])

        filter_parts: list[str] = []
        delayed_labels: list[str] = []
        for index, (start_ms, _) in enumerate(segment_files, start=1):
            label = f"d{index}"
            filter_parts.append(f"[{index}:a]aresample=24000,adelay={start_ms}|{start_ms}[{label}]")
            delayed_labels.append(f"[{label}]")

        mix_inputs = "[0:a]" + "".join(delayed_labels)
        filter_parts.append(
            f"{mix_inputs}amix=inputs={len(segment_files) + 1}:duration=longest:dropout_transition=0:normalize=0[mix]"
        )
        command.extend(
            [
                "-filter_complex",
                ";".join(filter_parts),
                "-map",
                "[mix]",
                "-c:a",
                "mp3",
                "-b:a",
                "128k",
                str(en_mp3),
            ]
        )

        run_command(command, "generate_en_mp3")
    finally:
        shutil.rmtree(segment_dir, ignore_errors=True)


def extract_xtts_reference_audio(input_video: Path, jp_reference_srt: Path, reference_audio_path: Path) -> None:
    jp_entries = [entry for entry in extract_srt_entries(jp_reference_srt.read_text(encoding="utf-8")) if entry.text.strip()]
    if jp_entries:
        first_entry = jp_entries[0]
        clip_start = max(0.0, first_entry.start_seconds - 1.0)
        clip_duration = min(
            XTTS_REFERENCE_SAMPLE_SECONDS,
            max(
                XTTS_REFERENCE_MIN_SECONDS,
                (first_entry.end_seconds - first_entry.start_seconds) + 4.0,
            ),
        )
    else:
        clip_start = 0.0
        clip_duration = XTTS_REFERENCE_SAMPLE_SECONDS

    run_command(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{clip_start:.3f}",
            "-t",
            f"{clip_duration:.3f}",
            "-i",
            str(input_video),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "22050",
            "-c:a",
            "pcm_s16le",
            str(reference_audio_path),
        ],
        "extract_xtts_reference_audio",
    )


def ensure_edge_tts() -> None:
    command = get_edge_tts_command()
    try:
        subprocess.run([*command, "--help"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise FileNotFoundError(
            "edge-tts が見つからないか実行できません。次を実行してください: python -m pip install edge-tts"
        ) from exc


def get_edge_tts_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--edge-tts"]
    edge_tts_bin = shutil.which("edge-tts")
    if edge_tts_bin:
        return [edge_tts_bin]
    return [sys.executable, "-m", "edge_tts"]


def run_command(command: list[str], step_name: str) -> None:
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        raise PipelineStepError(step_name, f"{step_name} failed") from exc


def run_command_with_retries(
    command: list[str],
    step_name: str,
    max_attempts: int = 4,
    initial_delay_seconds: float = 1.5,
) -> None:
    last_error: subprocess.CalledProcessError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            subprocess.run(command, check=True)
            return
        except subprocess.CalledProcessError as exc:
            last_error = exc
            if attempt >= max_attempts:
                break
            wait_seconds = initial_delay_seconds * (2 ** (attempt - 1))
            log_pipeline(
                f"WARN  {step_name} retry {attempt}/{max_attempts - 1} after command failure; waiting {wait_seconds:.1f}s"
            )
            sleep(wait_seconds)

    raise PipelineStepError(step_name, f"{step_name} failed after {max_attempts} attempts") from last_error


def run_command_with_output(command: list[str], step_name: str) -> str:
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise PipelineStepError(step_name, f"{step_name} failed") from exc
    return result.stdout.strip()


def sanitize_whisper_initial_prompt(prompt: str) -> str | None:
    terms: list[str] = []
    seen_terms: set[str] = set()
    keep_section_terms = False
    in_user_defined_section = False

    for raw_line in prompt.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("【") and line.endswith("】"):
            in_user_defined_section = line == "【ユーザー定義欄（蓄積）】"
            keep_section_terms = "短い例文" not in line
            continue
        if not keep_section_terms:
            continue

        normalized = line.replace("，", "、").replace(",", "、")
        if normalized.count("、") < 3 and not in_user_defined_section:
            continue

        for raw_term in normalized.split("、"):
            term = raw_term.strip(" 　,，、。")
            if not term or len(term) > 40:
                continue
            if term in seen_terms:
                continue
            seen_terms.add(term)
            terms.append(term)

    if not terms:
        return None

    safe_terms: list[str] = []
    current_length = 0
    for term in terms:
        added_length = len(term) if not safe_terms else len(term) + 1
        if current_length + added_length > WHISPER_INITIAL_PROMPT_MAX_CHARS:
            break
        safe_terms.append(term)
        current_length += added_length

    if not safe_terms:
        return None
    return "、".join(safe_terms)


def load_whisper_initial_prompt(prompt_file: Path | None) -> str | None:
    prompt_path = prompt_file if prompt_file is not None else DEFAULT_WHISPER_PROMPT_PATH
    if not prompt_path.exists():
        return None

    prompt = prompt_path.read_text(encoding="utf-8").strip()
    if not prompt:
        return None
    return sanitize_whisper_initial_prompt(prompt)


def skip_if_exists(output_path: Path, force: bool, message: str) -> bool:
    if output_path.exists() and not force:
        print(f"[skip] {message}: {output_path}")
        return True
    return False


def extract_audio(input_video: Path, audio_wav: Path, lowpass_cutoff_hz: float, force: bool) -> None:
    if skip_if_exists(audio_wav, force, "audio already exists"):
        return
    print(f"[音声抽出] {audio_wav}")
    filter_parts = [
        "loudnorm=I=-16",
        "highpass=f=100:poles=2",
    ]
    if lowpass_cutoff_hz > 0:
        filter_parts.append(f"lowpass=f={lowpass_cutoff_hz:g}:poles=2")
    filter_parts.append("acompressor=threshold=0.1:ratio=4:attack=0.01:release=0.1")
    audio_filters = ",".join(filter_parts)
    run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_video),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-af",
            audio_filters,
            "-c:a",
            "pcm_s16le",
            str(audio_wav),
        ],
        "extract_audio",
    )


def get_transcription_settings(config: PipelineConfig) -> TranscriptionSettings:
    if config.transcription_preset not in TRANSCRIPTION_PRESETS:
        raise PipelineStepError(
            "transcribe_audio",
            f"Unknown transcription preset: {config.transcription_preset}",
        )

    preset = TRANSCRIPTION_PRESETS[config.transcription_preset]
    if config.model_size:
        return TranscriptionSettings(
            model_size=config.model_size,
            compute_type=preset.compute_type,
            beam_size=preset.beam_size,
            best_of=preset.best_of,
            condition_on_previous_text=preset.condition_on_previous_text,
            temperature=preset.temperature,
        )
    return preset


def transcribe_audio(audio_wav: Path, jp_srt: Path, config: PipelineConfig) -> None:
    if skip_if_exists(jp_srt, config.force, "jp subtitle already exists"):
        return

    settings = get_transcription_settings(config)
    print(f"[文字起こし] {jp_srt}")
    print(
        "[文字起こし] "
        f"preset={config.transcription_preset}, model={settings.model_size}, "
        f"beam={settings.beam_size}, best_of={settings.best_of}"
    )
    from faster_whisper import WhisperModel

    transcription_workers = resolve_transcription_workers(config)
    transcription_cpu_threads = resolve_transcription_cpu_threads(config)
    subtitle_segments: list[tuple[float, float, str]] = []
    duration_seconds = get_media_duration_seconds(audio_wav)
    initial_prompt = load_whisper_initial_prompt(config.whisper_prompt_file)

    device, compute_type = resolve_transcription_runtime(settings, config)
    try:
        model = WhisperModel(
            settings.model_size,
            device=device,
            compute_type=compute_type,
            cpu_threads=transcription_cpu_threads,
            num_workers=transcription_workers,
        )
    except Exception as exc:
        if device == "cpu":
            raise
        print(f"[文字起こし] WARN device={device} を初期化できないため cpu にフォールバックします: {exc}")
        device = "cpu"
        compute_type = settings.compute_type
        model = WhisperModel(
            settings.model_size,
            device=device,
            compute_type=compute_type,
            cpu_threads=transcription_cpu_threads,
            num_workers=transcription_workers,
        )

    print(
        "[文字起こし] "
        f"workers={transcription_workers}, cpu_threads={transcription_cpu_threads}"
    )
    print(f"[文字起こし] device={device}, compute_type={compute_type}")
    print(f"[文字起こし] initial_prompt={'on' if initial_prompt else 'off'}")
    if device == "cpu" and config.transcription_preset == "high-accuracy" and duration_seconds >= 1800:
        print(
            "[文字起こし] WARN 長尺 + high-accuracy + CPU のため非常に時間がかかる可能性があります。"
            " 速度重視なら --transcription-preset balanced か fast を検討してください"
        )

    if config.chunk_seconds > 0 and duration_seconds > config.chunk_seconds:
        print(
            "[文字起こし] 長尺音声を自動分割します: "
            f"duration={duration_seconds:.1f}s, chunk={config.chunk_seconds:.1f}s, "
            f"overlap={config.chunk_overlap_seconds:.1f}s"
        )
        subtitle_segments = transcribe_audio_in_chunks(
            audio_wav,
            model,
            settings,
            duration_seconds,
            config,
            initial_prompt,
        )
    else:
        subtitle_segments = transcribe_audio_segments(
            model=model,
            audio_path=audio_wav,
            settings=settings,
            offset_seconds=0.0,
            initial_prompt=initial_prompt,
        )

    subtitle_segments = collapse_repeated_subtitle_segments(subtitle_segments)

    subtitle_blocks: list[str] = []
    for start_seconds, end_seconds, text in subtitle_segments:
        if not text:
            continue
        subtitle_blocks.append(
            "\n".join(
                [
                    str(len(subtitle_blocks) + 1),
                    f"{format_timestamp(start_seconds)} --> {format_timestamp(end_seconds)}",
                    text,
                ]
            )
        )

    jp_srt.write_text("\n\n".join(subtitle_blocks) + ("\n" if subtitle_blocks else ""), encoding="utf-8")


def collapse_repeated_subtitle_segments(
    segments: list[tuple[float, float, str]],
    max_gap_seconds: float = 1.0,
) -> list[tuple[float, float, str]]:
    if not segments:
        return []

    collapsed: list[tuple[float, float, str]] = []
    current_start, current_end, current_text = segments[0]

    for start_seconds, end_seconds, text in segments[1:]:
        if text == current_text and start_seconds - current_end <= max_gap_seconds:
            current_end = max(current_end, end_seconds)
            continue

        collapsed.append((current_start, current_end, current_text))
        current_start, current_end, current_text = start_seconds, end_seconds, text

    collapsed.append((current_start, current_end, current_text))

    if len(collapsed) != len(segments):
        print(f"[文字起こし] 連続重複字幕を圧縮: {len(segments)} -> {len(collapsed)}")

    return collapsed


def transcribe_audio_segments(
    model,
    audio_path: Path,
    settings: TranscriptionSettings,
    offset_seconds: float,
    initial_prompt: str | None,
) -> list[tuple[float, float, str]]:
    segments, _info = model.transcribe(
        str(audio_path),
        language="ja",
        beam_size=settings.beam_size,
        best_of=settings.best_of,
        condition_on_previous_text=settings.condition_on_previous_text,
        temperature=settings.temperature,
        vad_filter=True,
        initial_prompt=initial_prompt,
    )

    results: list[tuple[float, float, str]] = []
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        results.append((segment.start + offset_seconds, segment.end + offset_seconds, text))
    return results


def resolve_transcription_workers(config: PipelineConfig) -> int:
    if config.transcription_workers > 0:
        return config.transcription_workers

    cpu_count = os.cpu_count() or 1
    if cpu_count >= 16:
        return 4
    if cpu_count >= 10:
        return 3
    if cpu_count >= 6:
        return 2
    return 1


def resolve_transcription_cpu_threads(config: PipelineConfig) -> int:
    if config.transcription_cpu_threads > 0:
        return config.transcription_cpu_threads
    cpu_count = os.cpu_count() or 1
    workers = resolve_transcription_workers(config)
    return max(1, cpu_count // workers)


def resolve_transcription_max_pending_chunks(config: PipelineConfig) -> int:
    workers = resolve_transcription_workers(config)
    if config.transcription_max_pending_chunks > 0:
        return max(workers, config.transcription_max_pending_chunks)
    return max(workers, workers * 2)


def resolve_transcription_runtime(
    settings: TranscriptionSettings,
    config: PipelineConfig,
) -> tuple[str, str]:
    device = config.transcription_device.strip().lower() if config.transcription_device else "cpu"
    if device not in {"cpu", "cuda", "auto"}:
        raise PipelineStepError(
            "transcribe_audio",
            f"Unknown transcription device: {config.transcription_device}",
        )

    if config.transcription_compute_type:
        return device, config.transcription_compute_type

    # non-CPU devices are typically faster with float16 than float32.
    if device in {"cuda", "auto"} and settings.compute_type == "float32":
        return device, "float16"
    return device, settings.compute_type


def resolve_tts_workers(tts_workers: int) -> int:
    if tts_workers > 0:
        return tts_workers

    cpu_count = os.cpu_count() or 1
    # edge-tts は過度な並列で 503 が増えやすいため、自動設定は控えめにする。
    if cpu_count >= 16:
        return 3
    if cpu_count >= 8:
        return 2
    return 1


def build_tts_start_times(
    entries,
    segment_duration_by_text: dict[str, float],
    tts_timing_mode: str,
) -> tuple[list[int], int, float]:
    if tts_timing_mode not in {"strict", "no-overlap"}:
        raise PipelineStepError("generate_en_mp3", f"Unknown tts timing mode: {tts_timing_mode}")

    start_times_ms: list[int] = []
    previous_end_seconds = 0.0
    adjusted_count = 0
    max_shift_seconds = 0.0

    for entry in entries:
        text_key = entry.text.strip()
        segment_duration = segment_duration_by_text.get(text_key, 0.0)
        original_start = max(0.0, entry.start_seconds)
        scheduled_start = original_start

        if tts_timing_mode == "no-overlap" and scheduled_start < previous_end_seconds:
            scheduled_start = previous_end_seconds

        shift_seconds = max(0.0, scheduled_start - original_start)
        if shift_seconds > 0.0:
            adjusted_count += 1
            max_shift_seconds = max(max_shift_seconds, shift_seconds)

        start_times_ms.append(max(0, int(round(scheduled_start * 1000.0))))
        previous_end_seconds = max(previous_end_seconds, scheduled_start + segment_duration)

    return start_times_ms, adjusted_count, max_shift_seconds


def build_aligned_srt_text(
    source_srt_text: str,
    entries,
    start_times_ms: list[int],
    segment_duration_by_text: dict[str, float],
    min_duration_seconds: float = 0.20,
    min_gap_seconds: float = 0.01,
) -> tuple[str, float, float]:
    if len(entries) != len(start_times_ms):
        raise PipelineStepError(
            "generate_en_mp3",
            "aligned subtitle generation failed: entries/start_times length mismatch",
        )

    rendered_blocks: list[str] = []
    entry_index = 0
    previous_end = 0.0
    first_shift_seconds = 0.0
    last_shift_seconds = 0.0

    for block_lines in split_srt_blocks(source_srt_text):
        block = parse_srt_block(block_lines)
        text_key = " ".join(line.strip() for line in block.text_lines if line.strip())
        if not text_key:
            rendered_blocks.append(render_srt_block(block))
            continue

        if entry_index >= len(entries):
            raise PipelineStepError(
                "generate_en_mp3",
                "aligned subtitle generation failed: source srt blocks mismatch",
            )

        entry = entries[entry_index]
        start_seconds = max(previous_end, max(0.0, start_times_ms[entry_index] / 1000.0))
        segment_duration = max(min_duration_seconds, segment_duration_by_text.get(entry.text.strip(), 0.0))
        end_seconds = max(start_seconds + min_duration_seconds, start_seconds + segment_duration)

        shift_seconds = max(0.0, start_seconds - entry.start_seconds)
        if entry_index == 0:
            first_shift_seconds = shift_seconds
        last_shift_seconds = shift_seconds

        previous_end = end_seconds + min_gap_seconds
        aligned_timecode = f"{format_timestamp(start_seconds)} --> {format_timestamp(end_seconds)}"
        rendered_blocks.append(render_srt_block(SRTBlock(block.index, aligned_timecode, block.text_lines)))
        entry_index += 1

    if entry_index != len(entries):
        raise PipelineStepError(
            "generate_en_mp3",
            "aligned subtitle generation failed: not all entries were consumed",
        )

    return "\n\n".join(rendered_blocks) + ("\n" if rendered_blocks else ""), first_shift_seconds, last_shift_seconds


def iter_transcription_chunks(
    audio_wav: Path,
    duration_seconds: float,
    config: PipelineConfig,
    chunk_dir: Path,
) -> list[TranscriptionChunk]:
    overlap = max(0.0, config.chunk_overlap_seconds)
    chunk_seconds = config.chunk_seconds
    chunks: list[TranscriptionChunk] = []
    chunk_index = 0

    while True:
        core_start = chunk_index * chunk_seconds
        if core_start >= duration_seconds:
            break
        core_end = min(duration_seconds, (chunk_index + 1) * chunk_seconds)
        chunk_start = max(0.0, core_start - overlap)
        chunk_end = min(duration_seconds, core_end + overlap)
        chunk_duration = max(0.1, chunk_end - chunk_start)
        chunk_path = chunk_dir / f"chunk_{chunk_index:04d}.wav"
        chunks.append(
            TranscriptionChunk(
                index=chunk_index,
                core_start=core_start,
                core_end=core_end,
                chunk_start=chunk_start,
                chunk_end=chunk_end,
                chunk_duration=chunk_duration,
                chunk_path=chunk_path,
            )
        )
        chunk_index += 1

    return chunks


def extract_transcription_chunk(audio_wav: Path, chunk: TranscriptionChunk) -> None:
    run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(audio_wav),
            "-ss",
            f"{chunk.chunk_start:.3f}",
            "-t",
            f"{chunk.chunk_duration:.3f}",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(chunk.chunk_path),
        ],
        "split_audio_for_transcription",
    )


def filter_chunk_segments(
    chunk_segments: list[tuple[float, float, str]],
    chunk: TranscriptionChunk,
    duration_seconds: float,
) -> list[tuple[float, float, str]]:
    filtered_segments: list[tuple[float, float, str]] = []
    for start_seconds, end_seconds, text in chunk_segments:
        if chunk.index == 0:
            in_core = start_seconds < chunk.core_end
        elif chunk.core_end >= duration_seconds:
            in_core = start_seconds >= chunk.core_start
        else:
            in_core = chunk.core_start <= start_seconds < chunk.core_end
        if in_core:
            filtered_segments.append((start_seconds, end_seconds, text))
    return filtered_segments


def transcribe_chunk(
    model,
    settings: TranscriptionSettings,
    chunk: TranscriptionChunk,
    duration_seconds: float,
    initial_prompt: str | None,
) -> list[tuple[float, float, str]]:
    chunk_segments = transcribe_audio_segments(
        model=model,
        audio_path=chunk.chunk_path,
        settings=settings,
        offset_seconds=chunk.chunk_start,
        initial_prompt=initial_prompt,
    )
    return filter_chunk_segments(chunk_segments, chunk, duration_seconds)


def collect_transcription_result(
    pending_futures: dict[Future, TranscriptionChunk],
    results_by_index: dict[int, list[tuple[float, float, str]]],
) -> None:
    done, _ = wait(set(pending_futures), return_when=FIRST_COMPLETED)
    for future in done:
        chunk = pending_futures.pop(future)
        try:
            results_by_index[chunk.index] = future.result()
        finally:
            chunk.chunk_path.unlink(missing_ok=True)
        print(
            "[文字起こし] "
            f"chunk {chunk.index + 1}: core={chunk.core_start:.1f}-{chunk.core_end:.1f}s "
            f"window={chunk.chunk_start:.1f}-{chunk.chunk_end:.1f}s"
        )


def transcribe_audio_in_chunks(
    audio_wav: Path,
    model,
    settings: TranscriptionSettings,
    duration_seconds: float,
    config: PipelineConfig,
    initial_prompt: str | None,
) -> list[tuple[float, float, str]]:
    chunk_dir = audio_wav.parent / ".asr_chunks"
    if chunk_dir.exists():
        shutil.rmtree(chunk_dir)
    chunk_dir.mkdir(parents=True, exist_ok=True)

    transcription_workers = resolve_transcription_workers(config)
    max_pending_chunks = resolve_transcription_max_pending_chunks(config)
    results_by_index: dict[int, list[tuple[float, float, str]]] = {}
    chunks = iter_transcription_chunks(audio_wav, duration_seconds, config, chunk_dir)

    try:
        print(
            "[文字起こし] "
            f"parallel_chunks={transcription_workers}, max_pending_chunks={max_pending_chunks}"
        )
        with ThreadPoolExecutor(max_workers=transcription_workers) as executor:
            pending_futures: dict[Future, TranscriptionChunk] = {}
            for chunk in chunks:
                extract_transcription_chunk(audio_wav, chunk)
                future = executor.submit(
                    transcribe_chunk,
                    model,
                    settings,
                    chunk,
                    duration_seconds,
                    initial_prompt,
                )
                pending_futures[future] = chunk
                while len(pending_futures) >= max_pending_chunks:
                    collect_transcription_result(pending_futures, results_by_index)

            while pending_futures:
                collect_transcription_result(pending_futures, results_by_index)

    finally:
        shutil.rmtree(chunk_dir, ignore_errors=True)

    merged_segments: list[tuple[float, float, str]] = []
    for chunk_index in sorted(results_by_index):
        merged_segments.extend(results_by_index[chunk_index])
    merged_segments.sort(key=lambda item: item[0])
    return merged_segments


def ensure_argos_ja_en() -> None:
    import argostranslate.package

    installed = argostranslate.package.get_installed_packages()
    if any(package.from_code == "ja" and package.to_code == "en" for package in installed):
        return

    print("[Argos] ja->en モデルをダウンロードしています")
    argostranslate.package.update_package_index()
    available_packages = argostranslate.package.get_available_packages()
    package = next(
        (candidate for candidate in available_packages if candidate.from_code == "ja" and candidate.to_code == "en"),
        None,
    )
    if package is None:
        raise RuntimeError("Could not find an Argos Translate ja->en package")

    download_path = package.download()
    argostranslate.package.install_from_path(download_path)


def get_deepl_api_key(config_value: str | None) -> str:
    if config_value:
        return config_value
    env_value = os.getenv("DEEPL_API_KEY")
    if env_value:
        return env_value
    raise RuntimeError(
        "DeepL を使うには --deepl-api-key か環境変数 DEEPL_API_KEY を設定してください"
    )


def get_gemini_api_key(config_value: str | None) -> str:
    if config_value:
        return config_value
    env_value = os.getenv("GEMINI_API_KEY")
    if env_value:
        return env_value
    raise RuntimeError(
        "Gemini を使うには --gemini-api-key か環境変数 GEMINI_API_KEY を設定してください"
    )


def iterate_exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        chain.append(current)
        if current.__cause__ is not None:
            current = current.__cause__
            continue
        current = current.__context__
    return chain


def has_http_status(exc: BaseException, statuses: set[int]) -> bool:
    for item in iterate_exception_chain(exc):
        response = getattr(item, "response", None)
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int) and status_code in statuses:
            return True
    return False


def build_exception_summary(exc: BaseException, *, response_text_limit: int = 400) -> str:
    lines: list[str] = []
    for index, item in enumerate(iterate_exception_chain(exc), start=1):
        item_type = item.__class__.__name__
        item_message = str(item).strip()
        lines.append(f"[{index}] {item_type}: {item_message}")

        response = getattr(item, "response", None)
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            lines.append(f"[{index}] status_code={status_code}")

        response_text = getattr(response, "text", None)
        if isinstance(response_text, str) and response_text.strip():
            normalized = " ".join(response_text.strip().split())
            if len(normalized) > response_text_limit:
                normalized = normalized[:response_text_limit] + "..."
            lines.append(f"[{index}] response_body={normalized}")

    return "\n".join(lines)


def is_gemini_limit_error(exc: BaseException) -> bool:
    if has_http_status(exc, {429}):
        return True

    message = "\n".join(str(item) for item in iterate_exception_chain(exc)).lower()
    keywords = [
        "resource_exhausted",
        "resource has been exhausted",
        "quota",
        "rate limit",
        "too many requests",
        "exceeded",
    ]
    return any(keyword in message for keyword in keywords)


def is_gemini_alignment_error(exc: BaseException) -> bool:
    message = "\n".join(str(item) for item in iterate_exception_chain(exc)).lower()
    keywords = [
        "応答件数が入力件数と一致しません",
        "応答 id が不正です",
        "翻訳行が不足しています",
        "jsondecodeerror",
        "expecting value",
        "unterminated",
        "extra data",
    ]
    return any(keyword in message for keyword in keywords)


def is_deepl_limit_error(exc: BaseException) -> bool:
    # DeepL: 429 (rate limit), 456 (quota exceeded)
    if has_http_status(exc, {429, 456}):
        return True

    message = "\n".join(str(item) for item in iterate_exception_chain(exc)).lower()
    keywords = [
        "quota exceeded",
        "too many requests",
        "rate limit",
        "429",
        "456",
    ]
    return any(keyword in message for keyword in keywords)


def get_gemini_fallback_models(primary_model: str) -> list[str]:
    raw = os.getenv("GEMINI_FALLBACK_MODELS", "").strip()
    if not raw:
        raw_models = GEMINI_FALLBACK_MODELS_DEFAULT
    else:
        raw_models = [part.strip() for part in raw.split(",")]

    models: list[str] = []
    seen: set[str] = {primary_model}
    for model in raw_models:
        if not model or model in seen:
            continue
        seen.add(model)
        models.append(model)
    return models


def normalize_gemini_model_name(name: str) -> str:
    return name[len("models/") :] if name.startswith("models/") else name


def fetch_available_gemini_models(gemini_api_key: str) -> set[str] | None:
    global _GEMINI_AVAILABLE_MODELS_CACHE
    if _GEMINI_AVAILABLE_MODELS_CACHE is not None:
        return _GEMINI_AVAILABLE_MODELS_CACHE

    if not read_bool_env("GEMINI_DISCOVER_MODELS", GEMINI_DISCOVER_MODELS_DEFAULT):
        return None

    try:
        import requests

        discovered: set[str] = set()
        page_token: str | None = None
        for _ in range(5):
            params: dict[str, str | int] = {"key": gemini_api_key, "pageSize": 100}
            if page_token:
                params["pageToken"] = page_token

            response = requests.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                params=params,
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()

            for model in payload.get("models", []):
                methods = model.get("supportedGenerationMethods") or []
                if "generateContent" not in methods:
                    continue
                raw_name = str(model.get("name", "")).strip()
                if not raw_name:
                    continue
                normalized = normalize_gemini_model_name(raw_name)
                if not normalized.startswith("gemini"):
                    continue
                discovered.add(normalized)

            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        if discovered:
            _GEMINI_AVAILABLE_MODELS_CACHE = discovered
        return discovered or None
    except Exception:
        return None


def gemini_model_rank(model_name: str) -> tuple[int, int, str]:
    name = model_name.lower()
    if "pro" in name:
        tier = 0
    elif "flash" in name and "lite" not in name:
        tier = 1
    elif "lite" in name:
        tier = 2
    else:
        tier = 3

    m = re.search(r"gemini-(\d+)(?:\.(\d+))?", name)
    if m:
        major = int(m.group(1))
        minor = int(m.group(2) or "0")
    else:
        major = 0
        minor = 0

    # Higher version first.
    return (tier, -major * 1000 - minor, model_name)


def resolve_gemini_model_candidates(primary_model: str, gemini_api_key: str) -> list[str]:
    configured = [primary_model, *get_gemini_fallback_models(primary_model)]
    discovered = fetch_available_gemini_models(gemini_api_key)
    if not discovered:
        return configured

    resolved: list[str] = [primary_model]
    seen: set[str] = {primary_model}

    for model in configured[1:]:
        if model in seen or model not in discovered:
            continue
        seen.add(model)
        resolved.append(model)

    extras = sorted((m for m in discovered if m not in seen), key=gemini_model_rank)
    for model in extras:
        seen.add(model)
        resolved.append(model)

    return resolved


def translate_with_argos(source_text: str) -> str:
    ensure_argos_ja_en()
    import argostranslate.translate

    def translate_line(line: str) -> str:
        return argostranslate.translate.translate(line, "ja", "en")

    return translate_srt_text_lines(source_text, translate_line)


def translate_with_deepl(source_text: str, deepl_api_key: str) -> str:
    import requests

    headers = {
        "Authorization": f"DeepL-Auth-Key {deepl_api_key}",
    }

    translation_cache: dict[str, str] = {}

    unique_lines: list[str] = []
    seen_lines: set[str] = set()
    for block_lines in split_srt_blocks(source_text):
        block = parse_srt_block(block_lines)
        for line in block.text_lines:
            stripped = line.strip()
            if not stripped or stripped in seen_lines:
                continue
            seen_lines.add(stripped)
            unique_lines.append(stripped)

    pending_lines = [line for line in unique_lines if line not in translation_cache]
    batch: list[str] = []
    batch_chars = 0

    def flush_batch() -> None:
        nonlocal batch, batch_chars
        if not batch:
            return

        request_data: list[tuple[str, str]] = [
            ("source_lang", "JA"),
            ("target_lang", "EN"),
        ]
        request_data.extend(("text", text) for text in batch)

        response = requests.post(
            "https://api-free.deepl.com/v2/translate",
            data=request_data,
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        translations = data.get("translations", [])

        if len(translations) != len(batch):
            raise RuntimeError("DeepL の応答件数が入力件数と一致しません")

        for source_line, translated_item in zip(batch, translations):
            translated_text = translated_item.get("text", "").strip()
            translation_cache[source_line] = translated_text or source_line

        batch = []
        batch_chars = 0

    for line in pending_lines:
        line_chars = len(line)
        if batch and (
            len(batch) >= DEEPL_BATCH_MAX_TEXTS
            or batch_chars + line_chars > DEEPL_BATCH_MAX_CHARS
        ):
            flush_batch()
        batch.append(line)
        batch_chars += line_chars
    flush_batch()

    rendered_blocks: list[str] = []
    for block_lines in split_srt_blocks(source_text):
        block = parse_srt_block(block_lines)
        translated_lines: list[str] = []
        for line in block.text_lines:
            stripped = line.strip()
            if not stripped:
                translated_lines.append("")
                continue
            translated_lines.append(translation_cache.get(stripped, stripped))
        rendered_blocks.append(render_srt_block(SRTBlock(block.index, block.timecode, translated_lines)))

    return "\n\n".join(rendered_blocks) + ("\n" if rendered_blocks else "")


def translate_lines_with_gemini(
    lines: list[str],
    gemini_api_key: str,
    gemini_model: str,
    _split_depth: int = 0,
) -> dict[str, str]:
    import requests

    indexed_lines = [f"{idx}\t{text}" for idx, text in enumerate(lines, start=1)]
    prompt = (
        "Translate each Japanese line into natural English.\n"
        "Do not omit any line.\n"
        "Preserve numbers, math symbols, and variable names as-is when possible.\n"
        "Return JSON only with this exact schema:\n"
        "{\"translations\":[{\"id\":1,\"text\":\"...\"}]}\n"
        "IDs must match input IDs.\n"
        "Input:\n"
        + "\n".join(indexed_lines)
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent"
    params = {"key": gemini_api_key}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }

    last_error: Exception | None = None
    for attempt in range(1, 6):
        try:
            response = requests.post(url, params=params, json=payload, timeout=60)
            response.raise_for_status()
            data = response.json()

            candidates = data.get("candidates", [])
            if not candidates:
                raise RuntimeError("Gemini の応答に candidates がありません")
            content = candidates[0].get("content", {})
            parts = content.get("parts", [])
            if not parts:
                raise RuntimeError("Gemini の応答に parts がありません")
            text_payload = parts[0].get("text", "")
            parsed = json.loads(text_payload)

            items = parsed.get("translations", [])

            translated_by_id: dict[int, str] = {}
            for item in items:
                line_id_raw = item.get("id")
                line_id = int(line_id_raw)
                if line_id < 1 or line_id > len(lines):
                    raise RuntimeError("Gemini の応答 ID が不正です")
                if line_id in translated_by_id:
                    continue
                translated_text = str(item.get("text", "")).strip()
                translated_by_id[line_id] = translated_text or lines[line_id - 1]

            if len(translated_by_id) != len(lines):
                raise RuntimeError("Gemini の応答件数が入力件数と一致しません")

            translated_by_line: dict[str, str] = {}
            for line_id, source_line in enumerate(lines, start=1):
                translated_by_line[source_line] = translated_by_id[line_id]

            return translated_by_line
        except Exception as exc:
            last_error = exc
            print(
                "[翻訳] "
                f"gemini request failed: model={gemini_model}, attempt={attempt}/5"
            )
            print("[翻訳] gemini error detail:\n" + build_exception_summary(exc))
            if attempt >= 5:
                break
            sleep(1.5 * (2 ** (attempt - 1)))

    if last_error is not None and len(lines) >= 2 and is_gemini_alignment_error(last_error):
        midpoint = len(lines) // 2
        left = lines[:midpoint]
        right = lines[midpoint:]
        print(
            "[翻訳] "
            f"gemini split retry: depth={_split_depth}, size={len(lines)} -> "
            f"{len(left)} + {len(right)}"
        )

        translated_left = translate_lines_with_gemini(
            left,
            gemini_api_key,
            gemini_model,
            _split_depth=_split_depth + 1,
        )
        translated_right = translate_lines_with_gemini(
            right,
            gemini_api_key,
            gemini_model,
            _split_depth=_split_depth + 1,
        )
        merged = dict(translated_left)
        merged.update(translated_right)
        return merged

    raise RuntimeError("Gemini 翻訳 API の呼び出しに失敗しました") from last_error


def translate_with_gemini(source_text: str, gemini_api_key: str, gemini_model: str) -> str:
    translation_cache: dict[str, str] = {}

    unique_lines: list[str] = []
    seen_lines: set[str] = set()
    for block_lines in split_srt_blocks(source_text):
        block = parse_srt_block(block_lines)
        for line in block.text_lines:
            stripped = line.strip()
            if not stripped or stripped in seen_lines:
                continue
            seen_lines.add(stripped)
            unique_lines.append(stripped)

    pending_lines = [line for line in unique_lines if line not in translation_cache]
    if pending_lines:
        force_batched = len(pending_lines) > GEMINI_ONE_SHOT_MAX_TEXTS
        total_chars = sum(len(line) for line in pending_lines)
        if not force_batched:
            try:
                print(
                    "[翻訳] "
                    f"gemini one-shot attempt: lines={len(pending_lines)}, chars={total_chars}"
                )
                translated_by_line = translate_lines_with_gemini(
                    pending_lines,
                    gemini_api_key,
                    gemini_model,
                )
                translation_cache.update(translated_by_line)
                print("[翻訳] gemini one-shot success")
            except Exception as exc:
                print(
                    "[翻訳] "
                    "gemini one-shot failed; fallback to batched requests"
                )
                print("[翻訳] gemini one-shot error detail:\n" + build_exception_summary(exc))
                force_batched = True

        if force_batched:
            if len(pending_lines) > GEMINI_ONE_SHOT_MAX_TEXTS:
                print(
                    "[翻訳] "
                    f"skip gemini one-shot: lines={len(pending_lines)} exceeds "
                    f"GEMINI_ONE_SHOT_MAX_TEXTS={GEMINI_ONE_SHOT_MAX_TEXTS}"
                )
            batch: list[str] = []
            batch_chars = 0

            def flush_batch() -> None:
                nonlocal batch, batch_chars
                if not batch:
                    return
                translated_batch = translate_lines_with_gemini(batch, gemini_api_key, gemini_model)
                translation_cache.update(translated_batch)
                batch = []
                batch_chars = 0

            for line in pending_lines:
                line_chars = len(line)
                if batch and (
                    len(batch) >= GEMINI_BATCH_MAX_TEXTS
                    or batch_chars + line_chars > GEMINI_BATCH_MAX_CHARS
                ):
                    flush_batch()
                batch.append(line)
                batch_chars += line_chars
            flush_batch()

    rendered_blocks: list[str] = []
    for block_lines in split_srt_blocks(source_text):
        block = parse_srt_block(block_lines)
        translated_lines: list[str] = []
        for line in block.text_lines:
            stripped = line.strip()
            if not stripped:
                translated_lines.append("")
                continue
            translated_lines.append(translation_cache.get(stripped, stripped))
        rendered_blocks.append(render_srt_block(SRTBlock(block.index, block.timecode, translated_lines)))

    return "\n\n".join(rendered_blocks) + ("\n" if rendered_blocks else "")


def translate_jp_srt(
    jp_srt: Path,
    en_srt: Path,
    force: bool,
    translation_backend: str,
    deepl_api_key: str | None,
    gemini_api_key: str | None,
    gemini_model: str,
) -> None:
    if skip_if_exists(en_srt, force, "en subtitle already exists"):
        return

    print(f"[翻訳] {en_srt}")
    print(f"[翻訳] backend={translation_backend}")

    source_text = jp_srt.read_text(encoding="utf-8")

    if translation_backend == "argos":
        translated = translate_with_argos(source_text)
    elif translation_backend == "deepl":
        try:
            translated = translate_with_deepl(source_text, get_deepl_api_key(deepl_api_key))
        except Exception as deepl_exc:
            if not is_deepl_limit_error(deepl_exc):
                raise
            print("[翻訳] DeepL limit reached; fallback to Argos")
            translated = translate_with_argos(source_text)
    elif translation_backend == "gemini":
        gemini_key = get_gemini_api_key(gemini_api_key)
        gemini_models = resolve_gemini_model_candidates(gemini_model, gemini_key)
        print(f"[翻訳] gemini candidate models={gemini_models}")

        gemini_last_exc: Exception | None = None
        translated = None
        for model_index, current_model in enumerate(gemini_models):
            try:
                if model_index > 0:
                    print(f"[翻訳] switch gemini model -> {current_model}")
                translated = translate_with_gemini(
                    source_text,
                    gemini_key,
                    current_model,
                )
                break
            except Exception as gemini_exc:
                gemini_last_exc = gemini_exc
                print(
                    "[翻訳] "
                    f"gemini model failed: model={current_model}\n"
                    + build_exception_summary(gemini_exc)
                )
                if is_gemini_limit_error(gemini_exc) and model_index + 1 < len(gemini_models):
                    print(f"[翻訳] Gemini limit reached on model={current_model}; try next model")
                    continue
                if not is_gemini_limit_error(gemini_exc):
                    raise
                # limit error and no more Gemini models
                break

        if translated is None:
            if gemini_last_exc is not None and not is_gemini_limit_error(gemini_last_exc):
                raise gemini_last_exc

            print("[翻訳] Gemini limit reached; fallback to DeepL")
            try:
                translated = translate_with_deepl(source_text, get_deepl_api_key(deepl_api_key))
            except Exception as deepl_exc:
                if not is_deepl_limit_error(deepl_exc):
                    # DeepL key missing/unavailable or other non-limit failures: fallback to Argos.
                    print("[翻訳] DeepL unavailable; fallback to Argos")
                    translated = translate_with_argos(source_text)
                else:
                    print("[翻訳] DeepL limit reached; fallback to Argos")
                    translated = translate_with_argos(source_text)
    else:
        raise RuntimeError(f"Unknown translation backend: {translation_backend}")

    en_srt.write_text(translated, encoding="utf-8")


def _build_srt_from_plain_lines(lines: list[str]) -> str:
    blocks: list[str] = []
    for index, line in enumerate(lines, start=1):
        blocks.append(
            "\n".join(
                [
                    str(index),
                    "00:00:00,000 --> 00:00:01,000",
                    line,
                ]
            )
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _extract_plain_lines_from_srt(srt_text: str) -> list[str]:
    lines: list[str] = []
    for block_lines in split_srt_blocks(srt_text):
        block = parse_srt_block(block_lines)
        text = " ".join([line.strip() for line in block.text_lines if line.strip()]).strip()
        lines.append(text)
    return lines


def translate_plain_lines(
    lines: list[str],
    translation_backend: str,
    deepl_api_key: str | None,
    gemini_api_key: str | None,
    gemini_model: str,
) -> dict[str, str]:
    unique_lines: list[str] = []
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        unique_lines.append(stripped)

    if not unique_lines:
        return {}

    source_srt = _build_srt_from_plain_lines(unique_lines)

    if translation_backend == "argos":
        translated_srt = translate_with_argos(source_srt)
    elif translation_backend == "deepl":
        translated_srt = translate_with_deepl(source_srt, get_deepl_api_key(deepl_api_key))
    elif translation_backend == "gemini":
        gemini_key = get_gemini_api_key(gemini_api_key)
        gemini_models = resolve_gemini_model_candidates(gemini_model, gemini_key)
        translated_srt = None
        gemini_last_exc: Exception | None = None

        for current_model in gemini_models:
            try:
                translated_srt = translate_with_gemini(source_srt, gemini_key, current_model)
                break
            except Exception as gemini_exc:
                gemini_last_exc = gemini_exc
                if is_gemini_limit_error(gemini_exc):
                    continue
                raise

        if translated_srt is None:
            if gemini_last_exc is not None and not is_gemini_limit_error(gemini_last_exc):
                raise gemini_last_exc
            try:
                translated_srt = translate_with_deepl(source_srt, get_deepl_api_key(deepl_api_key))
            except Exception:
                translated_srt = translate_with_argos(source_srt)
    else:
        raise RuntimeError(f"Unknown translation backend: {translation_backend}")

    translated_lines = _extract_plain_lines_from_srt(translated_srt)
    if len(translated_lines) != len(unique_lines):
        raise RuntimeError("動画内テキスト翻訳の件数が一致しません")

    mapping: dict[str, str] = {}
    for source_line, translated_line in zip(unique_lines, translated_lines):
        mapping[source_line] = translated_line.strip() or source_line
    return mapping


def overlay_video_text(
    input_video: Path,
    output_video: Path,
    report_json: Path,
    translation_backend: str,
    deepl_api_key: str | None,
    gemini_api_key: str | None,
    gemini_model: str,
    sample_fps: float,
    min_chars: int,
    min_confidence: float,
    font_family: str,
    force: bool,
) -> None:
    if skip_if_exists(output_video, force, "video text overlay already exists"):
        return

    regions = detect_video_text_regions(
        input_video,
        report_json,
        sample_fps=sample_fps,
        min_chars=min_chars,
        min_confidence=min_confidence,
    )
    print(f"[動画内テキスト] detected_regions={len(regions)}")

    if not regions:
        shutil.copy2(input_video, output_video)
        return

    source_lines = [str(item["text"]) for item in regions]
    translations = translate_plain_lines(
        source_lines,
        translation_backend,
        deepl_api_key,
        gemini_api_key,
        gemini_model,
    )
    print(
        "[動画内テキスト] "
        f"translation_candidates={len(source_lines)}, unique={len(translations)}"
    )

    overlay_translated_regions(
        input_video=input_video,
        output_video=output_video,
        regions=regions,
        translations=translations,
        font_family=font_family,
    )


def write_en_txt(en_srt: Path, en_txt: Path, force: bool) -> None:
    if skip_if_exists(en_txt, force, "en text already exists"):
        return

    print(f"[テキスト生成] {en_txt}")
    from .srt_utils import parse_srt_block, split_srt_blocks

    subtitle_lines: list[str] = []
    for block_lines in split_srt_blocks(en_srt.read_text(encoding="utf-8")):
        block = parse_srt_block(block_lines)
        text_lines = [line.strip() for line in block.text_lines if line.strip()]
        if text_lines:
            subtitle_lines.append(" ".join(text_lines))

    en_txt.write_text("\n".join(subtitle_lines) + ("\n" if subtitle_lines else ""), encoding="utf-8")


def get_media_duration_seconds(media_path: Path) -> float:
    output = run_command_with_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(media_path),
        ],
        "probe_duration",
    )
    try:
        return float(output)
    except ValueError as exc:
        raise PipelineStepError("probe_duration", f"Could not parse duration for {media_path}") from exc


def generate_silence_mp3(output_path: Path, duration_seconds: float) -> None:
    run_command(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-t",
            f"{duration_seconds:.3f}",
            "-i",
            "anullsrc=r=24000:cl=mono",
            "-c:a",
            "mp3",
            "-b:a",
            "128k",
            str(output_path),
        ],
        "generate_en_mp3",
    )


def generate_en_mp3_with_edge_tts(
    en_srt: Path,
    input_video: Path,
    en_mp3: Path,
    voice: str,
    tts_volume: str,
    tts_workers: int,
    tts_timing_mode: str,
    subs_sync_mode: str,
    aligned_srt_output: Path | None,
) -> None:
    ensure_edge_tts()
    edge_tts_command = get_edge_tts_command()

    def synthesize_text_segment(text_value: str, target_path: Path, text_file: Path) -> None:
        run_command_with_retries(
            [
                *edge_tts_command,
                "--voice",
                voice,
                "--volume",
                tts_volume,
                "--file",
                str(text_file),
                "--write-media",
                str(target_path),
            ],
            "generate_en_mp3",
            max_attempts=8,
            initial_delay_seconds=3.0,
        )

    generate_tts_mp3_from_segments(
        en_srt,
        input_video,
        en_mp3,
        tts_workers,
        tts_timing_mode,
        subs_sync_mode,
        aligned_srt_output,
        synthesize_text_segment,
    )


def generate_en_mp3_with_xtts_v2(
    en_srt: Path,
    jp_reference_srt: Path,
    input_video: Path,
    en_mp3: Path,
    tts_workers: int,
    tts_timing_mode: str,
    subs_sync_mode: str,
    aligned_srt_output: Path | None,
) -> None:
    try:
        from TTS.api import TTS
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "XTTS v2 を使うには追加依存が必要です。python -m pip install -r requirements-xtts.txt を実行してください。"
        ) from exc

    reference_audio_path = en_mp3.parent / "xtts_reference.wav"
    extract_xtts_reference_audio(input_video, jp_reference_srt, reference_audio_path)

    try:
        try:
            import torch

            use_gpu = bool(torch.cuda.is_available())
        except Exception:
            use_gpu = False

        tts = TTS(model_name=XTTS_MODEL_NAME, progress_bar=False, gpu=use_gpu)

        def synthesize_text_segment(text_value: str, target_path: Path, text_file: Path) -> None:
            del text_file
            tts.tts_to_file(
                text=text_value,
                speaker_wav=str(reference_audio_path),
                language="en",
                file_path=str(target_path),
            )

        generate_tts_mp3_from_segments(
            en_srt,
            input_video,
            en_mp3,
            1,
            tts_timing_mode,
            subs_sync_mode,
            aligned_srt_output,
            synthesize_text_segment,
        )
    finally:
        if reference_audio_path.exists():
            reference_audio_path.unlink()


def generate_en_mp3(
    en_srt: Path,
    jp_reference_srt: Path,
    input_video: Path,
    en_mp3: Path,
    voice: str,
    tts_volume: str,
    force: bool,
    tts_workers: int,
    tts_timing_mode: str,
    tts_backend: str,
    subs_sync_mode: str,
    aligned_srt_output: Path | None,
) -> None:
    metadata_path = en_mp3.parent / f"{en_mp3.name}.meta.json"
    expected_metadata = build_en_mp3_metadata(
        en_srt,
        jp_reference_srt,
        input_video,
        tts_backend,
        voice,
        tts_volume,
        tts_timing_mode,
        subs_sync_mode,
    )
    if not should_regenerate_en_mp3(en_mp3, metadata_path, expected_metadata, input_video, force):
        return

    print(f"[音声合成] {en_mp3}")
    try:
        if tts_backend == "xtts-v2":
            generate_en_mp3_with_xtts_v2(
                en_srt,
                jp_reference_srt,
                input_video,
                en_mp3,
                tts_workers,
                tts_timing_mode,
                subs_sync_mode,
                aligned_srt_output,
            )
        else:
            generate_en_mp3_with_edge_tts(
                en_srt,
                input_video,
                en_mp3,
                voice,
                tts_volume,
                tts_workers,
                tts_timing_mode,
                subs_sync_mode,
                aligned_srt_output,
            )
    except Exception as exc:
        if tts_backend != "xtts-v2":
            raise
        log_pipeline(f"WARN  xtts_v2 failed; falling back to edge-tts: {exc}")
        print(f"[音声合成] XTTS v2 に失敗したため edge-tts にフォールバックします: {exc}")
        generate_en_mp3_with_edge_tts(
            en_srt,
            input_video,
            en_mp3,
            voice,
            tts_volume,
            tts_workers,
            tts_timing_mode,
            subs_sync_mode,
            aligned_srt_output,
        )

    write_en_mp3_metadata(metadata_path, expected_metadata)


def mux_final_video(input_video: Path, en_mp3: Path, final_mp4: Path, force: bool) -> None:
    if skip_if_exists(final_mp4, force, "final video already exists"):
        return

    print(f"[mux] {final_mp4}")
    run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_video),
            "-i",
            str(en_mp3),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            str(final_mp4),
        ],
        "mux_final_video",
    )


def escape_ffmpeg_filter_path(path: Path) -> str:
    value = str(path.resolve())
    value = value.replace("\\", r"\\")
    value = value.replace(":", r"\:")
    value = value.replace(",", r"\,")
    value = value.replace("'", r"\'")
    return value


def estimate_subtitle_band_ratio(en_srt: Path) -> float:
    max_lines = 1
    max_chars_per_line = 0
    max_total_chars = 0

    for block_lines in split_srt_blocks(en_srt.read_text(encoding="utf-8")):
        block = parse_srt_block(block_lines)
        text_lines = [line.strip() for line in block.text_lines if line.strip()]
        if not text_lines:
            continue
        max_lines = max(max_lines, len(text_lines))
        max_chars_per_line = max(max_chars_per_line, *(len(line) for line in text_lines))
        max_total_chars = max(max_total_chars, sum(len(line) for line in text_lines))

    # Favor taller bands so up to 4 subtitle lines fit without crowding.
    ratio = 0.28
    if max_lines >= 2:
        ratio += 0.05 * min(max_lines - 1, 3)
    if max_chars_per_line > 42:
        ratio += 0.03
    if max_chars_per_line > 56:
        ratio += 0.03
    if max_chars_per_line > 72:
        ratio += 0.03
    if max_total_chars > 84:
        ratio += 0.02
    if max_total_chars > 120:
        ratio += 0.02

    return min(max(ratio, 0.28), 0.52)


def estimate_font_scale(en_srt: Path) -> float:
    max_lines = 1
    for block_lines in split_srt_blocks(en_srt.read_text(encoding="utf-8")):
        block = parse_srt_block(block_lines)
        text_lines = [line.strip() for line in block.text_lines if line.strip()]
        if text_lines:
            max_lines = max(max_lines, len(text_lines))

    # Smaller font for 3+ line subtitles so they fit in the band without crowding.
    if max_lines >= 4:
        return 0.85
    if max_lines >= 3:
        return 0.90
    return 1.0


def build_subtitles_filter(en_srt: Path, burn_subs_in_band: bool, burn_subs_band_ratio: float) -> tuple[str, float | None]:
    subtitle_path = escape_ffmpeg_filter_path(en_srt)
    if not burn_subs_in_band:
        return f"subtitles=filename='{subtitle_path}'", None

    # Add a black band below the source frame and anchor subtitles inside it.
    band_ratio = burn_subs_band_ratio if burn_subs_band_ratio > 0 else estimate_subtitle_band_ratio(en_srt)
    font_scale = estimate_font_scale(en_srt)
    force_style = (
        f"Alignment=2,MarginV=24,MarginL=36,MarginR=36,"
        f"FontScale={font_scale:g},"
        f"Outline=1,Shadow=0,BorderStyle=1"
    )
    return (
        f"pad=iw:ih+ceil(ih*{band_ratio:g}):0:0:black,"
        f"subtitles=filename='{subtitle_path}':force_style='{force_style}'"
    ), band_ratio


def burn_subtitles(
    input_video: Path,
    en_srt: Path,
    final_sub_mp4: Path,
    burn_subs_in_band: bool,
    burn_subs_band_ratio: float,
    force: bool,
) -> None:
    if skip_if_exists(final_sub_mp4, force, "burned subtitle video already exists"):
        return

    ensure_ffmpeg_subtitles_filter()

    print(f"[字幕焼き込み] {final_sub_mp4}")
    subtitles_filter, resolved_band_ratio = build_subtitles_filter(
        en_srt,
        burn_subs_in_band,
        burn_subs_band_ratio,
    )
    video_encode_args = build_burn_subtitles_video_encode_args()
    if resolved_band_ratio is not None:
        print(f"[字幕焼き込み] band_ratio={resolved_band_ratio:.2f}")
    print(f"[字幕焼き込み] video_encode={' '.join(video_encode_args)}")
    run_command(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_video),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-vf",
            subtitles_filter,
            *video_encode_args,
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(final_sub_mp4),
        ],
        "burn_subtitles",
    )


def resolve_subtitle_codec(embed_subs_container: str, embed_subs_format: str) -> str:
    container = embed_subs_container.lower()
    subtitle_format = embed_subs_format.lower()
    if container == "mp4":
        # MP4 subtitle track is typically mov_text for broad player compatibility.
        return "mov_text"
    if subtitle_format == "ass":
        return "ass"
    return "srt"


def embed_subtitles(
    input_video: Path,
    en_srt: Path,
    output_video: Path,
    embed_subs_container: str,
    embed_subs_format: str,
    fallback_burn_output: Path,
    burn_subs_in_band: bool,
    burn_subs_band_ratio: float,
    force: bool,
) -> None:
    if skip_if_exists(output_video, force, "embedded subtitle video already exists"):
        return

    subtitle_codec = resolve_subtitle_codec(embed_subs_container, embed_subs_format)
    print(
        "[字幕埋め込み] "
        f"{output_video} "
        f"(container={embed_subs_container}, codec={subtitle_codec})"
    )

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_video),
        "-i",
        str(en_srt),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-map",
        "1:0",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-c:s",
        subtitle_codec,
        "-metadata:s:s:0",
        "language=eng",
    ]
    if embed_subs_container.lower() == "mp4":
        command.extend(["-movflags", "+faststart"])
    command.append(str(output_video))

    try:
        run_command(command, "embed_subtitles")
    except PipelineStepError:
        log_pipeline("WARN  embed_subtitles failed; fallback to burn_subtitles")
        print("[字幕埋め込み] 埋め込みに失敗したため、字幕焼き込みにフォールバックします")
        burn_subtitles(
            input_video,
            en_srt,
            fallback_burn_output,
            burn_subs_in_band,
            burn_subs_band_ratio,
            force,
        )