"""Resource and worker discovery for source and self-contained applications."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path


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
            for module in ("faster_whisper", "argostranslate.translate", "edge_tts", "requests"):
                importlib.import_module(module)
            vad = importlib.import_module("faster_whisper.vad")
            assets = Path(vad.get_assets_path())
            for name in ("silero_encoder_v5.onnx", "silero_decoder_v5.onnx"):
                if not (assets / name).is_file():
                    raise FileNotFoundError(f"Missing bundled VAD resource: {name}")
            vad.get_vad_model()
            subprocess.run(
                [sys.executable, "--edge-tts", "--help"],
                capture_output=True, text=True, check=True, timeout=60,
            )
        else:
            raise ValueError(f"Unknown bundle role: {role}")
        tools = {}
        for name in ("ffmpeg", "ffprobe"):
            binary = executable_directory() / (f"{name}.exe" if sys.platform == "win32" else name)
            result = subprocess.run([str(binary), "-version"], capture_output=True, text=True, check=True)
            tools[name] = result.stdout.splitlines()[0]
        payload = {"status": "ok", "role": role, "version": app_version(), "tools": tools}
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
