"""Shared Gemini translation request used by the worker and connection test."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import requests

DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite"


class GeminiAlignmentError(RuntimeError):
    pass


def gemini_api_key_auth(request: requests.PreparedRequest) -> requests.PreparedRequest:
    # Explicit auth prevents Requests from adding unrelated .netrc Basic credentials.
    return request


def request_gemini(
    lines: list[str], api_key: str, model: str, *, timeout: int = 60,
) -> requests.Response:
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
    return requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": api_key},
        auth=gemini_api_key_auth,
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        },
        timeout=timeout,
        allow_redirects=False,
    )


def request_translations(
    lines: list[str], api_key: str, model: str, *, timeout: int = 60,
) -> dict[str, str]:
    import requests

    response = request_gemini(lines, api_key, model, timeout=timeout)
    response.raise_for_status()
    if not 200 <= response.status_code < 300:
        raise requests.HTTPError("Unexpected Gemini HTTP status", response=response)
    return parse_gemini_translations(response.json(), lines)


def parse_gemini_translations(data: dict, lines: list[str]) -> dict[str, str]:
    try:
        text_payload = data["candidates"][0]["content"]["parts"][0]["text"]
        items = json.loads(text_payload)["translations"]
    except (KeyError, IndexError, TypeError, ValueError):
        raise GeminiAlignmentError("Gemini の応答 JSON が不正です") from None
    if not isinstance(items, list) or len(items) != len(lines):
        raise GeminiAlignmentError("Gemini の応答件数が入力件数と一致しません")
    translated_by_id: dict[int, str] = {}
    for item in items:
        line_id = item.get("id") if isinstance(item, dict) else None
        if type(line_id) is not int or line_id < 1 or line_id > len(lines) or line_id in translated_by_id:
            raise GeminiAlignmentError("Gemini の応答 ID が不正です")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise GeminiAlignmentError("Gemini の翻訳行が不足しています")
        translated_by_id[line_id] = " ".join(line.strip() for line in text.splitlines() if line.strip())
    return {line: translated_by_id[index] for index, line in enumerate(lines, start=1)}
