from __future__ import annotations

import re


_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|authorization|bearer|deepl[_-]?api[_-]?key|gemini[_-]?api[_-]?key)"
)


def redact_secrets(value: object) -> str:
    text = "" if value is None else str(value)
    if not text:
        return text

    redacted = text

    # Query-style credentials (e.g. ?key=..., &token=...).
    redacted = re.sub(
        r"(?i)([?&](?:key|api[_-]?key|token|access[_-]?token)=)([^&\s]+)",
        r"\1[REDACTED]",
        redacted,
    )

    # CLI args that may carry credentials.
    redacted = re.sub(
        r"(?i)(--(?:gemini|deepl)-api-key\s+)(\S+)",
        r"\1[REDACTED]",
        redacted,
    )

    # Authorization header-like values.
    redacted = re.sub(r"(?i)(Bearer\s+)([A-Za-z0-9._\-]+)", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"(?i)(DeepL-Auth-Key\s+)(\S+)", r"\1[REDACTED]", redacted)

    # KEY=VALUE / KEY: VALUE forms.
    redacted = re.sub(
        r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)\b\s*([=:])\s*([^\s,;\]\)\}]+)",
        r"\1\2[REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\b(api[_-]?key|token|secret|password|authorization)\b\s*([=:])\s*([^\s,;\]\)\}]+)",
        r"\1\2[REDACTED]",
        redacted,
    )

    if _SENSITIVE_KEY_PATTERN.search(redacted):
        # Final pass for quoted key-value payloads: "apiKey": "..."
        redacted = re.sub(
            r"(?i)(\"(?:api[_-]?key|token|secret|password|authorization)\"\s*:\s*\")(.*?)(\")",
            r"\1[REDACTED]\3",
            redacted,
        )

    return redacted
