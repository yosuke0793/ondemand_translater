"""Resource and worker discovery for source and self-contained applications."""

from __future__ import annotations

import importlib
import io
import json
import os
import ssl
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from media_license import validate_ffmpeg_license


def is_bundled() -> bool:
    return bool(getattr(sys, "frozen", False)) and (resource_root() / "BUNDLED").is_file()


def resource_root() -> Path:
    return Path(__file__).resolve().parent


def usage_terms_text() -> str:
    root = resource_root()
    document = (root if is_bundled() else root.parents[1]) / "docs" / "TERMS_AND_DISCLAIMER.md"
    text = document.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError("利用上の注意文書が空です。配布元から完全な版を再取得してください。")
    return text


def app_version() -> str:
    version_file = resource_root() / "VERSION"
    if not version_file.is_file():
        version_file = resource_root().parents[1] / "packaging" / "VERSION"
    return version_file.read_text(encoding="utf-8").strip() if version_file.is_file() else "development"


def executable_directory() -> Path:
    return Path(sys.executable).resolve().parent


def worker_command() -> list[str]:
    if not is_bundled():
        return [os.environ.get("TRANSLATE_WORKER_PYTHON", sys.executable), "-m", "lecture_poc"]
    worker = executable_directory() / ("TranslateWorker.exe" if sys.platform == "win32" else "TranslateWorker")
    if not worker.is_file():
        raise FileNotFoundError(f"同梱の変換プログラムが見つかりません。アプリを再インストールしてください: {worker}")
    return [str(worker)]


def configure_runtime() -> None:
    if not is_bundled():
        return
    if sys.platform == "win32":
        _check_windows_utf8()
    binary_directory = executable_directory()
    for name in ("ffmpeg", "ffprobe"):
        binary = binary_directory / (f"{name}.exe" if sys.platform == "win32" else name)
        if not binary.is_file():
            raise FileNotFoundError(f"同梱の動画処理ツールが見つかりません。アプリを再インストールしてください: {binary}")
    os.environ["PATH"] = str(binary_directory) + os.pathsep + os.environ.get("PATH", "")
    espeak_data = resource_root() / "piper" / "espeak-ng-data"
    missing = [
        name for name in ("phontab", "phondata", "phonindex")
        if not (espeak_data / name).is_file()
    ]
    if not (espeak_data / "voices").is_dir():
        missing.append("voices/")
    if missing:
        raise FileNotFoundError(
            f"同梱のPiper eSpeak-NGデータが不完全です: {espeak_data} "
            f"(missing: {', '.join(missing)})"
        )
    os.environ["ESPEAK_DATA_PATH"] = str(espeak_data)
    configure_tls_trust()


def _check_windows_utf8() -> None:
    import ctypes

    get_code_page = ctypes.WinDLL("kernel32").GetACP
    get_code_page.argtypes = []
    get_code_page.restype = ctypes.c_uint
    code_page = get_code_page()
    if code_page != 65001:
        raise RuntimeError(
            f"Bundled Windows runtime requires the UTF-8 process manifest "
            f"(code page 65001, found {code_page}). "
            "Reinstall a complete supported build; Windows 10 version 1903 or later is required."
        )


def configure_tls_trust() -> None:
    # urllib (Argos model downloads) otherwise uses OpenSSL CA paths from the build machine.
    if not os.environ.get("SSL_CERT_FILE"):
        import certifi

        os.environ["SSL_CERT_FILE"] = certifi.where()


def _tls_trust_report() -> dict:
    ca_file = ssl.get_default_verify_paths().cafile
    certificates = ssl.create_default_context().cert_store_stats()["x509_ca"]
    if not ca_file or certificates <= 0:
        raise RuntimeError("No HTTPS CA certificates are available; model downloads would fail.")
    return {"ca_file": ca_file, "ca_certificates": certificates}


def worker_environment() -> dict[str, str]:
    environment = dict(os.environ)
    if is_bundled():
        for name in ("PYTHONHOME", "PYTHONPATH", "TRANSLATE_PYTHON", "TRANSLATE_WORKER_PYTHON"):
            environment.pop(name, None)
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        environment["PATH"] = str(executable_directory()) + os.pathsep + environment.get("PATH", "")
    return environment


def piper_model_paths(voice: str = "en_US-ljspeech-medium") -> tuple[Path, Path]:
    """Return the pinned local voice paths in a frozen bundle or source checkout."""
    if not is_bundled():
        packaging = Path(__file__).resolve().parents[2] / "packaging"
        if packaging.is_dir() and str(packaging) not in sys.path:
            sys.path.insert(0, str(packaging))
    from piper_model import resolve_model_paths

    return resolve_model_paths(resource_root() if is_bundled() else None, voice=voice)


def _piper_smoke_test() -> dict:
    from lecture_poc.tts import PIPER_DEFAULT_VOICE, PIPER_VOICES

    voices = {name: _piper_voice_smoke_test(name) for name in PIPER_VOICES}
    return {**voices[PIPER_DEFAULT_VOICE], "voices": voices}


