"""Voice and style history persistence.

Stores unique voice/style values the user has entered so the GUI can
offer them as dropdown suggestions.
"""

from __future__ import annotations

import json
from pathlib import Path


def _history_path() -> Path:
    base = Path.home() / ".book_tts"
    base.mkdir(parents=True, exist_ok=True)
    return base / "history.json"


def load_history() -> tuple[list[str], list[str]]:
    """Return (voices, styles) lists from history file."""
    path = _history_path()
    if not path.is_file():
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("voices", []), data.get("styles", [])
    except (json.JSONDecodeError, OSError):
        return [], []


def save_history(voices: list[str], styles: list[str]) -> None:
    """Persist voice and style lists to disk."""
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"voices": voices, "styles": styles}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def record_voice(voice: str) -> list[str]:
    """Record a voice value if it's new.  Returns the updated voice list."""
    voices, styles = load_history()
    v = voice.strip()
    if v and v not in voices:
        voices.insert(0, v)
        save_history(voices, styles)
    return voices


def record_style(style: str) -> list[str]:
    """Record a style value if it's new.  Returns the updated style list."""
    voices, styles = load_history()
    s = style.strip()
    if s and s not in styles:
        styles.insert(0, s)
        save_history(voices, styles)
    return styles


def record(voice: str = "", style: str = "") -> None:
    """Record voice and/or style if they are non-empty and new."""
    voices, styles = load_history()
    changed = False
    v = voice.strip()
    if v and v not in voices:
        voices.insert(0, v)
        changed = True
    s = style.strip()
    if s and s not in styles:
        styles.insert(0, s)
        changed = True
    if changed:
        save_history(voices, styles)
