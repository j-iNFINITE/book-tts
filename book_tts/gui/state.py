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
from book_tts.parsers.epub_parser import EPUBParser
from book_tts.parsers.mobi_parser import MOBIParser
from book_tts.parsers.markdown_parser import MarkdownParser
from book_tts.pipeline import (
    ConversionPipeline,
    PipelineConfig,
    PipelineEvent,
    CompletedEvent,
    CancelledEvent,
    ErrorEvent,
)
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
        self._parse_result: Optional[ParseResult] = None
        self._selected_chapters: List[int] = []
        self._output_dir: Path = Path(DEFAULT_OUTPUT_DIR)
        self._pipeline: Optional[ConversionPipeline] = None
        self._conversion_thread: Optional[threading.Thread] = None
        self._epub_parser = EPUBParser()
        self._mobi_parser = MOBIParser()
        self._md_parser = MarkdownParser()

    # ── Properties ────────────────────────────────────────────────────────

    @property
    def parse_result(self) -> Optional[ParseResult]:
        with self._lock:
            return self._parse_result

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

    # ── Parsing ───────────────────────────────────────────────────────────

    def parse_file(self, file_path: str | Path) -> ParseResult:
        """Parse an EPUB or MOBI file and store the result.

        Returns the ParseResult for immediate use by the caller.
        Raises ValueError for unsupported formats, FileNotFoundError if missing.
        """
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {path}")

        suffix = path.suffix.lower()
        if suffix == ".epub":
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
            self._parse_result = result
            self._selected_chapters = list(range(len(result.chapters)))

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
    ) -> None:
        """Start conversion in a background thread.

        Raises RuntimeError if already converting or nothing is parsed.
        """
        with self._lock:
            if self._pipeline is not None and self._pipeline.is_running:
                raise RuntimeError("Conversion already in progress")
            if self._parse_result is None:
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

        self._conversion_thread = threading.Thread(
            target=self._run_conversion,
            args=(tts_config, input_p, chapters, out_dir),
            daemon=True,
            name="conversion-worker",
        )
        self._conversion_thread.start()

    def _run_conversion(
        self,
        tts_config: TTSConfig,
        input_path: Path,
        chapter_indices: List[int],
        output_dir: Path,
    ) -> None:
        config = PipelineConfig(tts=tts_config, output_dir=output_dir)
        tracker = ProgressTracker(total_chapters=len(chapter_indices))
        pipeline = ConversionPipeline(config, tracker=tracker)

        with self._lock:
            self._pipeline = pipeline

        try:
            for _event in pipeline.convert(input_path, chapter_indices):
                pass  # Progress is driven through the shared ProgressTracker
        except Exception as exc:
            logger.error("Conversion failed: %s", exc, exc_info=True)
            tracker.set_error(str(exc))

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
            self._parse_result = None
            self._selected_chapters = []
            self._pipeline = None
            logger.info("State reset")
