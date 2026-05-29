"""M4B audiobook builder with chapter markers.

Uses FFmpeg to concatenate chapter audio files into a single M4B container
with embedded chapter metadata and optional cover art.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

from pydub import AudioSegment

logger = logging.getLogger(__name__)


class M4BBuilder:
    """Build M4B audiobook files with chapter markers from per-chapter audio.

    Parameters
    ----------
    bitrate:
        Audio bitrate for the AAC encoding (default ``"128k"``).
    """

    def __init__(self, bitrate: str = "128k") -> None:
        self.bitrate = bitrate

    # ── Public API ─────────────────────────────────────────────────────────

    def build(
        self,
        chapter_paths: List[Path],
        chapter_titles: List[str],
        output_path: Path,
        book_title: str = "",
        book_author: str = "",
        cover_image: Optional[bytes] = None,
    ) -> Path:
        """Build an M4B file from per-chapter audio files.

        Parameters
        ----------
        chapter_paths:
            Ordered list of chapter audio file paths.
        chapter_titles:
            Parallel list of chapter titles (same length as *chapter_paths*).
        output_path:
            Destination ``.m4b`` file path.
        book_title:
            Book title for embedded metadata.
        book_author:
            Book author for embedded metadata.
        cover_image:
            Optional cover art as raw bytes (JPEG/PNG).

        Returns
        -------
        Path
            The written ``.m4b`` file path.

        Raises
        ------
        ValueError
            If *chapter_paths* and *chapter_titles* have different lengths
            or *chapter_paths* is empty.
        FileNotFoundError
            If ffmpeg is not available.
        subprocess.CalledProcessError
            If ffmpeg exits with a non-zero code.
        """
        if not chapter_paths:
            raise ValueError("At least one chapter file is required")
        if len(chapter_paths) != len(chapter_titles):
            raise ValueError(
                f"Mismatch: {len(chapter_paths)} paths vs "
                f"{len(chapter_titles)} titles"
            )

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        durations_ms = self._measure_durations(chapter_paths)
        metadata_path = self._write_metadata(
            chapter_titles=chapter_titles,
            durations_ms=durations_ms,
            book_title=book_title,
            book_author=book_author,
        )

        try:
            self._concat_chapters(
                chapter_paths=chapter_paths,
                metadata_path=metadata_path,
                output_path=output_path,
            )

            if cover_image is not None:
                self._embed_cover(output_path, cover_image)
        finally:
            metadata_path.unlink(missing_ok=True)

        logger.info("M4B built: %d chapters → %s", len(chapter_paths), output_path)
        return output_path

    # ── Internal helpers ───────────────────────────────────────────────────

    def _measure_durations(self, chapter_paths: List[Path]) -> List[int]:
        """Return duration in milliseconds for each chapter using pydub."""
        durations: list[int] = []
        for path in chapter_paths:
            seg = AudioSegment.from_file(str(path))
            durations.append(len(seg))
        return durations

    def _write_metadata(
        self,
        chapter_titles: List[str],
        durations_ms: List[int],
        book_title: str,
        book_author: str,
    ) -> Path:
        """Write an FFmpeg metadata file with chapter markers.

        The file is written to a temporary location and the caller is
        responsible for cleaning it up.
        """
        lines = [";FFMETADATA1"]

        if book_title:
            lines.append(f"title={book_title}")
        if book_author:
            lines.append(f"artist={book_author}")

        current_ms = 0
        for title, dur in zip(chapter_titles, durations_ms):
            lines.append("")
            lines.append("[CHAPTER]")
            lines.append("TIMEBASE=1/1000")
            lines.append(f"START={current_ms}")
            lines.append(f"END={current_ms + dur}")
            lines.append(f"title={title}")
            current_ms += dur

        content = "\n".join(lines) + "\n"
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        )
        tmp.write(content)
        tmp.close()
        logger.debug("Metadata written to %s", tmp.name)
        return Path(tmp.name)

    def _concat_chapters(
        self,
        chapter_paths: List[Path],
        metadata_path: Path,
        output_path: Path,
    ) -> None:
        """Concatenate chapter audio files and mux into M4B with metadata."""
        # Build a concat list file
        list_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        )
        for p in chapter_paths:
            # FFmpeg concat demuxer needs escaped single quotes
            escaped = str(p.resolve()).replace("'", "'\\''")
            list_file.write(f"file '{escaped}'\n")
        list_file.close()

        try:
            cmd = [
                "ffmpeg",
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", list_file.name,
                "-i", str(metadata_path),
                "-map_metadata", "1",
                "-c:a", "aac",
                "-b:a", self.bitrate,
                "-movflags", "+faststart",
                str(output_path),
            ]
            logger.debug("Running: %s", " ".join(cmd))
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        finally:
            Path(list_file.name).unlink(missing_ok=True)

    def _embed_cover(self, m4b_path: Path, cover_data: bytes) -> None:
        """Embed cover art into an existing M4B file."""
        cover_tmp = tempfile.NamedTemporaryFile(
            suffix=".jpg", delete=False
        )
        cover_tmp.write(cover_data)
        cover_tmp.close()

        # Re-mux with cover art as a video stream
        tmp_out = m4b_path.with_suffix(".tmp.m4b")
        try:
            cmd = [
                "ffmpeg",
                "-y",
                "-i", str(m4b_path),
                "-i", cover_tmp.name,
                "-map", "0:a",
                "-map", "1:v",
                "-c", "copy",
                "-disposition:v", "attached_pic",
                "-movflags", "+faststart",
                str(tmp_out),
            ]
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            tmp_out.replace(m4b_path)
        finally:
            Path(cover_tmp.name).unlink(missing_ok=True)
            tmp_out.unlink(missing_ok=True)
