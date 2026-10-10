"""Local Piper and official Azure Speech segment synthesis (no fallback/retries)."""

from __future__ import annotations

import io
from itertools import chain
import os
import re
import threading
import wave
from pathlib import Path
from xml.etree import ElementTree

from credential_store import validate_speech_region

PIPER_DEFAULT_VOICE = "en_US-ljspeech-medium"
PIPER_VOICES = (PIPER_DEFAULT_VOICE, "en_US-john-medium")
AZURE_DEFAULT_VOICE = "en-US-AriaNeural"


class TTSError(RuntimeError):
    pass


def validate_azure_region(region: str) -> str:
    """Validate an Azure region before interpolating it into an HTTPS hostname."""
    try:
        return validate_speech_region(region)
    except ValueError:
        raise TTSError("AZURE_SPEECH_REGION must be an Azure region name (for example eastus).") from None


def resolve_voice(backend: str, voice: str | None = None) -> str:
    if backend == "piper":
        resolved = PIPER_DEFAULT_VOICE if voice is None else voice
        if resolved not in PIPER_VOICES:
            raise TTSError(f"Piper supports only the pinned voices: {', '.join(PIPER_VOICES)}.")
    elif backend == "azure":
        resolved = AZURE_DEFAULT_VOICE if voice is None else voice
        if re.fullmatch(r"en-[A-Z]{2}-[A-Za-z][A-Za-z0-9]*Neural", resolved) is None:
            raise TTSError("Azure voice must be an English Neural voice (for example en-US-AriaNeural).")
    else:
        raise TTSError("Unknown TTS backend; choose piper or azure.")
    return resolved


def volume_gain(volume: str) -> float:
    if re.fullmatch(r"[+-]\d{1,6}%", volume) is None:
        raise TTSError("TTS volume must be a signed percentage (for example +100%).")
    percent = int(volume[:-1])
    if percent < -100:
        raise TTSError("TTS volume cannot be less than -100%.")
    return 1.0 + percent / 100.0


def validate_tts_settings(backend: str, voice: str | None, volume: str) -> str:
    resolved = resolve_voice(backend, voice)
    volume_gain(volume)
    if backend == "azure":
        azure_credentials()
    return resolved


def azure_credentials() -> tuple[str, str]:
    key = os.environ.get("AZURE_SPEECH_KEY", "").strip()
    if not key or any(ord(character) < 32 or ord(character) == 127 for character in key):
        raise TTSError("Set AZURE_SPEECH_KEY to your Azure AI Speech resource key.")
    region = validate_azure_region(os.environ.get("AZURE_SPEECH_REGION", ""))
    return key, region


def _validate_wav(audio: bytes) -> None:
    try:
        with wave.open(io.BytesIO(audio), "rb") as wav_file:
            if wav_file.getnframes() <= 0 or wav_file.getframerate() <= 0:
                raise ValueError("empty audio")
            frames = wav_file.readframes(wav_file.getnframes())
            if len(frames) != wav_file.getnframes() * wav_file.getnchannels() * wav_file.getsampwidth():
                raise ValueError("truncated audio")
    except (EOFError, OSError, ValueError, wave.Error):
        raise TTSError("Speech synthesis returned invalid or empty WAV audio.") from None


