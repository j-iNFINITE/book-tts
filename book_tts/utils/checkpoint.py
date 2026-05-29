"""Checkpoint manager for resumable conversions."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class CheckpointManager:
    """Tracks chapter completion status and enables resuming interrupted conversions.

    The checkpoint file is written atomically (write to .tmp then os.replace)
    to avoid corruption on crash.
    """

    def __init__(self, checkpoint_path: Path) -> None:
        self._path = checkpoint_path
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        """Load checkpoint from disk if it exists."""
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
                done = self.get_completed()
                logger.info(
                    "Loaded checkpoint with %d completed chapters", len(done)
                )
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("Corrupt checkpoint file, starting fresh: %s", exc)
                self._data = {"chapters": {}}
        else:
            self._data = {"chapters": {}}

    def _save(self) -> None:
        """Atomically save checkpoint to disk."""
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self._path)

    def is_done(self, chapter_index: int) -> bool:
        """Check if *chapter_index* was already completed."""
        return (
            self._data["chapters"].get(str(chapter_index), {}).get("status")
            == "done"
        )

    def mark_done(self, chapter_index: int, title: str, output_path: str) -> None:
        """Record *chapter_index* as successfully completed."""
        self._data["chapters"][str(chapter_index)] = {
            "index": chapter_index,
            "title": title,
            "status": "done",
            "output_path": output_path,
        }
        self._save()
        logger.debug("Checkpoint: chapter %d marked done", chapter_index)

    def mark_error(self, chapter_index: int, error: str) -> None:
        """Record *chapter_index* as failed."""
        self._data["chapters"][str(chapter_index)] = {
            "index": chapter_index,
            "status": "error",
            "error": error,
        }
        self._save()
        logger.debug("Checkpoint: chapter %d marked error", chapter_index)

    def get_completed(self) -> list[int]:
        """Return sorted list of completed chapter indices."""
        return sorted(
            int(k)
            for k, v in self._data["chapters"].items()
            if v.get("status") == "done"
        )

    def get_summary(self) -> dict:
        """Return checkpoint summary for display."""
        completed = self.get_completed()
        errors = [
            int(k)
            for k, v in self._data["chapters"].items()
            if v.get("status") == "error"
        ]
        return {
            "completed_count": len(completed),
            "error_count": len(errors),
            "completed_indices": completed,
            "has_checkpoint": bool(self._data["chapters"]),
        }

    def clear(self) -> None:
        """Clear the checkpoint file."""
        self._data = {"chapters": {}}
        if self._path.exists():
            self._path.unlink()
            logger.info("Checkpoint cleared")
