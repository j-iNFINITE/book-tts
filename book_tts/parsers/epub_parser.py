"""EPUB parser with multi-level navigation and improved text extraction."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ebooklib import epub, ITEM_DOCUMENT
from bs4 import BeautifulSoup, Tag, NavigableString

from book_tts.parsers.base import BaseBookParser
from book_tts.parsers.text_cleaner import TextCleaner
from book_tts.models import (
    BookMetadata,
    BoundaryType,
    Chapter,
    ConversionProgress,
    ConversionStatus,
    ParseResult,
    ParserType,
)
from book_tts.config import SKIP_GUIDE_TYPES, SKIP_NAME_KEYWORDS, MIN_VISIBLE_CHARS

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

_BLOCK_TAGS: frozenset[str] = frozenset({
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "td", "th", "blockquote", "pre",
})

# Container tags: recurse into them but don't emit as their own paragraph.
_CONTAINER_TAGS: frozenset[str] = frozenset({
    "div", "section", "article", "aside", "nav", "header", "footer",
    "main", "figure", "figcaption", "details", "summary",
})

_SKIP_TAGS: frozenset[str] = frozenset({
    "script", "style", "head", "meta", "link", "title",
    "noscript", "svg", "canvas",
})

_COVER_KEYWORDS: frozenset[str] = frozenset({
    "cover", "titlepage", "title-page", "front-cover",
    "half-title", "halftitle",
})
_COPYRIGHT_KEYWORDS: frozenset[str] = frozenset({
    "copyright", "colophon", "legal", "imprint",
})
_TOC_KEYWORDS: frozenset[str] = frozenset({
    "toc", "nav", "navigation", "contents", "table-of-contents",
})
_BOILERPLATE_CLASS_KEYWORDS: frozenset[str] = frozenset({
    "cover", "titlepage", "copyright", "colophon", "toc",
    "nav", "frontmatter", "backmatter",
})

# Matches footnote-style superscript references: "[1]", "(3)", "*"
_FOOTNOTE_REF_RE = re.compile(r"[\[\(]?\d+[\]\)]?|\*+")

# ── Helpers ────────────────────────────────────────────────────────────────────


def _normalise_href(href: str) -> str:
    return href.split("#")[0].strip()


def _fragment_from_href(href: str) -> str:
    if "#" in href:
        return href.split("#", 1)[1]
    return ""


def _is_boilerplate_filename(name: str) -> bool:
    lower = name.lower()
    for kw in _COVER_KEYWORDS | _COPYRIGHT_KEYWORDS | _TOC_KEYWORDS:
        if kw in lower:
            return True
    return False


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


# ── Parser ─────────────────────────────────────────────────────────────────────


class EPUBParser(BaseBookParser):
    """Parse EPUB files into structured chapters for TTS conversion.

    Navigation detection order:
      1. EPUB3 nav.xhtml (epub:type="toc")
      2. EPUB2 toc.ncx
      3. Heading-based fallback (h1-h6)
    """

    def __init__(self, cleaner: Optional[TextCleaner] = None) -> None:
        self.cleaner = cleaner or TextCleaner()

    # ── Public API ─────────────────────────────────────────────────────────

    def get_supported_formats(self) -> List[str]:
        return [".epub"]

    def parse(
        self,
        file_path: str | Path,
        progress_callback: Optional[Callable[[ConversionProgress], None]] = None,
    ) -> ParseResult:
        path = Path(file_path)
        if not self.validate_file(path):
            raise FileNotFoundError(f"EPUB file not found: {path}")

        book = epub.read_epub(str(path), options={"ignore_ncx": False})

        metadata = self._extract_metadata(book)
        cover = self._extract_cover(book)
        nav_items = self._detect_navigation(book)
        chapters = self._extract_chapters(book, nav_items)

        cleaned_chapters: list[Chapter] = []
        for ch in chapters:
            cleaned_paras = self.cleaner.clean_paragraphs(list(ch.paragraphs))
            if cleaned_paras:
                cleaned_chapters.append(
                    Chapter(
                        index=ch.index,
                        title=ch.title,
                        paragraphs=tuple(cleaned_paras),
                        source_file=ch.source_file,
                        boundaries=ch.boundaries,
                    )
                )

        # Re-index after cleaning — some chapters may have been filtered out.
        cleaned_chapters = [
            Chapter(
                index=i,
                title=ch.title,
                paragraphs=ch.paragraphs,
                source_file=ch.source_file,
                boundaries=ch.boundaries,
            )
            for i, ch in enumerate(cleaned_chapters)
        ]

        toc_titles = tuple(ch.title for ch in cleaned_chapters)

        if progress_callback:
            progress_callback(
                ConversionProgress(
                    status=ConversionStatus.COMPLETED,
                    total_chapters=len(cleaned_chapters),
                    message=f"Parsed {len(cleaned_chapters)} chapters",
                )
            )

        return ParseResult(
            metadata=metadata,
            chapters=tuple(cleaned_chapters),
            cover_image=cover,
            toc=toc_titles,
            parser_type=ParserType.EPUB,
        )

    # ── Navigation detection ───────────────────────────────────────────────

    def _detect_navigation(self, book: epub.EpubBook) -> List[Dict[str, str]]:
        """Try EPUB3 NAV → EPUB2 NCX → heading-based detection."""
        nav_items = self._try_nav_html(book)
        if nav_items:
            logger.debug("Navigation detected via EPUB3 NAV (%d items)", len(nav_items))
            return nav_items

        ncx_items = self._try_ncx(book)
        if ncx_items:
            logger.debug("Navigation detected via EPUB2 NCX (%d items)", len(ncx_items))
            return ncx_items

        heading_items = self._detect_headings(book)
        if heading_items:
            logger.debug("Navigation detected via headings (%d items)", len(heading_items))
            return heading_items

        logger.warning("No navigation detected; will use spine order with generic titles")
        return []

    # ── EPUB3 NAV ──────────────────────────────────────────────────────────

    def _try_nav_html(self, book: epub.EpubBook) -> List[Dict[str, str]]:
        for item in book.get_items():
            if item.get_type() != ITEM_DOCUMENT:
                continue

            name = item.get_name().lower()
            if "nav" not in name:
                continue

            html = item.get_content().decode("utf-8", errors="replace")
            soup = _soup(html)

            # epub:type="toc" is the standard EPUB3 nav marker
            nav_el = soup.find(
                "nav",
                attrs={"epub:type": re.compile(r"\btoc\b", re.I)},
            )
            if nav_el is None:
                nav_el = soup.find("nav", {"role": "navigation"})
            if nav_el is None:
                nav_el = soup.find("nav")
            if nav_el is None:
                continue

            return self._parse_nav_html(nav_el, item.get_name())

        return []

    def _parse_nav_html(
        self, nav_el: Tag, base_name: str = ""
    ) -> List[Dict[str, str]]:
        items: list[Dict[str, str]] = []
        seen_hrefs: set[str] = set()

        for a_tag in nav_el.find_all("a", href=True):
            href = a_tag["href"].strip()
            if not href:
                continue

            bare_href = _normalise_href(href)
            anchor = _fragment_from_href(href)
            title = a_tag.get_text(strip=True)

            if not title:
                continue

            key = f"{bare_href}#{anchor}"
            if key in seen_hrefs:
                continue
            seen_hrefs.add(key)

            items.append({
                "title": title,
                "href": bare_href,
                "anchor": anchor,
            })

        return items

    # ── EPUB2 NCX ──────────────────────────────────────────────────────────

    def _try_ncx(self, book: epub.EpubBook) -> List[Dict[str, str]]:
        ncx_item = None
        for item in book.get_items():
            name = item.get_name().lower()
            if name.endswith("toc.ncx") or name.endswith(".ncx"):
                ncx_item = item
                break

        if ncx_item is None:
            return []

        return self._parse_ncx(ncx_item)

    def _parse_ncx(self, ncx_item: epub.EpubItem) -> List[Dict[str, str]]:
        xml_bytes = ncx_item.get_content()
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError:
            logger.warning("Failed to parse NCX XML from %s", ncx_item.get_name())
            return []

        # NCX uses the DAISY namespace: http://www.daisy.org/z3986/2005/ncx/
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"

        items: list[Dict[str, str]] = []
        seen_hrefs: set[str] = set()

        for nav_point in root.iter(f"{ns}navPoint"):
            label_el = nav_point.find(f"{ns}navLabel")
            content_el = nav_point.find(f"{ns}content")

            if content_el is None:
                continue

            src = content_el.get("src", "").strip()
            if not src:
                continue

            title = ""
            if label_el is not None:
                text_el = label_el.find(f"{ns}text")
                if text_el is not None and text_el.text:
                    title = text_el.text.strip()

            if not title:
                title = Path(src).stem.replace("_", " ").replace("-", " ").title()

            bare_href = _normalise_href(src)
            anchor = _fragment_from_href(src)

            key = f"{bare_href}#{anchor}"
            if key in seen_hrefs:
                continue
            seen_hrefs.add(key)

            items.append({
                "title": title,
                "href": bare_href,
                "anchor": anchor,
            })

        return items

    # ── Heading-based fallback ─────────────────────────────────────────────

    def _detect_headings(self, book: epub.EpubBook) -> List[Dict[str, str]]:
        items: list[Dict[str, str]] = []
        seen_hrefs: set[str] = set()

        for doc_item in book.get_items_of_type(ITEM_DOCUMENT):
            html = doc_item.get_content().decode("utf-8", errors="replace")
            soup = _soup(html)
            item_name = doc_item.get_name()

            for level in range(1, 7):
                for heading in soup.find_all(f"h{level}"):
                    title = heading.get_text(strip=True)
                    if not title:
                        continue

                    href = item_name
                    heading_id = heading.get("id", "")
                    anchor = heading_id

                    key = f"{href}#{anchor}"
                    if key in seen_hrefs:
                        continue
                    seen_hrefs.add(key)

                    items.append({
                        "title": title,
                        "href": href,
                        "anchor": anchor,
                    })

        return items

    # ── Chapter extraction ─────────────────────────────────────────────────

    def _extract_chapters(
        self, book: epub.EpubBook, nav_items: List[Dict[str, str]]
    ) -> List[Chapter]:
        """Extract chapters using NCX/NAV as source of truth for boundaries.

        When nav_items define chapter boundaries (with #fragment anchors),
        HTML files are split at those anchor points. Files without nav entries
        are treated as continuation content appended to the current chapter.
        """
        # Build: filename -> [(anchor, title), ...] sorted by source order
        file_navs: Dict[str, List[Dict[str, str]]] = {}
        for nav in nav_items:
            bare = _normalise_href(nav["href"])
            file_navs.setdefault(bare, []).append(nav)

        # Preserve nav order per file as defined in nav_items
        for entries in file_navs.values():
            entries.sort(key=lambda n: nav_items.index(n))

        spine_ids = [item_id for item_id, _ in book.spine]
        spine_lookup = {item.get_id(): item for item in book.get_items()}

        chapters: list[Chapter] = []
        chapter_index = 0

        for item_id in spine_ids:
            item = spine_lookup.get(item_id)
            if item is None:
                continue
            if item.get_type() != ITEM_DOCUMENT:
                continue

            item_name = item.get_name()
            nav_entries = file_navs.get(item_name, [])

            # Skip boilerplate only for files without NAV entries
            if not nav_entries and self._is_boilerplate(item_name, item.get_content()):
                continue

            html = item.get_content().decode("utf-8", errors="replace")
            soup = _soup(html)
            content_root = self._find_content_root(soup)

            nav_entries = file_navs.get(item_name, [])

            if nav_entries:
                anchor_ids = [n["anchor"] for n in nav_entries if n["anchor"]]

                if anchor_ids:
                    # Collect all direct children
                    all_kids = [c for c in content_root.children if isinstance(c, Tag)]

                    # For each nav entry, find its anchor position (-1 if no anchor)
                    entry_positions = []
                    for nav in nav_entries:
                        aid = nav["anchor"]
                        if not aid:
                            entry_positions.append(-1)
                            continue
                        pos = -1
                        for i, child in enumerate(all_kids):
                            if child.get("id") == aid or child.find(id=aid):
                                pos = i
                                break
                        entry_positions.append(pos)

                    # Fill in positions for anchorless entries:
                    # They start after the previous entry's anchor
                    for idx in range(len(entry_positions)):
                        if entry_positions[idx] == -1:
                            if idx > 0 and entry_positions[idx - 1] >= 0:
                                entry_positions[idx] = entry_positions[idx - 1] + 1
                            else:
                                entry_positions[idx] = 0

                    # Collect content for each entry
                    for idx, nav_entry in enumerate(nav_entries):
                        start = entry_positions[idx]
                        end = (
                            entry_positions[idx + 1]
                            if idx + 1 < len(entry_positions)
                            else len(all_kids)
                        )

                        seg_kids = all_kids[start:end]

                        # Build wrapper and extract paragraphs
                        from bs4 import BeautifulSoup as BS4
                        wrapper = BS4("", "html.parser").new_tag("div")
                        for kid in seg_kids:
                            if hasattr(kid, "extract"):
                                wrapper.append(kid.extract())
                            else:
                                wrapper.append(kid)

                        paragraphs = self._extract_paragraphs_from_root(wrapper)
                        paragraphs = self._merge_broken_paragraphs(paragraphs)
                        boundaries = (BoundaryType.NONE,) + (BoundaryType.PARAGRAPH,) * (max(len(paragraphs) - 1, 0))

                        chapters.append(Chapter(
                            index=chapter_index,
                            title=nav_entry["title"],
                            paragraphs=tuple(paragraphs),
                            source_file=item_name,
                            boundaries=boundaries,
                        ))
                        chapter_index += 1
                else:
                    # Nav entries without anchors -> use whole file
                    paragraphs = self._extract_paragraphs_from_root(content_root)
                    paragraphs = self._merge_broken_paragraphs(paragraphs)
                    boundaries = (BoundaryType.NONE,) + (BoundaryType.PARAGRAPH,) * (max(len(paragraphs) - 1, 0))

                    chapters.append(
                        Chapter(
                            index=chapter_index,
                            title=nav_entries[0]["title"],
                            paragraphs=tuple(paragraphs),
                            source_file=item_name,
                            boundaries=boundaries,
                        )
                    )
                    chapter_index += 1
            else:
                # No nav entry -> continuation content, append to last chapter
                paragraphs = self._extract_paragraphs_from_root(content_root)
                paragraphs = self._merge_broken_paragraphs(paragraphs)
                total_chars = sum(len(p) for p in paragraphs)
                if total_chars < MIN_VISIBLE_CHARS:
                    continue

                if chapters:
                    # Merge into previous chapter with a section boundary.
                    last = chapters[-1]
                    new_boundaries = (BoundaryType.SECTION,) + (BoundaryType.PARAGRAPH,) * (max(len(paragraphs) - 1, 0))
                    merged_paras = list(last.paragraphs) + paragraphs
                    merged_boundaries = last.boundaries + new_boundaries if last.boundaries else new_boundaries
                    chapters[-1] = Chapter(
                        index=last.index,
                        title=last.title,
                        paragraphs=tuple(merged_paras),
                        source_file=last.source_file,
                        boundaries=merged_boundaries,
                    )
                else:
                    # No previous chapter -> create one with filename title
                    stem = Path(item_name).stem
                    title = (
                        stem.replace("_", " ").replace("-", " ").title()
                    )
                    boundaries = (BoundaryType.NONE,) + (BoundaryType.PARAGRAPH,) * (max(len(paragraphs) - 1, 0))
                    chapters.append(
                        Chapter(
                            index=chapter_index,
                            title=title,
                            paragraphs=tuple(paragraphs),
                            source_file=item_name,
                            boundaries=boundaries,
                        )
                    )
                    chapter_index += 1

        # Re-index chapters sequentially
        chapters = [
            Chapter(
                index=i,
                title=ch.title,
                paragraphs=ch.paragraphs,
                source_file=ch.source_file,
                boundaries=ch.boundaries,
            )
            for i, ch in enumerate(chapters)
        ]

        return chapters

    def _extract_paragraphs_from_root(self, root: Tag) -> List[str]:
        """Extract text paragraphs from a content root element."""
        paragraphs: list[str] = []
        self._walk_blocks(root, paragraphs, depth=0)
        return paragraphs

    def _merge_broken_paragraphs(self, paragraphs: List[str]) -> List[str]:
        """Merge paragraph fragments split by Calibre conversion.

        Calibre often breaks a sentence across multiple <p> tags:
          <p>...其想象力的变</p>
          <p>迁。</p>

        Merges short fragments that don't end with sentence punctuation.
        """
        if not paragraphs:
            return paragraphs

        SENTENCE_END = set("。！？.!?…—")
        INTERNAL_PUNCT = set("，,、；;：:")  # Punctuation that appears within sentences

        merged: List[str] = []
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            if merged:
                prev = merged[-1]

                prev_ends_sentence = prev and prev[-1] in SENTENCE_END
                has_internal_punct = any(c in INTERNAL_PUNCT for c in prev)

                # Merge short fragments (< 20 chars) without ending or internal punctuation
                # Catches: "变" + "迁。", "魅力所" + "在。"
                # Skips: "在记事本的中间， 划出一道分割线" (has comma)
                if not prev_ends_sentence and len(prev) < 20 and not has_internal_punct:
                    merged[-1] = prev + para
                    continue

                # Merge single-char or punctuation-only fragments
                if len(para) <= 2:
                    merged[-1] = prev + para
                    continue

                # Merge very short Chinese fragments that start with
                # single Chinese char + punctuation (like "在。" completing "魅力所")
                if (
                    not prev_ends_sentence
                    and len(para) >= 2
                    and '\u4e00' <= para[0] <= '\u9fff'
                    and para[1] in SENTENCE_END
                ):
                    merged[-1] = prev + para
                    continue

            merged.append(para)

        return merged

    # ── Text extraction ────────────────────────────────────────────────────

    def _extract_text_from_html(self, html_content: str) -> List[str]:
        """Extract text paragraphs, avoiding nested container duplication."""
        soup = _soup(html_content)
        content_root = self._find_content_root(soup)

        paragraphs: list[str] = []
        self._walk_blocks(content_root, paragraphs, depth=0)
        return paragraphs

    def _find_content_root(self, soup: BeautifulSoup) -> Tag:
        """Descend past wrapper divs to reach the actual content level."""
        body = soup.find("body")
        if body is None:
            return soup

        current = body
        while True:
            tag_children = [
                c for c in current.children if isinstance(c, Tag)
            ]
            container_children = [
                c for c in tag_children if c.name in _CONTAINER_TAGS
            ]
            text_children = [
                c for c in current.children
                if isinstance(c, NavigableString) and c.strip()
            ]

            # Only descend when there is exactly one container child and NOTHING else.
            if (
                len(container_children) == 1
                and len(tag_children) == len(container_children)
                and not text_children
            ):
                current = container_children[0]
            else:
                break

        return current

    def _walk_blocks(self, element: Tag, paragraphs: list[str], depth: int) -> None:
        for child in element.children:
            if isinstance(child, NavigableString):
                text = child.strip()
                if text and depth == 0:
                    paragraphs.append(text)
                continue

            if not isinstance(child, Tag):
                continue

            tag_name = child.name.lower()

            if tag_name in _SKIP_TAGS:
                continue

            if tag_name == "img":
                alt = child.get("alt", "").strip()
                if alt:
                    paragraphs.append(alt)
                continue

            # Footnote refs (sup) and markers (sub) get appended to the
            # preceding paragraph so they stay contextually attached.
            if tag_name in ("sup", "sub"):
                text = child.get_text(strip=True)
                if text and paragraphs:
                    if tag_name == "sup" and not _FOOTNOTE_REF_RE.match(text):
                        continue
                    paragraphs[-1] = paragraphs[-1] + text
                    continue

            if tag_name in _BLOCK_TAGS:
                text = self._extract_element_text(child)
                if text:
                    paragraphs.append(text)
                continue

            if tag_name in _CONTAINER_TAGS:
                self._walk_blocks(child, paragraphs, depth + 1)
                continue

            self._walk_blocks(child, paragraphs, depth + 1)

    def _extract_element_text(self, element: Tag) -> str:
        parts: list[str] = []

        for child in element.children:
            if isinstance(child, NavigableString):
                text = child.strip()
                if text:
                    parts.append(text)
            elif isinstance(child, Tag):
                child_tag = child.name.lower()

                if child_tag in _SKIP_TAGS:
                    continue

                if child_tag in _CONTAINER_TAGS:
                    inner = self._extract_element_text(child)
                    if inner:
                        parts.append(inner)
                    continue

                text = child.get_text(strip=True)
                if text:
                    parts.append(text)

        return " ".join(parts)

    # ── Boilerplate detection ──────────────────────────────────────────────

    def _is_boilerplate(self, item_name: str, content: bytes) -> bool:
        name_lower = item_name.lower()

        if _is_boilerplate_filename(name_lower):
            return True

        try:
            html = content.decode("utf-8", errors="replace")
        except Exception:
            return False

        soup = _soup(html)

        for el in soup.find_all(attrs={"epub:type": True}):
            epub_type = el["epub:type"].lower()
            for skip_type in SKIP_GUIDE_TYPES:
                if skip_type in epub_type:
                    return True

        body = soup.find("body")
        if body:
            classes = " ".join(body.get("class", []))
            for kw in _BOILERPLATE_CLASS_KEYWORDS:
                if kw in classes.lower():
                    return True

            for div in body.find_all("div", limit=5):
                div_classes = " ".join(div.get("class", []))
                for kw in _BOILERPLATE_CLASS_KEYWORDS:
                    if kw in div_classes.lower():
                        return True

        full_text = soup.get_text()
        text_preview = full_text[:500]
        for kw in SKIP_NAME_KEYWORDS:
            if kw in text_preview and len(full_text.strip()) < 500:
                return True

        imgs = soup.find_all("img")
        if imgs and len(full_text.strip()) < MIN_VISIBLE_CHARS:
            return True

        return False

    # ── Metadata ───────────────────────────────────────────────────────────

    def _extract_metadata(self, book: epub.EpubBook) -> BookMetadata:
        meta = book.get_metadata("DC", "title")
        title = meta[0][0] if meta else ""

        meta = book.get_metadata("DC", "creator")
        author = meta[0][0] if meta else ""

        meta = book.get_metadata("DC", "language")
        language = meta[0][0] if meta else ""

        meta = book.get_metadata("DC", "description")
        description = meta[0][0] if meta else ""

        meta = book.get_metadata("DC", "publisher")
        publisher = meta[0][0] if meta else ""

        meta = book.get_metadata("DC", "date")
        pub_date = meta[0][0] if meta else ""

        return BookMetadata(
            title=title,
            author=author,
            language=language,
            description=description,
            publisher=publisher,
            publication_date=pub_date,
        )

    # ── Cover extraction ───────────────────────────────────────────────────

    def _extract_cover(self, book: epub.EpubBook) -> Optional[bytes]:
        cover_item = None
        
        for item in book.get_items():
            name = item.get_name().lower()
            if any(kw in name for kw in ("cover", "front-cover")):
                if hasattr(item, "media_type") and item.media_type:
                    if item.media_type.startswith("image/"):
                        return item.get_content()

        for item in book.get_items():
            if hasattr(item, "get_id"):
                item_id = item.get_id().lower()
                if "cover" in item_id:
                    if hasattr(item, "media_type") and item.media_type:
                        if item.media_type.startswith("image/"):
                            return item.get_content()

        return None


class EPUBHTMLParser(BaseBookParser):
    """Pure HTML parser for EPUB files.

    Extracts all HTML files and uses <title> or <h1> as chapter names.
    Does not use EPUB navigation structure.
    """

    def __init__(self, cleaner: Optional[TextCleaner] = None) -> None:
        self.cleaner = cleaner or TextCleaner()

    def get_supported_formats(self) -> List[str]:
        return [".epub"]

    def parse(
        self,
        file_path: str | Path,
        progress_callback: Optional[Callable[[ConversionProgress], None]] = None,
    ) -> ParseResult:
        path = Path(file_path)
        if not self.validate_file(path):
            raise FileNotFoundError(f"EPUB file not found: {path}")

        book = epub.read_epub(str(path), options={"ignore_ncx": False})
        metadata = self._extract_metadata(book)
        cover = self._extract_cover(book)

        chapters: list[Chapter] = []
        chapter_index = 0

        for item in book.get_items_of_type(ITEM_DOCUMENT):
            html = item.get_content().decode("utf-8", errors="replace")
            soup = _soup(html)

            title = self._extract_title(soup, item.get_name())
            paragraphs = self._extract_paragraphs(soup)

            cleaned = self.cleaner.clean_paragraphs(paragraphs)
            if not cleaned:
                continue
            if sum(len(p) for p in cleaned) < MIN_VISIBLE_CHARS:
                continue

            boundaries = (BoundaryType.NONE,) + (BoundaryType.PARAGRAPH,) * (max(len(cleaned) - 1, 0))
            chapters.append(Chapter(
                index=chapter_index,
                title=title,
                paragraphs=tuple(cleaned),
                source_file=item.get_name(),
                boundaries=boundaries,
            ))
            chapter_index += 1

        if progress_callback:
            progress_callback(ConversionProgress(
                status=ConversionStatus.COMPLETED,
                total_chapters=len(chapters),
                message=f"Parsed {len(chapters)} chapters",
            ))

        return ParseResult(
            metadata=metadata,
            chapters=tuple(chapters),
            cover_image=cover,
            toc=tuple(ch.title for ch in chapters),
            parser_type=ParserType.EPUB,
        )

    def _extract_title(self, soup: BeautifulSoup, filename: str) -> str:
        title_tag = soup.find("title")
        if title_tag:
            title = title_tag.get_text(strip=True)
            if title:
                return title

        for tag in ["h1", "h2", "h3"]:
            heading = soup.find(tag)
            if heading:
                text = heading.get_text(strip=True)
                if text:
                    return text

        stem = Path(filename).stem
        return stem.replace("_", " ").replace("-", " ").title()

    def _extract_paragraphs(self, soup: BeautifulSoup) -> list[str]:
        body = soup.find("body")
        if not body:
            return []

        paragraphs: list[str] = []
        for tag in body.find_all(["p", "div", "h1", "h2", "h3", "h4", "h5", "h6"]):
            text = tag.get_text(strip=True)
            if text:
                paragraphs.append(text)
        return paragraphs

    def _extract_metadata(self, book: epub.EpubBook) -> BookMetadata:
        title = ""
        meta = book.get_metadata("DC", "title")
        if meta:
            title = meta[0][0]

        author = ""
        meta = book.get_metadata("DC", "creator")
        if meta:
            author = meta[0][0]

        language = ""
        meta = book.get_metadata("DC", "language")
        if meta:
            language = meta[0][0]

        return BookMetadata(title=title, author=author, language=language)

    def _extract_cover(self, book: epub.EpubBook) -> Optional[bytes]:
        for item in book.get_items():
            name = item.get_name().lower()
            if any(kw in name for kw in ("cover", "front-cover")):
                if hasattr(item, "media_type") and item.media_type:
                    if item.media_type.startswith("image/"):
                        return item.get_content()
        return None
