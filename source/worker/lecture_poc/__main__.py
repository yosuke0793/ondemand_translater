from __future__ import annotations

import argparse
import importlib.util
import platform
import sys
from pathlib import Path

from .pipeline import PipelineConfig, PipelineStepError, run_pipeline
from .security import redact_secrets
from .tts import TTSError, validate_tts_settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lecture_poc",
        description="講義動画の吹き替えと字幕翻訳を行う macOS / Linux 向け PoC",
    )
    parser.add_argument("--input", required=True, type=Path, help="入力する講義動画のパス")
    parser.add_argument("--outdir", required=True, type=Path, help="出力ディレクトリ")
    parser.add_argument(
        "--transcription-preset",
        choices=("high-accuracy", "balanced", "fast"),
        default="balanced",
        help="Whisper 文字起こしプリセット: high-accuracy / balanced / fast",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="faster-whisper のモデルサイズ（未指定時はプリセット既定値）",
    )
    parser.add_argument(
        "--whisper-prompt-file",
        type=Path,
        default=None,
        help="Whisper initial_prompt を読み込むテキストファイル。未指定時は既定パスを参照",
    )
    parser.add_argument(
        "--voice", default=None,
        help="音声名。Piper: 女性 en_US-ljspeech-medium（既定）／男性 en_US-john-medium。"
        "Azure: 女性 en-US-AriaNeural（既定）／男性 en-US-AndrewNeural など",
    )
    parser.add_argument(
        "--tts-workers",
        type=int,
        default=1,
        help="音声合成の並列実行数。0 で自動設定（既定値は 1）",
    )
    parser.add_argument(
        "--tts-timing-mode",
        choices=("strict", "no-overlap"),
        default="no-overlap",
        help="音声配置モード。no-overlap は必要に応じて後続字幕を遅らせて重なりを回避",
    )
    parser.add_argument(
        "--tts-backend",
        choices=("piper", "azure"),
        default="piper",
        help="英語音声合成。既定はローカル Piper、azure は公式 Azure AI Speech（有料）",
    )
    parser.add_argument(
        "--subs-sync-mode",
        choices=("original", "tts-aligned"),
        default="tts-aligned",
        help="動画表示に使う英語字幕の時刻モード。tts-aligned は no-overlap 時に音声配置へ同期",
    )
    parser.add_argument(
        "--tts-volume",
        default="+100%",
        help="吹き替え音量（例: +100%% で約2倍、+200%% で約3倍）",
    )
    parser.add_argument(
        "--chunk-seconds",
        type=float,
        default=600.0,
        help="文字起こし時の自動分割チャンク長（秒）。0 以下で無効",
    )
    parser.add_argument(
        "--chunk-overlap-seconds",
        type=float,
        default=1.5,
        help="チャンク境界の重複長（秒）。境界欠落を減らすために使用",
    )
    parser.add_argument(
        "--transcription-workers",
        type=int,
        default=0,
        help="長尺音声のチャンク文字起こしを並列実行する worker 数。0 で CPU コア数から自動設定",
    )
    parser.add_argument(
        "--transcription-cpu-threads",
        type=int,
        default=0,
        help="faster-whisper が使う CPU スレッド数。0 で自動",
    )
    parser.add_argument(
        "--transcription-max-pending-chunks",
        type=int,
        default=0,
        help="抽出済みで文字起こし待ちのチャンク上限。0 で worker 数に応じて自動",
    )
    parser.add_argument(
        "--transcription-device",
        choices=("cpu", "cuda", "auto"),
        default="auto",
        help="faster-whisper の実行デバイス。cpu/cuda/auto から選択",
    )
    parser.add_argument(
        "--transcription-compute-type",
        default=None,
        help="faster-whisper の compute_type を手動指定（例: int8, int8_float16, float16, float32）",
    )
    parser.add_argument(
        "--lowpass-cutoff-hz",
        type=float,
        default=6500.0,
        help="高周波ノイズ除去用 lowpass のカットオフ周波数（Hz）。0 以下で無効",
    )
    parser.add_argument(
        "--jp-clean-mode",
        choices=("off", "rule", "ollama"),
        default="rule",
        help="日本語字幕の整形方法: off/rule/ollama",
    )
    parser.add_argument(
        "--ollama-model",
        default="qwen2.5:7b-instruct",
        help="--jp-clean-mode ollama のときに使うローカルモデル名",
    )
    parser.add_argument(
        "--translation-backend",
        choices=("argos", "deepl", "gemini"),
        default="gemini",
        help="翻訳バックエンド: argos/deepl/gemini",
    )
    parser.add_argument(
        "--deepl-api-key",
        default=None,
        help="DeepL APIキー（未指定時は環境変数 DEEPL_API_KEY を使用）",
    )
    parser.add_argument(
        "--gemini-api-key",
        default=None,
        help="Gemini APIキー（未指定時は環境変数 GEMINI_API_KEY を使用）",
    )
    parser.add_argument(
        "--gemini-model",
        default="gemini-3.1-flash-lite",
        help="--translation-backend gemini のときに使うモデル名",
    )
    parser.add_argument("--burn-subs", action="store_true", help="英語字幕を焼き込んだ final_sub.mp4 も作成します")
    parser.add_argument(
        "--embed-subs",
        action="store_true",
        help="英語字幕を埋め込んだ final_soft_sub.* も作成します（ソフト字幕）",
    )
    parser.add_argument(
        "--embed-subs-container",
        choices=("mp4", "mkv"),
        default="mp4",
        help="--embed-subs 時の出力コンテナ（既定: mp4）",
    )
    parser.add_argument(
        "--embed-subs-format",
        choices=("srt", "ass"),
        default="srt",
        help="--embed-subs 時の字幕フォーマット（mkv 出力時に有効）",
    )
    parser.add_argument(
        "--burn-subs-in-band",
        action="store_true",
        help="--burn-subs 時に動画下へ黒帯を追加し、その帯に英語字幕を焼き込みます",
    )
    parser.add_argument(
        "--burn-subs-band-ratio",
        type=float,
        default=0.0,
        help="--burn-subs-in-band 時の帯の高さ比率。0 以下なら字幕量に応じて自動調整",
    )
    parser.add_argument(
        "--video-text-overlay",
        action="store_true",
        help="動画内の日本語テキストを検出して英語に置換（黒塗り＋英字描画）",
    )
    parser.add_argument(
        "--video-text-overlay-fps",
        type=float,
        default=2.0,
        help="動画内テキスト検出のサンプリングFPS（既定: 2.0）",
    )
    parser.add_argument(
        "--video-text-overlay-min-chars",
        type=int,
        default=3,
        help="動画内テキスト置換の最小文字数（既定: 3）",
    )
    parser.add_argument(
        "--video-text-overlay-min-confidence",
        type=float,
        default=70.0,
        help="OCR信頼度の下限（0-100, 既定: 70）",
    )
    parser.add_argument(
        "--video-text-overlay-font",
        default="Noto Sans",
        help="英語描画フォント（既定: Noto Sans）",
    )
    parser.add_argument("--force", action="store_true", help="すべての出力を上書きして再生成します")
    return parser


