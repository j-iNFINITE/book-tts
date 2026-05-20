"""File-system helpers used across the package."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any


def sanitize_filename(name: str, *, max_length: int = 200) -> str:
    """Return *name* stripped of characters unsafe for file-system paths.

    Collapses whitespace, removes OS-reserved characters, and truncates
    to *max_length* characters (excluding the extension if present).
    """
    # Replace characters that are problematic on Windows / Linux / macOS.
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    # Collapse runs of underscores / whitespace.
    cleaned = re.sub(r"[_\s]+", "_", cleaned).strip("_ .")
    if not cleaned:
        cleaned = "untitled"
    return cleaned[:max_length]


def safe_json_load(path: Path, *, default: Any = None) -> Any:
    """Load a JSON file, returning *default* when the file is missing or corrupt."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def safe_json_save(path: Path, data: Any, *, indent: int = 2) -> None:
    """Atomically write *data* as JSON to *path*.

    Writes to a temporary file first, then renames – avoids partial writes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=indent),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        # Clean up partial write on failure.
        tmp.unlink(missing_ok=True)
        raise


def check_ffmpeg() -> bool:
    """Return ``True`` if ``ffmpeg`` is reachable on ``$PATH``."""
    return shutil.which("ffmpeg") is not None
