"""Thread-safe progress tracker with cancellation support."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from book_tts.models import ConversionProgress, ConversionStatus


class ProgressTracker:
    """Thread-safe wrapper around :class:`ConversionProgress`.

    Uses :class:`threading.Event` for cooperative cancellation and
    :class:`threading.Lock` to protect mutable state.
    """

    def __init__(self, total_chapters: int = 0) -> None:
        from book_tts.models import ConversionProgress, ConversionStatus

        self._progress = ConversionProgress(
            status=ConversionStatus.IDLE,
            total_chapters=total_chapters,
        )
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._start_time: float = 0.0
        self._listeners: list[Callable[[ConversionProgress], None]] = []

    # ── Cancellation ──────────────────────────────────────────────────────

    def request_cancel(self) -> None:
        """Signal all workers to stop cooperatively."""
        self._cancel_event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    # ── Listeners ─────────────────────────────────────────────────────────

    def add_listener(self, callback: Callable[[ConversionProgress], None]) -> None:
        """Register *callback* to be invoked on every progress update."""
        self._listeners.append(callback)

    def _notify(self) -> None:
        for cb in self._listeners:
            try:
                cb(self._progress)
            except Exception:
                # Never let a broken listener crash the pipeline.
                pass

    # ── State mutations ───────────────────────────────────────────────────

    def start(self) -> None:
        """Mark the conversion as started."""
        with self._lock:
            self._start_time = time.monotonic()
            self._progress.status = self._progress.status.CONVERTING
            self._progress.elapsed_seconds = 0.0
            self._notify()

    def update_chapter(
        self,
        current: int,
        total: int | None = None,
        *,
        message: str = "",
    ) -> None:
        with self._lock:
            self._progress.current_chapter = current
            if total is not None:
                self._progress.total_chapters = total
            self._progress.message = message
            self._update_timing()
            self._notify()

    def update_paragraph(self, current: int, total: int | None = None) -> None:
        with self._lock:
            self._progress.current_paragraph = current
            if total is not None:
                self._progress.total_paragraphs = total
            self._update_timing()
            self._notify()

    def add_chapter_file(self, path: object) -> None:
        with self._lock:
            self._progress.chapter_files.append(path)  # type: ignore[arg-type]
            self._notify()

    def finish(self, status: ConversionStatus | None = None) -> None:
        """Mark the conversion as finished."""
        from book_tts.models import ConversionStatus

        with self._lock:
            if status is not None:
                self._progress.status = status
            elif self.is_cancelled:
                self._progress.status = ConversionStatus.CANCELLED
            else:
                self._progress.status = ConversionStatus.COMPLETED
            self._update_timing()
            self._notify()

    def set_error(self, message: str) -> None:
        from book_tts.models import ConversionStatus

        with self._lock:
            self._progress.status = ConversionStatus.ERROR
            self._progress.message = message
            self._notify()

    # ── Read access ───────────────────────────────────────────────────────

    @property
    def snapshot(self) -> ConversionProgress:
        """Return a read-only snapshot of the current progress."""
        with self._lock:
            return self._progress

    # ── Internal helpers ──────────────────────────────────────────────────

    def _update_timing(self) -> None:
        """Recalculate elapsed / ETA (called while lock is held)."""
        if self._start_time == 0.0:
            return
        elapsed = time.monotonic() - self._start_time
        self._progress.elapsed_seconds = round(elapsed, 1)

        done = self._progress.current_chapter
        total = self._progress.total_chapters
        if done > 0 and total > done:
            avg = elapsed / done
            self._progress.estimated_remaining = round(avg * (total - done), 1)
