"""Markdown file parser with heading-based chapter detection."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, List, Optional

from book_tts.models import BookMetadata, BoundaryType, Chapter, ParseResult, ParserType
from book_tts.parsers.base import BaseBookParser
from book_tts.parsers.text_cleaner import TextCleaner
from book_tts.config import MIN_VISIBLE_CHARS

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


class MarkdownParser(BaseBookParser):
    """Parse Markdown files into chapters based on heading structure."""

    def __init__(self, cleaner: Optional[TextCleaner] = None) -> None:
        self.cleaner = cleaner or TextCleaner()

    def get_supported_formats(self) -> List[str]:
        return [".md", ".markdown"]

    def parse(
        self,
        file_path: str | Path,
        progress_callback: Optional[Callable] = None,
    ) -> ParseResult:
        path = Path(file_path)
        if not self.validate_file(path):
            raise FileNotFoundError(f"Markdown file not found: {path}")

        content = path.read_text(encoding="utf-8", errors="replace")
        headings = list(_HEADING_RE.finditer(content))

        if not headings:
            return self._parse_as_single_chapter(path, content)

        # Split into chapters at heading boundaries
        chapters = self._split_at_headings(path.stem, content, headings)

        return ParseResult(
            metadata=BookMetadata(title=path.stem),
            chapters=tuple(chapters),
            toc=tuple(ch.title for ch in chapters),
            parser_type=ParserType.MARKDOWN,
        )

    def _parse_as_single_chapter(
        self, path: Path, content: str
    ) -> ParseResult:
        paragraphs = self._content_to_paragraphs(content)
        cleaned = self.cleaner.clean_paragraphs(paragraphs)
        boundaries = (BoundaryType.NONE,) + (BoundaryType.PARAGRAPH,) * (max(len(cleaned) - 1, 0))
        if not cleaned:
            cleaned = ["(empty)"]

        chapter = Chapter(
            index=0,
            title=path.stem,
            paragraphs=tuple(cleaned),
            source_file=path.name,
            boundaries=boundaries,
        )
        return ParseResult(
            metadata=BookMetadata(title=path.stem),
            chapters=(chapter,),
            toc=(path.stem,),
            parser_type=ParserType.MARKDOWN,
        )

    def _split_at_headings(
        self,
        book_title: str,
        content: str,
        headings: list[re.Match],
    ) -> list[Chapter]:
        from collections import Counter

        level_counts = Counter(len(m.group(1)) for m in headings)

        # Use the deepest heading level (highest number) that appears more than once
        multi_levels = [l for l, c in level_counts.items() if c > 1]
        split_level = max(multi_levels) if multi_levels else min(level_counts)

        # Also include any higher-level headings as split points
        split_points = [
            m for m in headings if len(m.group(1)) <= split_level
        ]

        chapters: list[Chapter] = []
        for idx, match in enumerate(split_points):
            title = self._clean_title(match.group(2).strip())
            start = match.start()

            # End at next split point or end of content
            if idx + 1 < len(split_points):
                end = split_points[idx + 1].start()
            else:
                end = len(content)

            section = content[start:end]
            paragraphs = self._content_to_paragraphs(section)
            cleaned = self.cleaner.clean_paragraphs(paragraphs)
            leading = BoundaryType.SECTION if idx > 0 else BoundaryType.NONE
            boundaries = (leading,) + (BoundaryType.PARAGRAPH,) * (max(len(cleaned) - 1, 0))

            total_chars = sum(len(p) for p in cleaned)
            if total_chars < MIN_VISIBLE_CHARS:
                continue

            chapters.append(
                Chapter(
                    index=len(chapters),
                    title=title,
                    paragraphs=tuple(cleaned),
                    source_file=f"{book_title}.md",
                    boundaries=boundaries,
                )
            )

        return chapters

    @staticmethod
    def _clean_title(title: str) -> str:
        title = re.sub(r"\$\s*\^?\{?\d+\}?\s*\$", "", title)
        title = re.sub(r"\^\{?\d+\}?", "", title)
        title = re.sub(r"^\|\s*", "", title)
        return title.strip()

    @staticmethod
    def _content_to_paragraphs(content: str) -> List[str]:
        """Convert markdown content to paragraphs.

        Splits on blank lines, strips markdown and HTML formatting.
        """
        content = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", content)
        content = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", content)
        content = re.sub(r"^#{1,6}\s+", "", content, flags=re.MULTILINE)
        content = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", content)

        content = re.sub(r"\$\s*\^?\{?\d+\}?\s*\$", "", content)
        content = re.sub(r"\^\{?\d+\}?", "", content)
        content = re.sub(r"^\|\s*", "", content, flags=re.MULTILINE)

        content = re.sub(r"<div[^>]*>\s*<img[^>]*/?>\s*</div>", "", content, flags=re.DOTALL)
        content = re.sub(r"<img[^>]*/?>", "", content)
        content = re.sub(r"\[Pasted[^\]]*\]", "", content)

        blocks = re.split(r"\n\s*\n", content)

        paragraphs: List[str] = []
        for block in blocks:
            text = block.strip()
            text = re.sub(r"\s*\n\s*", " ", text)
            text = text.strip()
            if text:
                paragraphs.append(text)

        return paragraphs
