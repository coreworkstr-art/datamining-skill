"""Decodes the escapes of a JSON string literal in plain text, without parsing the document."""

from __future__ import annotations

import re

_ESCAPE = re.compile(
    r"\\u(?P<high>[dD][89abAB][0-9a-fA-F]{2})\\u(?P<low>[dD][c-fC-F][0-9a-fA-F]{2})"
    r"|\\u(?P<code>[0-9a-fA-F]{4})"
    r'|\\(?P<simple>["\\/bfnrt])'
)
_SIMPLE = {'"': '"', "\\": "\\", "/": "/"}


def _replace(match: re.Match[str]) -> str:
    if match["high"] is not None:
        pair = ((int(match["high"], 16) - 0xD800) << 10) + (int(match["low"], 16) - 0xDC00)
        return chr(0x10000 + pair)
    if match["code"] is not None:
        code = int(match["code"], 16)
        if 0xD800 <= code <= 0xDFFF:
            return "\N{REPLACEMENT CHARACTER}"  # a lone surrogate has no text form
        return " " if code < 0x20 else chr(code)
    return _SIMPLE.get(match["simple"], " ")


def decode_json_escapes(text: str) -> str:
    """Replace ``\\uXXXX``, ``\\/``, ``\\"`` and ``\\\\`` by the characters they stand for.

    A JSON file may write ``a@b.com`` as ``a\\u0040b.com``, which a search of the raw text
    would miss. Escapes that stand for control characters (``\\n``, ``\\t``, ``\\u000a``)
    become a space, so a decoded value never contains a line break.
    """
    if "\\" not in text:
        return text
    return _ESCAPE.sub(_replace, text)
