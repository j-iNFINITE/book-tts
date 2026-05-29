"""Voice, style, API keys, and base URL history persistence.

Stores unique voice/style values the user has entered so the GUI can
offer them as dropdown suggestions.  Also remembers the last-used API
keys and base URL so the user doesn't have to re-enter them.
"""

from __future__ import annotations

import json
from pathlib import Path


def _history_path() -> Path:
    base = Path.home() / ".book_tts"
    base.mkdir(parents=True, exist_ok=True)
    return base / "history.json"


def load_history() -> tuple[list[str], list[str], list[str], str]:
    """Return (voices, styles, api_keys, base_url) from history file."""
    path = _history_path()
    if not path.is_file():
        return [], [], [], ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (
            data.get("voices", []),
            data.get("styles", []),
            data.get("api_keys", []),
            data.get("base_url", ""),
        )
    except (json.JSONDecodeError, OSError):
        return [], [], [], ""


def save_history(
    voices: list[str],
    styles: list[str],
    api_keys: list[str] | None = None,
    base_url: str | None = None,
) -> None:
    """Persist voice, style, API keys, and base URL lists to disk."""
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {"voices": voices, "styles": styles}
    if api_keys is not None:
        data["api_keys"] = api_keys
    if base_url is not None:
        data["base_url"] = base_url
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def record_voice(voice: str) -> list[str]:
    """Record a voice value if it's new.  Returns the updated voice list."""
    voices, styles, api_keys, base_url = load_history()
    v = voice.strip()
    if v and v not in voices:
        voices.insert(0, v)
        save_history(voices, styles, api_keys, base_url)
    return voices


def record_style(style: str) -> list[str]:
    """Record a style value if it's new.  Returns the updated style list."""
    voices, styles, api_keys, base_url = load_history()
    s = style.strip()
    if s and s not in styles:
        styles.insert(0, s)
        save_history(voices, styles, api_keys, base_url)
    return styles


def record_api_keys(api_keys: list[str]) -> None:
    """Persist the list of API keys (replaces the full list)."""
    voices, styles, _, base_url = load_history()
    save_history(voices, styles, api_keys, base_url)


def record_base_url(base_url: str) -> None:
    """Persist the API base URL."""
    voices, styles, api_keys, _ = load_history()
    save_history(voices, styles, api_keys, base_url)


def record(
    voice: str = "",
    style: str = "",
    api_keys: list[str] | None = None,
    base_url: str | None = None,
) -> None:
    """Record voice, style, API keys, and/or base URL if they are non-empty and new."""
    voices, styles, cur_api_keys, cur_base_url = load_history()
    changed = False
    v = voice.strip()
    if v and v not in voices:
        voices.insert(0, v)
        changed = True
    s = style.strip()
    if s and s not in styles:
        styles.insert(0, s)
        changed = True
    new_api_keys = api_keys if api_keys is not None else cur_api_keys
    new_base_url = base_url if base_url is not None else cur_base_url
    if api_keys is not None:
        changed = True
    if base_url is not None:
        changed = True
    if changed:
        save_history(voices, styles, new_api_keys, new_base_url)
