"""Shared Gemini translation request used by the worker and connection test."""

from __future__ import annotations

import json

DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite"


def request_translations(
    lines: list[str], api_key: str, model: str, *, timeout: int = 60,
    require_text: bool = False,
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
    response = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": api_key},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        },
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    candidates = data.get("candidates", [])
    if not candidates:
        raise RuntimeError("Gemini の応答に candidates がありません")
    parts = candidates[0].get("content", {}).get("parts", [])
    if not parts:
        raise RuntimeError("Gemini の応答に parts がありません")
    parsed = json.loads(parts[0].get("text", ""))
    translated_by_id: dict[int, str] = {}
    for item in parsed.get("translations", []):
        line_id = int(item.get("id"))
        if line_id < 1 or line_id > len(lines):
            raise RuntimeError("Gemini の応答 ID が不正です")
        if line_id in translated_by_id:
            continue
        raw_text = item.get("text", "")
        translated_text = raw_text.strip() if isinstance(raw_text, str) else ""
        if require_text and not translated_text:
            raise RuntimeError("Gemini の翻訳結果が空です")
        translated_by_id[line_id] = translated_text or lines[line_id - 1]
    if len(translated_by_id) != len(lines):
        raise RuntimeError("Gemini の応答件数が入力件数と一致しません")
    return {source: translated_by_id[index] for index, source in enumerate(lines, start=1)}
