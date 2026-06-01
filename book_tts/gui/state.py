"""Conversion state management for the Gradio GUI.

Thread-safe session state that coordinates parsing, chapter selection,
and delegates conversion to the ConversionPipeline.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import List, Optional

from book_tts.config import DEFAULT_OUTPUT_DIR, DEFAULT_VOICE
from book_tts.models import (
    ConversionStatus,
    ParseResult,
    TTSConfig,
)
from book_tts.parsers.epub_parser import EPUBParser, EPUBHTMLParser
from book_tts.parsers.mobi_parser import MOBIParser
from book_tts.parsers.markdown_parser import MarkdownParser
from book_tts.pipeline import (
    ConversionPipeline,
    PipelineConfig,
    PipelineEvent,
    ChapterDoneEvent,
    CompletedEvent,
    CancelledEvent,
    ErrorEvent,
)
from book_tts.utils.checkpoint import CheckpointManager
from book_tts.utils.progress import ProgressTracker

logger = logging.getLogger(__name__)


class ConversionState:
    """Manages a single conversion session with thread-safe state.

    Parsing is handled directly by parser instances held here so the
    GUI can preview chapters before conversion.  Conversion delegates
    to :class:`ConversionPipeline`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._parse_results: dict[str, ParseResult] = {}
        self._selected_chapters: List[int] = []
        self._output_dir: Path = Path(DEFAULT_OUTPUT_DIR)
        self._pipeline: Optional[ConversionPipeline] = None
        self._conversion_thread: Optional[threading.Thread] = None
        self._epub_parser = EPUBParser()
        self._epub_html_parser = EPUBHTMLParser()
        self._mobi_parser = MOBIParser()
        self._md_parser = MarkdownParser()
        self._checkpoint_summary: Optional[dict] = None
        self._failed_chapters: set[int] = set()

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def parse_result(self) -> Optional[ParseResult]:
        with self._lock:
            if self._parse_results:
                return list(self._parse_results.values())[-1]
            return None

    def get_parse_result(self, filename: str) -> Optional[ParseResult]:
        with self._lock:
            return self._parse_results.get(filename)

    @property
    def selected_chapters(self) -> List[int]:
        with self._lock:
            return list(self._selected_chapters)

    @property
    def output_dir(self) -> Path:
        with self._lock:
            return self._output_dir

    @output_dir.setter
    def output_dir(self, value: Path) -> None:
        with self._lock:
            self._output_dir = value

    @property
    def is_converting(self) -> bool:
        with self._lock:
            if self._pipeline is not None:
                return self._pipeline.is_running
        return False

    @property
    def chapter_files(self) -> list[Path]:
        with self._lock:
            if self._pipeline is not None:
                return self._pipeline.chapter_files
        return []

    @property
    def checkpoint_summary(self) -> Optional[dict]:
        with self._lock:
            return self._checkpoint_summary

    @property
    def failed_chapters(self) -> set[int]:
        with self._lock:
            return set(self._failed_chapters)

    def add_failed_chapter(self, idx: int) -> None:
        with self._lock:
            self._failed_chapters.add(idx)

    def clear_failed_chapters(self) -> None:
        with self._lock:
            self._failed_chapters.clear()

    def check_checkpoint(self, file_path: str | Path, output_dir: Optional[Path] = None) -> Optional[dict]:
        """Check if a checkpoint exists for the given file.

        Returns checkpoint summary dict if found, None otherwise.
        """
        path = Path(file_path)
        out_dir = output_dir or self._output_dir
        book_dir = out_dir / path.stem
        checkpoint_path = book_dir / "checkpoint.json"

        if not checkpoint_path.exists():
            return None

        try:
            mgr = CheckpointManager(checkpoint_path)
            summary = mgr.get_summary()
            if summary["has_checkpoint"] and summary["completed_count"] > 0:
                return summary
        except Exception:
            pass
        return None

    # ── Parsing ───────────────────────────────────────────────────────────

    def parse_file(self, file_path: str | Path, use_html_parser: bool = False) -> ParseResult:
        """Parse an EPUB or MOBI file and store the result.

        Returns the ParseResult for immediate use by the caller.
        Raises ValueError for unsupported formats, FileNotFoundError if missing.
        """
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {path}")

        suffix = path.suffix.lower()
        if suffix == ".epub":
            if use_html_parser:
                result = self._epub_html_parser.parse(path)
            else:
                result = self._epub_parser.parse(path)
        elif suffix in (".mobi", ".azw", ".azw3"):
            result = self._mobi_parser.parse(path)
        elif suffix in (".md", ".markdown"):
            result = self._md_parser.parse(path)
        else:
            raise ValueError(
                f"Unsupported format: {suffix}. "
                "Please upload an EPUB or MOBI file."
            )

        with self._lock:
            self._parse_results[path.name] = result
            self._selected_chapters = list(range(len(result.chapters)))
            self._checkpoint_summary = self.check_checkpoint(path)

        logger.info(
            "Parsed %s: %d chapters found", path.name, len(result.chapters)
        )
        return result

    def set_selected_chapters(self, chapter_indices: List[int]) -> None:
        with self._lock:
            self._selected_chapters = list(chapter_indices)

    # ── Conversion ────────────────────────────────────────────────────────

    def start_conversion(
        self,
        voice: str = DEFAULT_VOICE,
        style: str = "",
        api_keys: Optional[List[str]] = None,
        base_url: str = "",
        input_path: Optional[Path] = None,
        output_dir: Optional[Path] = None,
        resume: bool = False,
        output_format: str = "mp3",
    ) -> None:
        """Start conversion in a background thread.

        Raises RuntimeError if already converting or nothing is parsed.
        """
        with self._lock:
            if self._pipeline is not None and self._pipeline.is_running:
                raise RuntimeError("Conversion already in progress")
            if self.parse_result is None:
                raise RuntimeError("No file parsed yet")
            if not self._selected_chapters:
                raise RuntimeError("No chapters selected")

            if output_dir is not None:
                self._output_dir = output_dir

            out_dir = self._output_dir
            chapters = list(self._selected_chapters)
            input_p = input_path or Path(".")

        keys = tuple(api_keys) if api_keys else ()
        tts_config = TTSConfig(
            api_keys=keys,
            voice=voice,
            style=style,
            base_url=base_url,
        )

        config = PipelineConfig(tts=tts_config, output_dir=out_dir, output_format=output_format)
        tracker = ProgressTracker(total_chapters=len(chapters))
        pipeline = ConversionPipeline(config, tracker=tracker)

        with self._lock:
            self._pipeline = pipeline
            pipeline._is_running = True

        self._conversion_thread = threading.Thread(
            target=self._run_conversion,
            args=(pipeline, input_p, chapters, resume, self._parse_results.get(input_p.name)),
            daemon=True,
            name="conversion-worker",
        )
        self._conversion_thread.start()

    def _run_conversion(
        self,
        pipeline: ConversionPipeline,
        input_path: Path,
        chapter_indices: List[int],
        resume: bool = False,
        parse_result: Optional[ParseResult] = None,
    ) -> None:
        try:
            successful: set[int] = set()
            for event in pipeline.convert(input_path, chapter_indices, resume=resume, parse_result=parse_result):
                if isinstance(event, ChapterDoneEvent):
                    successful.add(event.index)
            failed = set(chapter_indices) - successful
            with self._lock:
                self._failed_chapters = failed
        except Exception as exc:
            logger.error("Conversion failed: %s", exc, exc_info=True)
            tracker.set_error(str(exc))
            with self._lock:
                self._failed_chapters = set(chapter_indices)

    def cancel(self) -> None:
        with self._lock:
            if self._pipeline is not None:
                self._pipeline.cancel()
                logger.info("Cancellation requested")

    def get_progress(self):
        """Return the current progress snapshot for GUI polling."""
        from book_tts.models import ConversionProgress

        with self._lock:
            if self._pipeline is not None:
                return self._pipeline.progress
        return ConversionProgress(status=ConversionStatus.IDLE)

    # ── Reset ─────────────────────────────────────────────────────────────

    def reset(self) -> None:
        with self._lock:
            if self._pipeline is not None and self._pipeline.is_running:
                self._pipeline.cancel()
            self._parse_results.clear()
            self._selected_chapters = []
            self._pipeline = None
            self._failed_chapters.clear()
            logger.info("State reset")
