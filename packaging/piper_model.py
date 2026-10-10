"""Pin and stage the offline Piper voices used by native distributions."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

PACKAGING = Path(__file__).resolve().parent
MODEL_MANIFEST = PACKAGING / "piper_model.json"
MODEL_STAGE = PACKAGING / ".work" / "piper-model"
DEFAULT_VOICE = "en_US-ljspeech-medium"


class PiperModelError(RuntimeError):
    """A safe, actionable model-cache or model-download failure."""


def load_model_manifest() -> dict:
    manifest = json.loads(MODEL_MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest["voice"]["name"] != DEFAULT_VOICE:
        raise ValueError("Unsupported Piper model manifest")
    return manifest


def supported_voices() -> tuple[str, ...]:
    manifest = load_model_manifest()
    return tuple(model["name"] for model in
                 [manifest["voice"], *manifest.get("additional_voices", [])])


def voice_manifest(voice: str = DEFAULT_VOICE) -> dict:
    manifest = load_model_manifest()
    for model in [manifest["voice"], *manifest.get("additional_voices", [])]:
        if model["name"] == voice:
            return model
    raise ValueError(f"Unsupported Piper voice: {voice}")


def model_metadata(model: dict) -> dict:
    """Release provenance, retaining the legacy default-model record."""
    return {
        **{key: model[key] for key in
           ("name", "license", "creator", "repository", "revision")},
        "files": {asset["name"]: asset["sha256"] for asset in model["files"]},
        **({"creator_page": model["creator_page"],
            "file_urls": {asset["name"]: asset["url"]
                          for asset in model["files"] if "url" in asset}}
           if any("url" in asset for asset in model["files"]) else {}),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(url: str, destination: Path, expected_hash: str) -> None:
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=destination.name + ".", suffix=".partial", delete=False,
    ) as temporary:
        partial = Path(temporary.name)
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as output:
            while block := response.read(1024 * 1024):
                digest.update(block)
                output.write(block)
        if digest.hexdigest() != expected_hash:
            raise ValueError(f"Piper asset SHA-256 mismatch: {destination.name}")
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)


def stage_model(destination: Path = MODEL_STAGE, voice: str | None = None) -> Path:
    """Stage all build voices, or only the selected source-runtime voice."""
    models = [voice_manifest(name) for name in
              (supported_voices() if voice is None else (voice,))]
    destination.mkdir(parents=True, exist_ok=True)
    for model in models:
        _stage_voice(destination, model)
    shutil.copy2(MODEL_MANIFEST, destination / "piper_model.json")
    return destination


def _stage_voice(destination: Path, voice: dict) -> None:
    base_url = f'{voice["repository"]}/resolve/{voice["revision"]}/en/en_US/ljspeech/medium'
    for item in voice["files"]:
        path = destination / item["name"]
        if path.is_file() and _sha256(path) == item["sha256"]:
            continue
        path.unlink(missing_ok=True)
        if item.get("source") == "local":
            source = PACKAGING / item["name"]
            if not source.is_file() or _sha256(source) != item["sha256"]:
                raise ValueError(f"Piper licensing evidence SHA-256 mismatch: {source.name}")
            shutil.copy2(source, path)
            continue
        try:
            _download(item.get("url", f"{base_url}/{item['name']}"), path, item["sha256"])
        except ValueError as exc:
            raise PiperModelError(str(exc)) from None
        except OSError:
            raise PiperModelError(
                f"Could not download or write the pinned Piper asset {item['name']}; "
                "check network access and cache permissions."
            ) from None


def default_model_cache(voice: str = DEFAULT_VOICE) -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    revision = voice_manifest(voice)["revision"]
    return base / "ondemand-translater" / "piper" / revision


def _is_valid_model_dir(directory: Path, voice: str = DEFAULT_VOICE) -> bool:
    try:
        assets = voice_manifest(voice)["files"]
        return all(
            (directory / asset["name"]).is_file()
            and _sha256(directory / asset["name"]) == asset["sha256"]
            for asset in assets
        )
    except (OSError, ValueError):
        return False


def resolve_model_dir(resource_root: Path | None = None, voice: str = DEFAULT_VOICE) -> Path:
    """Resolve the offline voice; source installs cache only the pinned assets."""
    voice_manifest(voice)
    if resource_root is not None:
        bundled = Path(resource_root) / "piper-model"
        if _is_valid_model_dir(bundled, voice):
            return bundled
        raise FileNotFoundError(f"The bundled Piper model is missing or damaged: {bundled}")
    configured = os.environ.get("PIPER_MODEL_DIR")
    if configured:
        candidate = Path(configured).expanduser()
        if _is_valid_model_dir(candidate, voice):
            return candidate
        raise FileNotFoundError(f"PIPER_MODEL_DIR is missing or has an invalid pinned Piper model: {candidate}")
    if _is_valid_model_dir(MODEL_STAGE, voice):
        return MODEL_STAGE
    cache = default_model_cache(voice)
    try:
        return stage_model(cache, voice=voice)
    except PiperModelError:
        raise
    except (OSError, ValueError) as exc:
        raise PiperModelError(
            "Could not prepare the pinned Piper voice assets "
            f"({type(exc).__name__}). Check network access and cache permissions, "
            "or set PIPER_MODEL_DIR to a verified model directory."
        ) from None


def resolve_model_paths(
    resource_root: Path | None = None, voice: str = DEFAULT_VOICE,
) -> tuple[Path, Path]:
    directory = resolve_model_dir(resource_root, voice)
    return (
        directory / f"{voice}.onnx",
        directory / f"{voice}.onnx.json",
    )
