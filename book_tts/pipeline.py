"""Conversion pipeline: parse → synthesize → merge.

A single module with one interface for both CLI and GUI adapters.
"""

from __future__ import annotations

import logging
import shutil
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Optional

from book_tts.audio.merger import AudioMerger
from book_tts.audio.m4b_builder import M4BBuilder
from book_tts.config import AUDIO_FORMAT, DEFAULT_OUTPUT_DIR
from book_tts.markup import MarkupInjector
from book_tts.models import (
    BookMetadata,
    ConversionProgress,
    ConversionStatus,
    ParseResult,
    TTSConfig,
)
from book_tts.parsers.epub_parser import EPUBParser
from book_tts.parsers.mobi_parser import MOBIParser
from book_tts.parsers.markdown_parser import MarkdownParser
from book_tts.parsers.base import BaseBookParser
from book_tts.parsers.text_cleaner import TextCleaner
from book_tts.tts.synthesizer import ParagraphSynthesizer

if TYPE_CHECKING:
    from book_tts.tts.client import MiMoTTSClient
    from book_tts.tts.edge_client import EdgeTTSClient

from book_tts.tts.sml import strip_sml_tokens
from book_tts.utils.checkpoint import CheckpointManager
from book_tts.utils.file_utils import check_ffmpeg, sanitize_filename, safe_json_save
from book_tts.utils.progress import ProgressTracker

