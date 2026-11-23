from __future__ import annotations

import os
import re
from collections.abc import Iterable
from urllib.parse import quote, quote_plus, unquote


_REDACTED = "[REDACTED]"
_SECRET_NAMES = r"key|[\w-]*api[_-]?key|[\w-]*token|[\w-]*secret|[\w-]*password|authorization|azure_speech_key|ocp-apim-subscription-key"
_QUERY_PARAM = re.compile(r"([?&;])([^?&=;\s]+)=([^&#;\s\"'<>]*)")
_SECRET_FIELD = re.compile(
    rf"""(?ix)(?<![\w-])((?:['"])?(?:{_SECRET_NAMES})(?:['"])?\s*[:=]\s*)
    (?:\s*(?:Bearer|Basic|DeepL-Auth-Key)\s+)?
    (?: "(?:\\.|[^"\\])*" | '(?:\\.|[^'\\])*' | \[REDACTED\] | [^\s,;)\]}}]+ )"""
)
_CLI_SECRET = re.compile(
    r"""(?ix)(--(?:gemini|deepl)-api-key(?:=|\s+))(?:"[^"]*"|'[^']*'|\[REDACTED\]|\S+)"""
)
_AUTH_VALUE = re.compile(r"(?i)\b(Bearer|DeepL-Auth-Key)(\s+)(?!\[REDACTED\])\S+")


def redact_secrets(value: object, *, secrets: Iterable[str] = ()) -> str:
    text = "" if value is None else str(value)
    known = {*secrets, *(os.getenv(name, "") for name in (
        "GEMINI_API_KEY", "DEEPL_API_KEY", "AZURE_SPEECH_KEY",
    ))}
    variants = {encoded for secret in known if secret for encoded in (
        secret, quote(secret, safe=""), quote_plus(secret, safe=""),
    )}
    for secret in sorted(variants, key=len, reverse=True):
        text = text.replace(secret, _REDACTED)

    def redact_param(match: re.Match[str]) -> str:
        separator, name, _ = match.groups()
        if re.fullmatch(_SECRET_NAMES, unquote(name), re.IGNORECASE):
            return f"{separator}{name}={_REDACTED}"
        return match[0]

    text = _QUERY_PARAM.sub(redact_param, text)
    text = _SECRET_FIELD.sub(lambda match: match[1] + _REDACTED, text)
    text = _CLI_SECRET.sub(lambda match: match[1] + _REDACTED, text)
    return _AUTH_VALUE.sub(lambda match: match[1] + match[2] + _REDACTED, text)
