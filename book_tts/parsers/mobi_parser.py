"""MOBI/AZW ebook parser using the mobi library for unpacking."""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

from bs4 import BeautifulSoup

from book_tts.models import BookMetadata, BoundaryType, Chapter, ParseResult, ParserType
from book_tts.parsers.base import BaseBookParser
from book_tts.parsers.epub_parser import EPUBParser
from book_tts.parsers.text_cleaner import TextCleaner

if TYPE_CHECKING:
    from book_tts.models import ConversionProgress

logger = logging.getLogger(__name__)


class MOBIParser(BaseBookParser):
    """Parse MOBI / AZW / AZW3 files using a two-strategy fallback.

    Strategy 1 (primary): convert to EPUB via calibre's ``ebook-convert``,
    then delegate to :class:`EPUBParser`.

    Strategy 2 (fallback): unpack with the ``mobi`` Python library and
    parse the extracted HTML + NCX directly.
    """

    def __init__(self, cleaner: Optional[TextCleaner] = None) -> None:
        self.cleaner = cleaner or TextCleaner()
        self._epub_parser = EPUBParser(cleaner)

    # ── Public interface ──────────────────────────────────────────────────

    def get_supported_formats(self) -> List[str]:
        return [".mobi", ".azw", ".azw3"]

    def parse(
        self,
        file_path: str | Path,
        progress_callback: Optional[Callable[[ConversionProgress], None]] = None,
    ) -> ParseResult:
        path = Path(file_path)
        if not self.validate_file(path):
            raise FileNotFoundError(f"File not found or unsupported format: {path}")

        return self._parse_via_mobi_lib(path)

    # ── MOBI unpacking ────────────────────────────────────────────────────

    def _parse_via_mobi_lib(self, file_path: Path) -> ParseResult:
        """Unpack MOBI with the ``mobi`` library and parse HTML + NCX."""
        import mobi  # type: ignore[import-untyped]

        tmp_dir = Path(tempfile.mkdtemp(prefix="book_tts_mobi_"))
        try:
            extracted_dir, content_path = mobi.extract(str(file_path))
            extracted = Path(extracted_dir)
            content = Path(content_path)

            # mobi.extract may return EPUB (mobi8) or HTML (mobi7)
            if content.suffix.lower() == ".epub":
                parse_result = self._epub_parser.parse(content)
                return ParseResult(
                    metadata=parse_result.metadata,
                    chapters=parse_result.chapters,
                    cover_image=parse_result.cover_image,
                    toc=parse_result.toc,
                    parser_type=ParserType.MOBI,
                )

            # HTML fallback (mobi7)
            mobi7 = extracted / "mobi7"
            if not mobi7.is_dir():
                mobi7 = extracted

            html_path = mobi7 / "book.html"
            ncx_path = mobi7 / "toc.ncx"

            if not html_path.is_file():
                for candidate in mobi7.glob("*.html"):
                    html_path = candidate
                    break
                else:
                    raise RuntimeError(
                        f"No book.html found in MOBI archive: {mobi7}"
                    )

            chapters_meta: List[Dict] = []
            if ncx_path.is_file():
                chapters_meta = self._parse_ncx_chapters(ncx_path)

            return self._extract_chapters_from_html(html_path, chapters_meta)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── NCX parsing ──────────────────────────────────────────────────────

    def _parse_ncx_chapters(self, ncx_path: Path) -> List[Dict]:
        """Parse toc.ncx → list of ``{title, anchor}`` descriptors."""
        try:
            xml_content = ncx_path.read_text(encoding="utf-8", errors="replace")
            soup = BeautifulSoup(xml_content, "lxml-xml")
        except Exception:
            xml_content = ncx_path.read_text(encoding="utf-8", errors="replace")
            soup = BeautifulSoup(xml_content, "html.parser")

        chapters: List[Dict] = []
        for nav_point in soup.find_all("navPoint"):
            label_el = nav_point.find("navLabel")
            text_el = label_el.find("text") if label_el else None
            content_el = nav_point.find("content")

            title = text_el.get_text(strip=True) if text_el else ""
            src = content_el.get("src", "") if content_el else ""

            # "chapter1.html#section2" → anchor = "section2"
            anchor = ""
            if "#" in src:
                anchor = src.split("#", 1)[1]

            if title or anchor:
                chapters.append({"title": title, "anchor": anchor})

        return chapters

    # ── HTML chapter extraction ──────────────────────────────────────────

    def _extract_chapters_from_html(
        self,
        html_path: Path,
        chapters_meta: List[Dict],
    ) -> ParseResult:
        """Split *html_path* into chapters based on anchor positions."""
        try:
            raw_html = html_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            raise RuntimeError(f"Cannot read MOBI HTML: {exc}") from exc

        soup = BeautifulSoup(raw_html, "html.parser")
        metadata = self._extract_metadata_from_html(soup)

        if not chapters_meta:
            paragraphs = self._html_to_paragraphs(soup)
            cleaned = self.cleaner.clean_paragraphs(paragraphs)
            boundaries = (BoundaryType.NONE,) + (BoundaryType.PARAGRAPH,) * (max(len(cleaned) - 1, 0))
            chapter = Chapter(
                index=0,
                title=metadata.title or "Full Text",
                paragraphs=tuple(cleaned),
                source_file=html_path.name,
                boundaries=boundaries,
            )
            return ParseResult(
                metadata=metadata,
                chapters=(chapter,),
                parser_type=ParserType.MOBI,
            )

        anchor_positions: List[tuple[str, int]] = []
        for idx, el in enumerate(soup.find_all(True)):
            el_id = el.get("id", "")
            if el_id:
                anchor_positions.append((el_id, idx))

        anchor_to_index = {aid: pos for aid, pos in anchor_positions}
        all_elements = list(soup.find_all(True))

        split_points: List[tuple[int, str]] = []
        for ch in chapters_meta:
            anchor = ch.get("anchor", "")
            title = ch.get("title", "")
            if anchor and anchor in anchor_to_index:
                split_points.append((anchor_to_index[anchor], title))

        split_points.sort(key=lambda sp: sp[0])

        chapters: List[Chapter] = []
        for i, (start_idx, title) in enumerate(split_points):
            end_idx = split_points[i + 1][0] if i + 1 < len(split_points) else len(all_elements)

            segment_elements = all_elements[start_idx:end_idx]
            paragraphs = self._elements_to_paragraphs(segment_elements)
            cleaned = self.cleaner.clean_paragraphs(paragraphs)
            leading = BoundaryType.SECTION if i > 0 else BoundaryType.NONE
            boundaries = (leading,) + (BoundaryType.PARAGRAPH,) * (max(len(cleaned) - 1, 0))

            if cleaned:
                chapters.append(
                    Chapter(
                        index=i,
                        title=title,
                        paragraphs=tuple(cleaned),
                        source_file=html_path.name,
                        boundaries=boundaries,
                    )
                )

        if not chapters:
            paragraphs = self._html_to_paragraphs(soup)
            cleaned = self.cleaner.clean_paragraphs(paragraphs)
            boundaries = (BoundaryType.NONE,) + (BoundaryType.PARAGRAPH,) * (max(len(cleaned) - 1, 0))
            chapters = [
                Chapter(
                    index=0,
                    title=metadata.title or "Full Text",
                    paragraphs=tuple(cleaned),
                    source_file=html_path.name,
                    boundaries=boundaries,
                )
            ]

        return ParseResult(
            metadata=metadata,
            chapters=tuple(chapters),
            parser_type=ParserType.MOBI,
        )

    # ── Metadata extraction ──────────────────────────────────────────────

    def _extract_metadata_from_html(self, soup: BeautifulSoup) -> BookMetadata:
        title = ""
        author = ""
        language = ""
        description = ""

        title_tag = soup.find("title")
        if title_tag:
            title = title_tag.get_text(strip=True)

        for meta in soup.find_all("meta"):
            name = (meta.get("name") or meta.get("property") or "").lower()
            content = meta.get("content", "")
            if name == "author" or name == "dc:creator":
                author = content
            elif name == "language" or name == "dc:language":
                language = content
            elif name == "description" or name == "dc:description":
                description = content

        return BookMetadata(
            title=title,
            author=author,
            language=language,
            description=description,
        )

    def _extract_metadata_from_opf(self, opf_path: Path) -> BookMetadata:
        """Extract metadata from an OPF manifest (Dublin Core tags)."""
        try:
            opf_content = opf_path.read_text(encoding="utf-8", errors="replace")
            soup = BeautifulSoup(opf_content, "lxml-xml")
        except Exception:
            opf_content = opf_path.read_text(encoding="utf-8", errors="replace")
            soup = BeautifulSoup(opf_content, "html.parser")

        title = ""
        author = ""
        language = ""
        description = ""
        publisher = ""
        pub_date = ""

        # Dublin Core namespace: dc:title, dc:creator, etc.
        for tag_name, attr in [
            ("dc:title", "title"),
            ("dc:creator", "author"),
            ("dc:language", "language"),
            ("dc:description", "description"),
            ("dc:publisher", "publisher"),
            ("dc:date", "publication_date"),
        ]:
            el = soup.find(tag_name)
            if el:
                value = el.get_text(strip=True)
                if attr == "title":
                    title = value
                elif attr == "author":
                    author = value
                elif attr == "language":
                    language = value
                elif attr == "description":
                    description = value
                elif attr == "publisher":
                    publisher = value
                elif attr == "publication_date":
                    pub_date = value

        return BookMetadata(
            title=title,
            author=author,
            language=language,
            description=description,
            publisher=publisher,
            publication_date=pub_date,
        )

    # ── HTML → paragraphs ────────────────────────────────────────────────

    @staticmethod
    def _html_to_paragraphs(soup: BeautifulSoup) -> List[str]:
        paragraphs: List[str] = []
        for tag in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "h5", "h6"]):
            text = tag.get_text(strip=True)
            if text:
                paragraphs.append(text)
        return paragraphs

    @staticmethod
    def _elements_to_paragraphs(elements: list) -> List[str]:
        paragraphs: List[str] = []
        for el in elements:
            if el.name in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6"):
                text = el.get_text(strip=True)
                if text:
                    paragraphs.append(text)
        return paragraphs
