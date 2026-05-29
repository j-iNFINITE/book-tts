"""Unit tests for book_tts.models."""

import pytest

from book_tts.models import (
    BookMetadata,
    BoundaryType,
    Chapter,
    ConversionStatus,
    ParseResult,
    ParserType,
    TTSConfig,
)


class TestChapter:
    """Tests for the Chapter dataclass."""

    def test_chapter_creation(self):
        """Chapter fields are stored correctly."""
        ch = Chapter(
            index=0,
            title="Introduction",
            paragraphs=("Hello world.",),
            source_file="ch01.html",
            word_count=12,
        )
        assert ch.index == 0
        assert ch.title == "Introduction"
        assert ch.paragraphs == ("Hello world.",)
        assert ch.source_file == "ch01.html"
        assert ch.word_count == 12

    def test_chapter_word_count_auto(self):
        """word_count auto-computes from paragraphs when left at default."""
        ch = Chapter(
            index=1,
            title="Test",
            paragraphs=("一二三四五", "六七八",),
        )
        assert ch.word_count == 8  # len("一二三四五") + len("六七八")

    def test_chapter_immutable(self):
        """Chapter is frozen — attribute assignment raises."""
        ch = Chapter(index=0, title="X", paragraphs=("a",))
        with pytest.raises(AttributeError):
            ch.title = "Y"


class TestBookMetadata:
    """Tests for BookMetadata dataclass."""

    def test_book_metadata_defaults(self):
        """All fields default to empty string."""
        m = BookMetadata()
        assert m.title == ""
        assert m.author == ""
        assert m.language == ""
        assert m.description == ""
        assert m.publisher == ""
        assert m.publication_date == ""

    def test_book_metadata_fields(self):
        """Fields are stored correctly."""
        m = BookMetadata(title="My Book", author="Author", language="zh")
        assert m.title == "My Book"
        assert m.author == "Author"
        assert m.language == "zh"


class TestParseResult:
    """Tests for ParseResult dataclass."""

    def test_parse_result_with_chapters(self):
        """ParseResult holds metadata and chapters."""
        meta = BookMetadata(title="Book")
        ch = Chapter(index=0, title="Ch1", paragraphs=("text",))
        pr = ParseResult(metadata=meta, chapters=(ch,))
        assert pr.metadata.title == "Book"
        assert len(pr.chapters) == 1
        assert pr.parser_type == ParserType.EPUB

    def test_parse_result_defaults(self):
        """Optional fields have correct defaults."""
        pr = ParseResult(metadata=BookMetadata(), chapters=())
        assert pr.cover_image is None
        assert pr.toc == ()
        assert pr.parser_type == ParserType.EPUB


class TestTTSConfig:
    """Tests for TTSConfig dataclass."""

    def test_tts_config_defaults(self):
        """Default TTSConfig has expected values."""
        cfg = TTSConfig()
        assert cfg.api_keys == ()
        assert cfg.voice == "冰糖"
        assert cfg.rpm_limit == 90

    def test_tts_config_custom(self):
        """TTSConfig accepts custom API keys and settings."""
        cfg = TTSConfig(
            api_keys=("key1", "key2"),
            voice="custom",
            style="gentle",
            base_url="https://example.com/v1",
            rpm_limit=60,
        )
        assert cfg.api_keys == ("key1", "key2")
        assert cfg.voice == "custom"
        assert cfg.base_url == "https://example.com/v1"


class TestEnums:
    """Tests for model enums."""

    def test_parser_type_values(self):
        """ParserType has expected members."""
        assert ParserType.EPUB.name == "EPUB"
        assert ParserType.MOBI.name == "MOBI"
        assert ParserType.MARKDOWN.name == "MARKDOWN"

    def test_conversion_status_values(self):
        """ConversionStatus has expected members."""
        assert ConversionStatus.IDLE.name == "IDLE"
        assert ConversionStatus.COMPLETED.name == "COMPLETED"
        assert ConversionStatus.ERROR.name == "ERROR"

    def test_boundary_type_values(self):
        """BoundaryType has expected members."""
        assert BoundaryType.NONE.name == "NONE"
        assert BoundaryType.PARAGRAPH.name == "PARAGRAPH"
        assert BoundaryType.SECTION.name == "SECTION"