class PiperBackend:
    def __init__(self, voice: str | None = None):
        self.voice = resolve_voice("piper", voice)
        self._local = threading.local()
        self._paths: tuple[Path, Path] | None = None
        self._lock = threading.Lock()

    def synthesize(self, text: str, target: Path) -> None:
        try:
            from piper import PiperVoice
        except ImportError:
            raise TTSError("Piper is unavailable; install the pinned piper-tts dependency.") from None
        inference_errors = _onnx_runtime_errors()
        # Each worker owns its voice/session: phonemizer state is not shared.
        if not hasattr(self._local, "engine"):
            with self._lock:
                if self._paths is None:
                    try:
                        from app_runtime import piper_model_paths

                        self._paths = piper_model_paths(self.voice)
                    except ImportError as exc:
                        raise TTSError(
                            f"Piper model resolver is unavailable ({type(exc).__name__})."
                        ) from None
                    except (OSError, ValueError, RuntimeError) as exc:
                        raise TTSError(
                            f"Piper model resolution failed ({type(exc).__name__}): {exc}"
                        ) from None
                model_path, config_path = self._paths
                try:
                    self._local.engine = PiperVoice.load(
                        str(model_path), config_path=str(config_path), use_cuda=False,
                    )
                except (ImportError, OSError, ValueError, RuntimeError, KeyError, *inference_errors) as exc:
                    raise TTSError(
                        f"Piper model loading failed ({type(exc).__name__}); "
                        "check the pinned model configuration and local runtime. No fallback was attempted."
                    ) from None
        try:
            output = io.BytesIO()
            chunks = iter(self._local.engine.synthesize(text))
            first_chunk = next(chunks, None)
            if first_chunk is None:
                raise TTSError("Piper returned empty audio.")
            with wave.open(output, "wb") as wav_file:
                audio_format = (
                    first_chunk.sample_channels, first_chunk.sample_width, first_chunk.sample_rate,
                )
                wav_file.setnchannels(first_chunk.sample_channels)
                wav_file.setsampwidth(first_chunk.sample_width)
                wav_file.setframerate(first_chunk.sample_rate)
                for chunk in chain((first_chunk,), chunks):
                    chunk_format = (chunk.sample_channels, chunk.sample_width, chunk.sample_rate)
                    if chunk_format != audio_format:
                        raise TTSError("Piper returned inconsistent audio formats.")
                    wav_file.writeframes(chunk.audio_int16_bytes)
            audio = output.getvalue()
            _validate_wav(audio)
        except TTSError:
            raise
        except (ImportError, OSError, ValueError, RuntimeError, wave.Error, *inference_errors) as exc:
            raise TTSError(
                f"Piper inference failed ({type(exc).__name__}); "
                "check the local runtime and voice configuration. No fallback was attempted."
            ) from None
        try:
            target.write_bytes(audio)
        except OSError as exc:
            raise TTSError(
                f"Piper audio write failed ({type(exc).__name__}, errno={exc.errno}); "
                "check output permissions and available disk space."
            ) from None


def _onnx_runtime_errors() -> tuple[type[Exception], ...]:
    try:
        from onnxruntime.capi import onnxruntime_pybind11_state
    except ImportError:
        return ()
    return tuple(
        getattr(onnxruntime_pybind11_state, name)
        for name in (
            "EngineError", "EPFail", "Fail", "InvalidArgument", "InvalidGraph",
            "InvalidProtobuf", "NoSuchFile", "NotImplemented", "RuntimeException",
        )
    )


class AzureBackend:
    def __init__(self, voice: str | None = None):
        self.voice = resolve_voice("azure", voice)
        self._key, region = azure_credentials()
        self._url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"

    def synthesize(self, text: str, target: Path) -> None:
        try:
            import requests
        except ImportError:
            raise TTSError("Azure Speech requires the requests dependency.") from None
        root = ElementTree.Element(
            "speak", {"version": "1.0", "xmlns": "http://www.w3.org/2001/10/synthesis",
                      "xml:lang": self.voice[:5]},
        )
        ElementTree.SubElement(root, "voice", {"name": self.voice}).text = text
        ssml = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
        try:
            # Exactly one paid request per unique subtitle. Do not resend after
            # ambiguous timeouts, redirects or transient service failures.
            response = requests.post(
                self._url,
                headers={
                    "Ocp-Apim-Subscription-Key": self._key,
                    "Content-Type": "application/ssml+xml",
                    "X-Microsoft-OutputFormat": "riff-24khz-16bit-mono-pcm",
                    "User-Agent": "OnDemandTranslator",
                },
                data=ssml,
                timeout=(10, 60),
                allow_redirects=False,
            )
        except requests.Timeout:
            raise TTSError("Azure Speech request timed out; not retried to avoid duplicate charges.") from None
        except requests.RequestException:
            raise TTSError("Azure Speech connection failed; no retry or fallback was attempted.") from None
        try:
            status = response.status_code
            if status != 200:
                if status in {401, 403}:
                    detail = "check the Speech resource key and region"
                elif status == 429 or 500 <= status <= 599:
                    detail = "transient service failure; not retried to avoid duplicate charges"
                else:
                    detail = "request rejected; no retry or fallback was attempted"
                raise TTSError(f"Azure Speech HTTP {status}: {detail}.")
            audio = response.content
            _validate_wav(audio)
            target.write_bytes(audio)
        finally:
            response.close()


def create_backend(backend: str, voice: str | None = None) -> PiperBackend | AzureBackend:
    resolve_voice(backend, voice)
    return PiperBackend(voice) if backend == "piper" else AzureBackend(voice)
