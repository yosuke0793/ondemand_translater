"""Escaping for user-controlled values embedded in FFmpeg filtergraph strings."""

from __future__ import annotations

# FFmpeg unescapes a filter argument twice with av_get_token(): first while splitting
# the filtergraph into filters, then while splitting the filter's options. Quoting
# with '...' cannot represent a literal quote, so values are backslash-escaped
# unquoted instead, innermost level first.
_OPTION_SPECIALS = "\\':"
_GRAPH_SPECIALS = "\\'[],;"
_DRAWTEXT_SPECIALS = "\\%"
_TOKEN_WHITESPACE = " \n\t\r"


def _escape_token(value: str, specials: str) -> str:
    escaped = ["\\" + char if char in specials else char for char in value]
    # av_get_token() also drops unescaped whitespace at either end of a token.
    for index in (0, -1):
        if escaped and escaped[index] in _TOKEN_WHITESPACE:
            escaped[index] = "\\" + escaped[index]
    return "".join(escaped)


def escape_filter_value(value: str) -> str:
    """Return an unquoted option value that FFmpeg's filter parser reads back as ``value``."""
    return _escape_token(_escape_token(value, _OPTION_SPECIALS), _GRAPH_SPECIALS)


def escape_drawtext_text(text: str) -> str:
    """Escape drawtext ``text`` so it renders literally with the default text expansion."""
    return escape_filter_value(_escape_token(text, _DRAWTEXT_SPECIALS))