def ensure_python_dependencies(translation_backend: str, video_text_overlay: bool, tts_backend: str) -> None:
    if tts_backend not in {"piper", "azure"}:
        raise ModuleNotFoundError("TTS backend must be piper or azure.")
    required_modules = ["faster_whisper", "piper" if tts_backend == "piper" else "requests"]
    if translation_backend == "argos":
        required_modules.append("argostranslate")
    elif translation_backend in {"deepl", "gemini"}:
        required_modules.append("requests")

    if video_text_overlay:
        required_modules.extend(["cv2", "pytesseract"])

    missing_modules = [
        module_name
        for module_name in required_modules
        if importlib.util.find_spec(module_name) is None
    ]
    if not missing_modules:
        return

    missing_list = ", ".join(missing_modules)
    raise ModuleNotFoundError(
        "必要な Python パッケージが見つかりません: "
        f"{missing_list}. 仮想環境を有効化してから `python -m pip install -r requirements.txt` を実行し、"
        "その同じ環境で `python -m lecture_poc ...` を起動してください。"
    )


def main(argv: list[str] | None = None) -> int:
    current_platform = platform.system()
    if current_platform not in {"Darwin", "Linux", "Windows"}:
        print(
            f"この PoC は macOS / Linux / Windows 対応です。現在の OS: {current_platform}",
            file=sys.stderr,
        )
        return 1

    args = build_parser().parse_args(argv)

    if args.burn_subs and args.embed_subs:
        print("--burn-subs と --embed-subs は同時指定できません", file=sys.stderr)
        return 1

    try:
        args.voice = validate_tts_settings(args.tts_backend, args.voice, args.tts_volume)
        ensure_python_dependencies(args.translation_backend, args.video_text_overlay, args.tts_backend)
    except (ModuleNotFoundError, TTSError) as exc:
        print(redact_secrets(exc), file=sys.stderr)
        return 1

    config = PipelineConfig(
        input_video=args.input,
        outdir=args.outdir,
        model_size=args.model,
        whisper_prompt_file=args.whisper_prompt_file,
        transcription_preset=args.transcription_preset,
        voice=args.voice,
        tts_volume=args.tts_volume,
        chunk_seconds=args.chunk_seconds,
        chunk_overlap_seconds=args.chunk_overlap_seconds,
        transcription_workers=args.transcription_workers,
        transcription_cpu_threads=args.transcription_cpu_threads,
        transcription_max_pending_chunks=args.transcription_max_pending_chunks,
        transcription_device=args.transcription_device,
        transcription_compute_type=args.transcription_compute_type,
        tts_workers=args.tts_workers,
        tts_timing_mode=args.tts_timing_mode,
        tts_backend=args.tts_backend,
        subs_sync_mode=args.subs_sync_mode,
        lowpass_cutoff_hz=args.lowpass_cutoff_hz,
        jp_clean_mode=args.jp_clean_mode,
        ollama_model=args.ollama_model,
        translation_backend=args.translation_backend,
        deepl_api_key=args.deepl_api_key,
        gemini_api_key=args.gemini_api_key,
        gemini_model=args.gemini_model,
        burn_subs=args.burn_subs,
        embed_subs=args.embed_subs,
        embed_subs_container=args.embed_subs_container,
        embed_subs_format=args.embed_subs_format,
        burn_subs_in_band=args.burn_subs_in_band,
        burn_subs_band_ratio=args.burn_subs_band_ratio,
        video_text_overlay=args.video_text_overlay,
        video_text_overlay_fps=args.video_text_overlay_fps,
        video_text_overlay_min_chars=args.video_text_overlay_min_chars,
        video_text_overlay_min_confidence=args.video_text_overlay_min_confidence,
        video_text_overlay_font=args.video_text_overlay_font,
        force=args.force,
    )

    try:
        run_pipeline(config)
    except PipelineStepError as exc:
        print(f"[{exc.step}] {redact_secrets(exc)}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(redact_secrets(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(redact_secrets(exc), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())