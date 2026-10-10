"""Resource and worker discovery for source and self-contained applications."""

from __future__ import annotations

import importlib
import io
import json
import os
import subprocess
import sys
import wave
from pathlib import Path
from media_license import validate_ffmpeg_license


def is_bundled() -> bool:
    return bool(getattr(sys, "frozen", False)) and (resource_root() / "BUNDLED").is_file()


def resource_root() -> Path:
    return Path(__file__).resolve().parent


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
    binary_directory = executable_directory()
    for name in ("ffmpeg", "ffprobe"):
        binary = binary_directory / (f"{name}.exe" if sys.platform == "win32" else name)
        if not binary.is_file():
            raise FileNotFoundError(f"同梱の動画処理ツールが見つかりません。アプリを再インストールしてください: {binary}")
    os.environ["PATH"] = str(binary_directory) + os.pathsep + os.environ.get("PATH", "")


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
