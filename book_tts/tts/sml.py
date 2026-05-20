"""SML (Speech Markup Language) tokens for structural text boundaries.

Embedded in the text stream during parsing, these tokens survive the
cleaning pipeline and guide multi-pass sentence splitting.
"""

from __future__ import annotations

import re

# ── Token constants ──────────────────────────────────────────────────────────
SML_BREAK = "[break]"  # short pause between consecutive paragraphs
SML_PAUSE = "[pause]"  # longer pause between major sections / div boundaries

_SML_TOKENS: tuple[str, str] = (SML_BREAK, SML_PAUSE)

# Null-byte placeholders that survive all regex-based text cleaning.
_PLACEHOLDER_BREAK = "\x00SML_B\x00"
_PLACEHOLDER_PAUSE = "\x00SML_P\x00"

_TOKEN_TO_PLACEHOLDER = {
    SML_BREAK: _PLACEHOLDER_BREAK,
    SML_PAUSE: _PLACEHOLDER_PAUSE,
}

_PLACEHOLDER_TO_TOKEN = {v: k for k, v in _TOKEN_TO_PLACEHOLDER.items()}

# Matches any SML token or placeholder for stripping.
_SML_STRIP_PATTERN = re.compile(
    r"\[break\]|\[pause\]|\x00SML_[BP]\x00"
)

# Pattern that matches SML tokens for splitting.
_SML_SPLIT_PATTERN = re.compile(r"\[break\]|\[pause\]")


def protect_sml_tokens(text: str) -> str:
    """Replace SML tokens with null-byte placeholders immune to regex cleaning."""
    for token, placeholder in _TOKEN_TO_PLACEHOLDER.items():
        text = text.replace(token, placeholder)
    return text


def restore_sml_tokens(text: str) -> str:
    """Restore SML tokens from null-byte placeholders."""
    for placeholder, token in _PLACEHOLDER_TO_TOKEN.items():
        text = text.replace(placeholder, token)
    return text


def strip_sml_tokens(text: str) -> str:
    """Remove all SML tokens and placeholders from *text*.

    Called just before sending text to the TTS API.
    """
    return _SML_STRIP_PATTERN.sub("", text).strip()


def split_on_sml_tokens(text: str) -> list[str]:
    """Split *text* on SML token boundaries, dropping the tokens themselves."""
    parts = _SML_SPLIT_PATTERN.split(text)
    return [p.strip() for p in parts if p.strip()]


def has_sml_token(text: str) -> bool:
    """Return True if *text* contains any SML token or placeholder."""
    return bool(_SML_STRIP_PATTERN.search(text))


def intersperse_break_tokens(paragraphs: list[str]) -> list[str]:
    """Insert ``[break]`` tokens between consecutive non-empty paragraphs."""
    if not paragraphs:
        return paragraphs
    result: list[str] = []
    for i, para in enumerate(paragraphs):
        text = para.strip()
        if not text:
            continue
        if i < len(paragraphs) - 1:
            text = text + " [break]"
        result.append(text)
    return result
