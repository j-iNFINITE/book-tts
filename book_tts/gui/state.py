"""Conversion state management for the Gradio GUI.

Thread-safe session state that coordinates parsing, chapter selection,
TTS synthesis, and audio merging across the GUI lifecycle.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import List, Optional

from book_tts.audio.merger import AudioMerger
from book_tts.config import DEFAULT_OUTPUT_DIR, DEFAULT_VOICE, AUDIO_FORMAT
from book_tts.models import (
    ConversionProgress,
    ConversionStatus,
    ParseResult,
    TTSConfig,
)
from book_tts.parsers.epub_parser import EPUBParser
from book_tts.parsers.mobi_parser import MOBIParser
from book_tts.parsers.markdown_parser import MarkdownParser
from book_tts.parsers.text_cleaner import TextCleaner
from book_tts.tts.client import MiMoTTSClient
from book_tts.tts.synthesizer import ParagraphSynthesizer
from book_tts.utils.file_utils import sanitize_filename
from book_tts.utils.progress import ProgressTracker

logger = logging.getLogger(__name__)


class ConversionState:
    """Manages a single conversion session with thread-safe state.

    Coordinates the full pipeline: file parsing → chapter selection →
    TTS synthesis → audio merging, while exposing progress to the GUI.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._parse_result: Optional[ParseResult] = None
        self._selected_chapters: List[int] = []
        self._output_dir: Path = Path(DEFAULT_OUTPUT_DIR)
        self._is_converting: bool = False
        self._conversion_thread: Optional[threading.Thread] = None
        self._progress_tracker: Optional[ProgressTracker] = None
        self._epub_parser = EPUBParser()
        self._mobi_parser = MOBIParser()
        self._md_parser = MarkdownParser()
        self._cleaner = TextCleaner()

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
            return self._is_converting

    @property
    def progress_tracker(self) -> Optional[ProgressTracker]:
        with self._lock:
            return self._progress_tracker

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
        output_dir: Optional[Path] = None,
    ) -> None:
        """Start conversion in a background thread.

        Raises RuntimeError if already converting or nothing is parsed.
        """
        with self._lock:
            if self._is_converting:
                raise RuntimeError("Conversion already in progress")
            if self._parse_result is None:
                raise RuntimeError("No file parsed yet")
            if not self._selected_chapters:
                raise RuntimeError("No chapters selected")

            self._is_converting = True
            if output_dir is not None:
                self._output_dir = output_dir

        keys = tuple(api_keys) if api_keys else ()
        tts_config = TTSConfig(
            api_keys=keys,
            voice=voice,
            style=style,
            base_url=base_url,
        )

        with self._lock:
            result = self._parse_result
            chapters = list(self._selected_chapters)
            out_dir = self._output_dir

        self._conversion_thread = threading.Thread(
            target=self._run_conversion,
            args=(tts_config, result, chapters, out_dir),
            daemon=True,
            name="conversion-worker",
        )
        self._conversion_thread.start()

    def _run_conversion(
        self,
        tts_config: TTSConfig,
        parse_result: ParseResult,
        chapter_indices: List[int],
        output_dir: Path,
    ) -> None:
        tracker = ProgressTracker(total_chapters=len(chapter_indices))
        with self._lock:
            self._progress_tracker = tracker

        try:
            import shutil

            client = MiMoTTSClient(tts_config)
            synthesizer = ParagraphSynthesizer(
                tts_client=client,
                cleaner=self._cleaner,
                progress_tracker=tracker,
            )
            merger = AudioMerger()

            tracker.start()

            # Match CLI directory structure: output_dir/book_name/chapters/
            book_stem = Path(parse_result.metadata.title or "audiobook").stem
            book_dir = output_dir / book_stem
            chapters_dir = book_dir / "chapters"
            chapters_dir.mkdir(parents=True, exist_ok=True)

            chapter_audio_files: list[Path] = []

            for chapter_pos, chapter_idx in enumerate(chapter_indices):
                if tracker.is_cancelled:
                    break

                chapter = parse_result.chapters[chapter_idx]
                ch_title = chapter.title
                safe_name = sanitize_filename(ch_title) or f"chapter_{chapter_idx:04d}"
                chapter_output = chapters_dir / f"{chapter_idx:04d}_{safe_name}.{AUDIO_FORMAT}"

                tracker.update_chapter(
                    chapter_pos,
                    len(chapter_indices),
                    message=f"Synthesizing chapter {chapter_pos + 1}/{len(chapter_indices)}: {ch_title}",
                )

                para_dir = chapters_dir / f"_para_{chapter_idx:04d}"
                para_dir.mkdir(parents=True, exist_ok=True)

                para_files = synthesizer.synthesize_chapter(
                    paragraphs=list(chapter.paragraphs),
                    output_dir=para_dir,
                    chapter_index=chapter_idx,
                    total_chapters=len(chapter_indices),
                )

                if tracker.is_cancelled:
                    break

                if para_files:
                    merged = merger.merge_to_chapter(para_files, chapter_output)
                    if merged:
                        chapter_audio_files.append(merged)
                        tracker.add_chapter_file(merged)

                shutil.rmtree(para_dir, ignore_errors=True)

            if chapter_audio_files and not tracker.is_cancelled:
                tracker.finish(ConversionStatus.COMPLETED)
            elif tracker.is_cancelled:
                tracker.finish(ConversionStatus.CANCELLED)
            else:
                tracker.finish(ConversionStatus.COMPLETED)

        except Exception as exc:
            logger.error("Conversion failed: %s", exc, exc_info=True)
            tracker.set_error(str(exc))
        finally:
            with self._lock:
                self._is_converting = False

    def cancel(self) -> None:
        with self._lock:
            if self._progress_tracker is not None:
                self._progress_tracker.request_cancel()
                logger.info("Cancellation requested")

    def get_progress(self) -> ConversionProgress:
        with self._lock:
            if self._progress_tracker is not None:
                return self._progress_tracker.snapshot
        return ConversionProgress(status=ConversionStatus.IDLE)

    # ── Reset ─────────────────────────────────────────────────────────────

    def reset(self) -> None:
        with self._lock:
            if self._is_converting:
                self.cancel()
            self._parse_result = None
            self._selected_chapters = []
            self._progress_tracker = None
            logger.info("State reset")
