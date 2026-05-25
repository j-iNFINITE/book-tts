"""Core data models for book_tts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


# ── Enums ─────────────────────────────────────────────────────────────────────

class ParserType(Enum):
    """Supported input formats."""

    EPUB = auto()
    MOBI = auto()
    MARKDOWN = auto()


class BoundaryType(Enum):
    """Structural boundary between paragraphs for SML token injection.

    ``NONE`` — first paragraph in output, or no boundary.
    ``PARAGRAPH`` — consecutive paragraph boundary (→ ``[break]``).
    ``SECTION`` — major section / chapter boundary (→ ``[pause]``).
    """

    NONE = auto()
    PARAGRAPH = auto()
    SECTION = auto()


class ConversionStatus(Enum):
    """Lifecycle states of a conversion job."""

    IDLE = auto()
    PARSING = auto()
    CONVERTING = auto()
    COMPLETED = auto()
    CANCELLED = auto()
    ERROR = auto()


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Chapter:
    """A single chapter extracted from a book.

    ``paragraphs`` is a *tuple* to guarantee immutability.
    ``boundaries`` (optional) maps each paragraph to a :class:`BoundaryType`
    for downstream SML token injection.  Must match ``paragraphs`` length.
    """

    index: int
    title: str
    paragraphs: tuple[str, ...]
    source_file: str = ""
    word_count: int = 0
    boundaries: tuple[BoundaryType, ...] = ()

    def __post_init__(self) -> None:
        # Auto-compute word_count when left at default.
        if self.word_count == 0:
            object.__setattr__(
                self, "word_count", sum(len(p) for p in self.paragraphs)
            )


@dataclass(frozen=True, slots=True)
class BookMetadata:
    """High-level metadata about a book."""

    title: str = ""
    author: str = ""
    language: str = ""
    description: str = ""
    publisher: str = ""
    publication_date: str = ""


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Output of a parser run."""

    metadata: BookMetadata
    chapters: tuple[Chapter, ...]
    cover_image: bytes | None = None
    toc: tuple[str, ...] = ()
    parser_type: ParserType = ParserType.EPUB


@dataclass(slots=True)
class ConversionProgress:
    """Mutable progress snapshot shared across workers."""

    status: ConversionStatus = ConversionStatus.IDLE
    current_chapter: int = 0
    total_chapters: int = 0
    current_paragraph: int = 0
    total_paragraphs: int = 0
    message: str = ""
    chapter_files: list[Path] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    estimated_remaining: float = 0.0


@dataclass(frozen=True, slots=True)
class TTSConfig:
    """Configuration for the TTS engine."""

    api_keys: tuple[str, ...] = ()
    voice: str = "冰糖"
    style: str = ""
    base_url: str = ""
    rpm_limit: int = 90