logger = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Immutable configuration for a conversion pipeline run."""

    tts: TTSConfig
    output_dir: Path = Path(DEFAULT_OUTPUT_DIR)
    output_format: str = "m4b"  # "mp3" or "m4b"


# ── Events ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    current: int
    total: int
    message: str


@dataclass(frozen=True, slots=True)
class ChapterDoneEvent:
    index: int
    title: str
    path: Path


@dataclass(frozen=True, slots=True)
class CompletedEvent:
    metadata: BookMetadata
    chapter_files: tuple[Path, ...]
    total_elapsed: float


@dataclass(frozen=True, slots=True)
class CancelledEvent:
    partial_files: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class ErrorEvent:
    error: str


PipelineEvent = ProgressEvent | ChapterDoneEvent | CompletedEvent | CancelledEvent | ErrorEvent


# ── Pipeline ──────────────────────────────────────────────────────────────────


def _select_parser(input_path: Path) -> BaseBookParser:
    ext = input_path.suffix.lower()
    if ext == ".epub":
        return EPUBParser()
    elif ext in (".mobi", ".azw", ".azw3"):
        return MOBIParser()
    elif ext in (".md", ".markdown"):
        return MarkdownParser()
    else:
        raise ValueError(f"Unsupported file format: {ext}")


def _save_podcast_metadata(
    output_dir: Path,
    metadata: BookMetadata,
    chapter_titles: list[str],
    chapter_files: list[Path],
) -> None:
    podcast = {
        "title": metadata.title or "Untitled",
        "author": metadata.author or "Unknown",
        "description": metadata.description or f"{metadata.title} - Audiobook",
        "language": metadata.language or "zh-CN",
        "explicit": False,
        "category": "Arts/Books",
        "chapters": [
            {"title": t, "file": str(p.name)}
            for t, p in zip(chapter_titles, chapter_files)
        ],
    }
    safe_json_save(output_dir / "podcast.json", podcast)


class ConversionPipeline:
    """Orchestrate the full parse → synthesize → merge pipeline.

    Usage::

        pipeline = ConversionPipeline(config)
        for event in pipeline.convert(Path("book.epub")):
            match event:
                case ProgressEvent(c, t, msg):
                    print(f"[{c}/{t}] {msg}")
                case ChapterDoneEvent(idx, title, path):
                    print(f"  Done: {title}")
                case CompletedEvent(meta, files, elapsed):
                    print(f"All done: {len(files)} chapters in {elapsed:.0f}s")

    Pass an optional *tracker* to share progress with an external poller
    (e.g. a GUI progress bar).
    """

    def __init__(
        self,
        config: PipelineConfig,
        tracker: Optional[ProgressTracker] = None,
    ) -> None:
        self._config = config
        self._tracker: Optional[ProgressTracker] = tracker
        self._cancel_event = threading.Event()
        self._chapter_files: list[Path] = []
        self._is_running = False
        self._lock = threading.Lock()

    # ── Read-only properties for external pollers ──────────────────────────

    @property
    def progress(self) -> ConversionProgress:
        """Return the latest progress snapshot (thread-safe)."""
        if self._tracker is not None:
            return self._tracker.snapshot
        return ConversionProgress(status=ConversionStatus.IDLE)

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._is_running

    @property
    def chapter_files(self) -> list[Path]:
        with self._lock:
            return list(self._chapter_files)

    # ── Public interface ───────────────────────────────────────────────────

    def convert(
        self,
        input_path: Path,
        chapter_indices: Optional[list[int]] = None,
        resume: bool = False,
        parse_result: Optional[ParseResult] = None,
    ) -> Iterator[PipelineEvent]:
        """Run the full conversion pipeline, yielding events as progress is made."""
        self._cancel_event.clear()
        if not check_ffmpeg():
            raise RuntimeError(
                "ffmpeg not found. Please install it: "
                "https://ffmpeg.org/download.html"
            )
        with self._lock:
            self._is_running = True
            self._chapter_files = []
        start_time = time.monotonic()

        if parse_result is None:
            parse_result = self._parse(input_path)
        chapters = parse_result.chapters
        if not chapters:
            yield ErrorEvent("No chapters found in the ebook.")
            with self._lock:
                self._is_running = False
            return

        selected = list(chapter_indices) if chapter_indices else list(range(len(chapters)))
        if not selected:
            yield ErrorEvent("No chapters selected.")
            with self._lock:
                self._is_running = False
            return

        book_name = self._get_book_name(parse_result.metadata.title, input_path.stem)
        book_dir, chapters_dir = self._prepare_dirs(input_path, book_name=book_name)

        checkpoint: Optional[CheckpointManager] = None
        if resume:
            checkpoint = CheckpointManager(book_dir / "checkpoint.json")

        if self._config.tts.api_keys:
            from book_tts.tts.client import MiMoTTSClient

            client: MiMoTTSClient | EdgeTTSClient = MiMoTTSClient(self._config.tts)
        else:
            try:
                from book_tts.tts.edge_client import EdgeTTSClient

                client = EdgeTTSClient(voice=self._config.tts.voice)
                logger.info("Using EdgeTTS fallback (no API key provided)")
            except ImportError:
                raise RuntimeError(
                    "No API keys provided and edge-tts not installed. "
                    "Install with: pip install book-tts[edge]"
                )
        try:
            cleaner = TextCleaner()
            merger = AudioMerger()
            injector = MarkupInjector()

            if self._tracker is None:
                self._tracker = ProgressTracker(total_chapters=len(selected))
            self._tracker.start()

            chapter_audio_paths: list[Path] = []
            chapter_titles: list[str] = []

            for pos, ch_idx in enumerate(selected):
                if self._cancel_event.is_set():
                    yield CancelledEvent(tuple(chapter_audio_paths))
                    with self._lock:
                        self._is_running = False
                    return

                chapter = chapters[ch_idx]
                ch_title = chapter.title
                safe_name = sanitize_filename(ch_title) or f"chapter_{ch_idx:04d}"
                chapter_output = chapters_dir / f"{ch_idx:04d}_{safe_name}.{AUDIO_FORMAT}"

                # Checkpoint: skip already-completed chapters
                if checkpoint is not None and checkpoint.is_done(ch_idx):
                    logger.info("Skipping chapter %d (already done): %s", ch_idx, ch_title)
                    chapter_audio_paths.append(chapter_output)
                    chapter_titles.append(ch_title)
                    with self._lock:
                        self._chapter_files.append(chapter_output)
                    if self._tracker is not None:
                        self._tracker.add_chapter_file(chapter_output)
                        self._tracker.update_chapter(pos + 1, total=len(selected),
                                                     message=f"Skipped: {ch_title}")
                    yield ChapterDoneEvent(
                        index=ch_idx,
                        title=ch_title,
                        path=chapter_output,
                    )
                    continue

                yield ProgressEvent(
                    current=pos + 1,
                    total=len(selected),
                    message=f"Synthesizing chapter {pos + 1}/{len(selected)}: {ch_title}",
                )

                # Inject SML tokens from boundary metadata
                tagged_paragraphs = injector.inject(
                    list(chapter.paragraphs),
                    list(chapter.boundaries) if chapter.boundaries else None,
                )

                synthesizer = ParagraphSynthesizer(
                    tts_client=client,
                    cleaner=cleaner,
                    progress_tracker=self._tracker,
                )

                para_dir = chapters_dir / f"_para_{ch_idx:04d}"
                para_dir.mkdir(parents=True, exist_ok=True)

                try:
                    para_paths = synthesizer.synthesize_chapter(
                        paragraphs=tagged_paragraphs,
                        output_dir=para_dir,
                        chapter_index=ch_idx,
                        total_chapters=len(selected),
                    )

                    if para_paths:
                        merged = merger.merge_to_chapter(
                            audio_paths=para_paths,
                            output_path=chapter_output,
                        )
                        if merged:
                            chapter_audio_paths.append(merged)
                            chapter_titles.append(ch_title)
                            with self._lock:
                                self._chapter_files.append(merged)
                            if self._tracker is not None:
                                self._tracker.add_chapter_file(merged)
                                self._tracker.update_chapter(pos + 1, total=len(selected),
                                                             message=f"Done: {ch_title}")
                            if checkpoint is not None:
                                checkpoint.mark_done(ch_idx, ch_title, str(merged))
                            yield ChapterDoneEvent(
                                index=ch_idx,
                                title=ch_title,
                                path=merged,
                            )
                except Exception as exc:
                    logger.error("Chapter %d failed: %s", ch_idx, exc)
                    if self._tracker is not None:
                        self._tracker.update_chapter(pos + 1, total=len(selected),
                                                     message=f"Failed: {ch_title}")
                    if checkpoint is not None:
                        checkpoint.mark_error(ch_idx, str(exc))
                    yield ProgressEvent(
                        current=pos + 1,
                        total=len(selected),
                        message=f"Error on chapter {ch_idx}: {exc}",
                    )

                shutil.rmtree(para_dir, ignore_errors=True)

            if not chapter_audio_paths:
                yield ErrorEvent("No chapter audio was generated.")
                with self._lock:
                    self._is_running = False
                return

            # ── Embed cover art in MP3 chapters ────────────────────────────
            if (
                self._config.output_format != "m4b"
                and parse_result.cover_image is not None
            ):
                for ch_path in chapter_audio_paths:
                    merger.embed_cover(ch_path, parse_result.cover_image)

            # ── Build M4B if requested ─────────────────────────────────────
            final_files: list[Path] = list(chapter_audio_paths)
            if self._config.output_format == "m4b":
                m4b_path = book_dir / f"{sanitize_filename(book_name)}.m4b"
                m4b_builder = M4BBuilder()
                try:
                    built = m4b_builder.build(
                        chapter_paths=chapter_audio_paths,
                        chapter_titles=chapter_titles,
                        output_path=m4b_path,
                        book_title=parse_result.metadata.title or input_path.stem,
                        book_author=parse_result.metadata.author or "",
                        cover_image=parse_result.cover_image,
                    )
                    final_files = [built]
                    with self._lock:
                        self._chapter_files = [built]
                    if self._tracker is not None:
                        self._tracker.add_chapter_file(built)
                    logger.info("M4B audiobook built: %s", built)
                    for ch_path in chapter_audio_paths:
                        if ch_path.exists():
                            ch_path.unlink(missing_ok=True)
                    shutil.rmtree(chapters_dir, ignore_errors=True)
                except Exception as exc:
                    logger.warning("M4B build failed, keeping chapter files: %s", exc)

            _save_podcast_metadata(
                output_dir=book_dir,
                metadata=parse_result.metadata,
                chapter_titles=chapter_titles,
                chapter_files=chapter_audio_paths,
            )

            self._tracker.finish(ConversionStatus.COMPLETED)
            elapsed = time.monotonic() - start_time
            with self._lock:
                self._is_running = False
            yield CompletedEvent(
                metadata=parse_result.metadata,
                chapter_files=tuple(final_files),
                total_elapsed=elapsed,
            )
        finally:
            client.close()

    def dry_run(
        self,
        input_path: Path,
        chapter_indices: Optional[list[int]] = None,
    ) -> Iterator[PipelineEvent]:
        """Parse and split text without TTS; write per-chapter .txt preview files."""
        self._cancel_event.clear()
        if not check_ffmpeg():
            raise RuntimeError(
                "ffmpeg not found. Please install it: "
                "https://ffmpeg.org/download.html"
            )

        parse_result = self._parse(input_path)
        chapters = parse_result.chapters
        if not chapters:
            yield ErrorEvent("No chapters found in the ebook.")
            return

        selected = list(chapter_indices) if chapter_indices else list(range(len(chapters)))
        if not selected:
            yield ErrorEvent("No chapters selected.")
            return

        book_name = self._get_book_name(parse_result.metadata.title, input_path.stem)
        _, dry_dir = self._prepare_dirs(input_path, book_name=book_name, subdir="_dry_text")

        cleaner = TextCleaner()
        injector = MarkupInjector()
        total_chunks = 0
        total_chars = 0
        chapter_done_paths: list[Path] = []

        for pos, ch_idx in enumerate(selected):
            if self._cancel_event.is_set():
                yield CancelledEvent(tuple(chapter_done_paths))
                return

            chapter = chapters[ch_idx]
            yield ProgressEvent(
                current=pos + 1,
                total=len(selected),
                message=f"Dry-run chapter {pos + 1}/{len(selected)}: {chapter.title}",
            )

            safe_name = sanitize_filename(chapter.title) or f"chapter_{ch_idx:04d}"
            out_path = dry_dir / f"{ch_idx:04d}_{safe_name}.txt"

            # Inject SML tokens so the multi-pass splitter sees structural boundaries
            tagged_paragraphs = injector.inject(
                list(chapter.paragraphs),
                list(chapter.boundaries) if chapter.boundaries else None,
            )

            lines: list[str] = [
                f"# {chapter.title}",
                f"# Paragraphs: {len(chapter.paragraphs)}",
                "",
            ]

            for para in tagged_paragraphs:
                cleaned = cleaner.clean(para)
                if not cleaned.strip():
                    continue
                chunks = ParagraphSynthesizer._split_text_multi_pass(cleaned)
                for chunk in chunks:
                    tts_text = strip_sml_tokens(chunk).strip()
                    if tts_text:
                        lines.append(tts_text.replace("\n", " "))
                        total_chunks += 1
                        total_chars += len(tts_text)

            out_path.write_text("\n".join(lines), encoding="utf-8")
            chapter_done_paths.append(out_path)

        yield ChapterDoneEvent(
            index=-1,
            title=f"Dry run complete: {total_chunks} chunks, {total_chars} chars",
            path=dry_dir,
        )

    def cancel(self) -> None:
        """Signal cancellation.  The pipeline will stop at the next checkpoint."""
        self._cancel_event.set()
        if self._tracker is not None:
            self._tracker.request_cancel()

    # ── Internal helpers ───────────────────────────────────────────────────

    def _parse(self, input_path: Path) -> ParseResult:
        logger.info("Parsing %s ...", input_path.name)
        parser = _select_parser(input_path)
        return parser.parse(input_path)

    @staticmethod
    def _get_book_name(metadata_title: str, fallback_stem: str) -> str:
        source = metadata_title.strip() if metadata_title else fallback_stem
        for sep in "（(：:—--｜|":
            idx = source.find(sep)
            if idx > 0:
                source = source[:idx].rstrip()
                break
        return source

    def _prepare_dirs(self, input_path: Path, book_name: str = "", subdir: str = "chapters") -> tuple[Path, Path]:
        book_stem = sanitize_filename(book_name) if book_name else sanitize_filename(input_path.stem)
        book_dir = self._config.output_dir / book_stem
        sub_dir = book_dir / subdir
        sub_dir.mkdir(parents=True, exist_ok=True)
        return book_dir, sub_dir
