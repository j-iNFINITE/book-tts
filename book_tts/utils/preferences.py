"""User preference persistence for GUI settings."""

from __future__ import annotations

import json
from pathlib import Path


def _prefs_path() -> Path:
    base = Path.home() / ".book_tts"
    base.mkdir(parents=True, exist_ok=True)
    return base / "preferences.json"


def load() -> dict:
    path = _prefs_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save(prefs: dict) -> None:
    path = _prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def get(key: str, default: str = "") -> str:
    return load().get(key, default)


def set(key: str, value: str) -> None:
    prefs = load()
    prefs[key] = value
    save(prefs)