def _piper_voice_smoke_test(voice_name: str) -> dict:
    from piper import PiperVoice

    model_path, config_path = piper_model_paths(voice_name)
    voice = PiperVoice.load(str(model_path), config_path=str(config_path), use_cuda=False)
    output = io.BytesIO()
    audio_format: tuple[int, int, int] | None = None
    with wave.open(output, "wb") as wav_file:
        for chunk in voice.synthesize("Piper is ready."):
            chunk_format = (chunk.sample_channels, chunk.sample_width, chunk.sample_rate)
            if audio_format is None:
                wav_file.setnchannels(chunk.sample_channels)
                wav_file.setsampwidth(chunk.sample_width)
                wav_file.setframerate(chunk.sample_rate)
                audio_format = chunk_format
            elif chunk_format != audio_format:
                raise RuntimeError("Piper changed its WAV format between audio chunks.")
            wav_file.writeframes(chunk.audio_int16_bytes)
    with wave.open(io.BytesIO(output.getvalue()), "rb") as wav_file:
        sample_rate = wav_file.getframerate()
        frames = wav_file.getnframes()
        duration = frames / sample_rate if sample_rate else 0
    if frames <= 0 or duration <= 0:
        raise RuntimeError("Piper produced an empty WAV during its offline self-test.")
    return {"voice": voice_name, "wav_duration_seconds": duration}


# Filtergraph metacharacters that are valid in Windows and macOS directory names,
# e.g. a profile folder such as C:\Users\O'Brien.
SUBTITLE_SMOKE_DIRECTORY_PREFIX = "subtitle check ' [x], ; "


def _subtitle_burn_smoke_test() -> dict:
    from lecture_poc.pipeline import build_subtitles_filter

    ffmpeg = executable_directory() / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    with tempfile.TemporaryDirectory(prefix=SUBTITLE_SMOKE_DIRECTORY_PREFIX) as directory:
        subtitles = Path(directory) / "en_aligned.srt"
        subtitles.write_text("1\n00:00:00,000 --> 00:00:05,000\nSubtitle check\n", encoding="utf-8")
        for in_band in (False, True):
            video_filter, _ = build_subtitles_filter(subtitles, in_band, 0.3)
            result = subprocess.run(
                [str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "color=c=black:s=320x180:d=1", "-vf", video_filter,
                 "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                capture_output=True, timeout=120,
            )
            if result.returncode != 0:
                error = result.stderr.decode("utf-8", errors="replace").strip()[-800:]
                raise RuntimeError(f"Bundled FFmpeg could not burn subtitles from {subtitles}: {error}")
            if not any(value > 128 for value in result.stdout):
                raise RuntimeError("Bundled FFmpeg burned no visible subtitle text; check OS font support.")
    return {"directory_prefix": SUBTITLE_SMOKE_DIRECTORY_PREFIX, "in_band": [False, True]}


def bundle_self_test(role: str, report_path: Path | None = None) -> int:
    """Validate a built artifact without a display, credentials or model downloads."""
    try:
        configure_runtime()
        if not is_bundled():
            raise RuntimeError("This check must run from a frozen application.")
        prompts = resource_root() / "lecture_poc" / "prompts"
        for name in ("engineering_terms.csv", "whisper_initial_prompt.txt", "jp_clean_ollama.txt"):
            if not (prompts / name).is_file():
                raise FileNotFoundError(f"Missing bundled resource: {name}")
        if role == "gui":
            importlib.import_module("tkinter")
            importlib.import_module("requests")
            usage_terms_text()
            worker_command()
            from credential_store import secure_backend

            secure_backend()
        elif role == "worker":
            for module in ("faster_whisper", "argostranslate.translate", "piper", "requests"):
                importlib.import_module(module)
            vad = importlib.import_module("faster_whisper.vad")
            assets = Path(vad.get_assets_path())
            for name in ("silero_encoder_v5.onnx", "silero_decoder_v5.onnx"):
                if not (assets / name).is_file():
                    raise FileNotFoundError(f"Missing bundled VAD resource: {name}")
            vad.get_vad_model()
            piper_report = _piper_smoke_test()
            tls_report = _tls_trust_report()
            subtitle_report = _subtitle_burn_smoke_test()
        else:
            raise ValueError(f"Unknown bundle role: {role}")
        tools = {}
        for name in ("ffmpeg", "ffprobe"):
            binary = executable_directory() / (f"{name}.exe" if sys.platform == "win32" else name)
            result = subprocess.run([str(binary), "-version"], capture_output=True, text=True, check=True)
            license_result = subprocess.run([str(binary), "-L"], capture_output=True, text=True, check=True)
            validate_ffmpeg_license(
                result.stdout + result.stderr, license_result.stdout + license_result.stderr,
            )
            tools[name] = result.stdout.splitlines()[0]
        payload = {"status": "ok", "role": role, "version": app_version(), "tools": tools}
        if role == "worker":
            payload["piper"] = piper_report
            payload["tls"] = tls_report
            payload["subtitles"] = subtitle_report
        exit_code = 0
    except (ImportError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        payload = {"status": "error", "role": role, "error": str(exc)}
        exit_code = 1
    serialized = json.dumps(payload, ensure_ascii=True)
    if report_path is not None:
        report_path.write_text(serialized + "\n", encoding="utf-8")
    elif sys.stdout is not None:
        print(serialized, flush=True)
    return exit_code
