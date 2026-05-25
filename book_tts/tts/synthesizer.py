"""Paragraph-level TTS synthesis orchestrator."""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, List, Optional

from book_tts.config import MAX_TTS_CHARS, MAX_WORKERS, DEFAULT_RPM_LIMIT
from book_tts.models import ConversionProgress, ConversionStatus
from book_tts.parsers.text_cleaner import TextCleaner
from book_tts.tts.client import MiMoTTSClient
from book_tts.tts.rate_limiter import RateLimiter
from book_tts.tts.sml import split_on_sml_tokens, strip_sml_tokens, has_sml_token
from book_tts.utils.progress import ProgressTracker

logger = logging.getLogger(__name__)


class ParagraphSynthesizer:
    """Synthesize paragraphs to audio files with parallelism and retry.

    Parameters
    ----------
    tts_client:
        Configured MiMo TTS client.
    cleaner:
        Text cleaner for pre-processing.
    progress_tracker:
        Thread-safe progress tracker for GUI updates.
    max_workers:
        Maximum parallel TTS threads.
    """

    def __init__(
        self,
        tts_client: MiMoTTSClient,
        cleaner: TextCleaner,
        progress_tracker: ProgressTracker,
        max_workers: int = MAX_WORKERS,
        rpm_limit: int = DEFAULT_RPM_LIMIT,
    ) -> None:
        self._client = tts_client
        self._cleaner = cleaner
        self._tracker = progress_tracker
        self._max_workers = max_workers
        self._rate_limiter = RateLimiter(max_calls=rpm_limit, period=60.0)

    def synthesize_chapter(
        self,
        paragraphs: List[str],
        output_dir: Path,
        chapter_index: int,
        total_chapters: int,
    ) -> List[Path]:
        """Synthesize all paragraphs in a chapter.

        Returns list of generated audio file paths, sorted by index.
        """
        # Guard SML tokens from bracket removal during cleaning.
        from book_tts.tts.sml import protect_sml_tokens, restore_sml_tokens

        guarded = [protect_sml_tokens(p) for p in paragraphs]
        cleaned = self._cleaner.clean_paragraphs(guarded)
        restored = [restore_sml_tokens(p) for p in cleaned]
        if not restored:
            logger.warning("Chapter %d has no text after cleaning", chapter_index)
            return []

        output_dir.mkdir(parents=True, exist_ok=True)

        self._tracker.update_chapter(
            chapter_index,
            total_chapters,
            message=f"Synthesizing chapter {chapter_index + 1}/{total_chapters}",
        )

        results: dict[int, list[Path]] = {}
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            futures = {}
            for i, para in enumerate(restored):
                if self._tracker.is_cancelled:
                    logger.info("Cancellation requested, stopping synthesis")
                    return []

                future = executor.submit(
                    self._synthesize_paragraph_with_retry,
                    para,
                    i,
                    output_dir,
                )
                futures[future] = i

            for future in as_completed(futures):
                idx = futures[future]
                try:
                    paths = future.result()
                    if paths:
                        results[idx] = paths
                except Exception as exc:
                    logger.error("Paragraph %d failed: %s", idx, exc)

        # Flatten: preserve paragraph order, flatten chunks per paragraph
        sorted_paths: list[Path] = []
        for i in sorted(results):
            sorted_paths.extend(results[i])
        return sorted_paths

    def _synthesize_paragraph_with_retry(
        self,
        text: str,
        index: int,
        output_dir: Path,
        max_retries: int = 3,
    ) -> Optional[List[Path]]:
        """Synthesize a single paragraph with exponential backoff retry.

        Returns list of file paths (one per chunk if text was split).
        """
        chunks = self._split_text_multi_pass(text)
        paths: List[Path] = []

        for chunk_idx, chunk in enumerate(chunks):
            if not chunk.strip():
                continue

            fname = f"para_{index:04d}_{chunk_idx:02d}.wav"
            fpath = output_dir / fname

            # Strip SML tokens before sending to TTS.
            clean_chunk = strip_sml_tokens(chunk)
            if not clean_chunk.strip():
                continue

            for attempt in range(max_retries):
                try:
                    self._rate_limiter.acquire()
                    audio_bytes = self._client.synthesize(
                        text=clean_chunk,
                        audio_format="wav",
                    )
                    fpath.write_bytes(audio_bytes)
                    paths.append(fpath)
                    break
                except Exception as exc:
                    if attempt == max_retries - 1:
                        logger.error(
                            "Paragraph %d chunk %d failed after %d attempts: %s",
                            index,
                            chunk_idx,
                            max_retries,
                            exc,
                        )
                    else:
                        time.sleep(2 ** attempt)

        return paths if paths else None

    @staticmethod
    def _split_text_multi_pass(text: str, max_chars: int = MAX_TTS_CHARS) -> list[str]:
        """Multi-pass sentence splitter for TTS-friendly chunk sizes.

        1. Split on SML tokens (hard boundaries, never merged across).
        2. Within each SML segment, split on hard punctuation into sentences.
        3. Greedily combine sentences until nearing max_chars.
        4. If a single sentence exceeds max_chars, split it further
           (soft punct → space → force-chop).
        """
        if not text:
            return [""]

        hard_punct_re = re.compile(r"(?<=[。！？.!?])")
        soft_punct_re = re.compile(r"(?<=[，,、；;：:])")

        # ── Step 1: Split on SML tokens (hard boundaries) ─────────────
        if has_sml_token(text):
            sml_segments = split_on_sml_tokens(text)
        else:
            sml_segments = [text]

        result: list[str] = []
        for segment in sml_segments:
            # ── Step 2: Split on hard punctuation into sentences ──────
            sentences = hard_punct_re.split(segment)
            sentences = [s.strip() for s in sentences if s.strip()]
            if not sentences:
                result.append(segment.strip())
                continue

            # ── Step 3: Greedily combine sentences toward max_chars ──
            groups: list[str] = []
            current = ""
            for s in sentences:
                if len(current) + len(s) > max_chars and current:
                    groups.append(current.strip())
                    current = s
                else:
                    current = (current + s) if current else s
            if current:
                groups.append(current.strip())

            # ── Step 4: Split oversized groups ────────────────────────
            for group in groups:
                if len(group) <= max_chars:
                    result.append(group)
                    continue

                # Try soft punctuation first.
                sparts = soft_punct_re.split(group)
                sparts = [p.strip() for p in sparts if p.strip()]
                sub_groups: list[str] = []
                cur = ""
                for p in sparts:
                    if len(cur) + len(p) > max_chars and cur:
                        sub_groups.append(cur.strip())
                        cur = p
                    else:
                        cur = (cur + p) if cur else p
                if cur:
                    sub_groups.append(cur.strip())

                # Any sub-group still too large → space split → force-chop.
                for sg in sub_groups:
                    if len(sg) <= max_chars:
                        result.append(sg)
                        continue
                    words = sg.split()
                    if words:
                        wgroup = ""
                        for w in words:
                            if len(wgroup) + len(w) + 1 > max_chars and wgroup:
                                result.append(wgroup.strip())
                                wgroup = w
                            else:
                                wgroup = f"{wgroup} {w}" if wgroup else w
                        if wgroup:
                            if len(wgroup) <= max_chars:
                                result.append(wgroup.strip())
                            else:
                                for k in range(0, len(wgroup), max_chars):
                                    result.append(wgroup[k:k + max_chars].strip())
                    else:
                        for k in range(0, len(sg), max_chars):
                            result.append(sg[k:k + max_chars].strip())

        return result if result else [text.strip()]
