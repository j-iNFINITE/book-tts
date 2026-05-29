"""Audio file merger using pydub."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from pydub import AudioSegment

from book_tts.config import AUDIO_FORMAT, DEFAULT_PARA_PAUSE_MS

logger = logging.getLogger(__name__)

_DEFAULT_BITRATE = "192k"


class AudioMerger:
    """Merge multiple audio files into chapter and book-level outputs.

    Parameters
    ----------
    pause_ms:
        Silence duration (ms) inserted between paragraphs within a chapter.
    bitrate:
        Output bitrate for lossy formats (e.g. ``"192k"``).
    """

    def __init__(
        self,
        pause_ms: int = DEFAULT_PARA_PAUSE_MS,
        bitrate: str = _DEFAULT_BITRATE,
    ) -> None:
        self.pause_ms = pause_ms
        self.bitrate = bitrate

    def merge_to_chapter(
        self,
        audio_paths: List[Path],
        output_path: Path,
        format: str = AUDIO_FORMAT,
    ) -> Optional[Path]:
        """Merge paragraph audio files into a single chapter file.

        Returns ``None`` if *audio_paths* is empty.
        """
        if not audio_paths:
            logger.warning("No audio files to merge for chapter")
            return None

        combined = AudioSegment.empty()
        pause = AudioSegment.silent(duration=self.pause_ms)

        for i, path in enumerate(audio_paths):
            segment = AudioSegment.from_file(str(path))
            combined += segment
            if i < len(audio_paths) - 1:
                combined += pause

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        combined.export(str(output_path), format=format, bitrate=self.bitrate)

        logger.info(
            "Chapter merged: %d files → %s", len(audio_paths), output_path
        )
        return output_path

    def merge_chapters(
        self,
        chapter_paths: List[Path],
        output_path: Path,
        chapter_pause_ms: int = 1000,
    ) -> Path:
        """Merge chapter audio files into a complete audiobook.

        Raises
        ------
        ValueError
            If *chapter_paths* is empty.
        """
        if not chapter_paths:
            raise ValueError("At least one chapter file is required")

        combined = AudioSegment.empty()
        chapter_pause = AudioSegment.silent(duration=chapter_pause_ms)

        for i, path in enumerate(chapter_paths):
            segment = AudioSegment.from_file(str(path))
            combined += segment
            if i < len(chapter_paths) - 1:
                combined += chapter_pause

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        combined.export(str(output_path), format=AUDIO_FORMAT, bitrate=self.bitrate)

        logger.info(
            "Audiobook merged: %d chapters → %s",
            len(chapter_paths),
            output_path,
        )
        return output_path

    def embed_cover(self, audio_path: Path, cover_image: bytes) -> None:
        """Embed cover art into an MP3 file using mutagen."""
        try:
            from mutagen.mp3 import MP3
            from mutagen.id3 import APIC
        except ImportError:
            logger.warning("mutagen not installed, skipping cover art embedding")
            return

        try:
            audio = MP3(str(audio_path))
            if audio.tags is None:
                audio.add_tags()
            audio.tags.add(
                APIC(
                    encoding=3,  # UTF-8
                    mime="image/jpeg",
                    type=3,  # Cover (front)
                    data=cover_image,
                )
            )
            audio.save()
            logger.info("Cover art embedded in %s", audio_path)
        except Exception as exc:
            logger.warning("Failed to embed cover art: %s", exc)
