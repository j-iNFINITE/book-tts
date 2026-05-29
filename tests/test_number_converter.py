"""Unit tests for book_tts.parsers.number_converter."""

from book_tts.parsers.number_converter import (
    _int_to_chinese,
    _roman_to_int,
    convert_all,
)


class TestRomanConversion:
    """Tests for Roman numeral conversion."""

    def test_roman_to_int_basic(self):
        """Core Roman numeral values convert correctly."""
        assert _roman_to_int("IV") == 4
        assert _roman_to_int("IX") == 9
        assert _roman_to_int("XLII") == 42
        assert _roman_to_int("MCMXCIX") == 1999

    def test_roman_to_chinese_in_chapter(self):
        """Roman numerals after 'Chapter' are converted to Chinese."""
        result = convert_all("Chapter IV")
        assert result == "Chapter 四"

    def test_roman_to_chinese_with_prefix(self):
        """Chinese prefix '第' with Roman numeral converts correctly."""
        result = convert_all("第IV章")
        assert result == "第四章"


class TestYearConversion:
    """Tests for year-to-Chinese conversion."""

    def test_year_conversion(self):
        """4-digit year followed by 年 converts to spoken Chinese."""
        result = convert_all("2024年")
        assert result == "二零二四年"

    def test_year_2008(self):
        """Year 2008 uses special 二零零X pattern."""
        result = convert_all("2008年")
        assert result == "二零零八年"


class TestSymbolConversion:
    """Tests for percent, math symbols, etc."""

    def test_percent(self):
        """Percentage converts to 百分之 form."""
        result = convert_all("50%")
        assert result == "百分之五十"

    def test_math_symbols(self):
        """Plus and equals between digits convert to Chinese words."""
        result = convert_all("3+2=5")
        assert "加" in result
        assert "等于" in result

    def test_percent_in_context(self):
        """Percentage within a sentence converts correctly."""
        result = convert_all("增长了30%")
        assert "百分之三十" in result


class TestConvertAll:
    """Tests for the full convert_all pipeline."""

    def test_convert_all_mixed(self):
        """Full pipeline handles Roman numerals, years, and percent."""
        result = convert_all("第IV章讲述了2024年的故事，占50%")
        assert "第四章" in result
        assert "二零二四年" in result
        assert "百分之五十" in result

    def test_no_conversion_needed(self):
        """Pure Chinese text passes through unchanged."""
        text = "这是一段纯中文文本，没有任何数字或符号。"
        assert convert_all(text) == text

    def test_mixed_text(self):
        """English, Chinese, and numbers all handled in one pass."""
        result = convert_all("在2024年，小明读了Chapter III")
        assert "二零二四年" in result
        assert "Chapter 三" in result

    def test_int_to_chinese_small(self):
        """Small integers convert to Chinese correctly."""
        assert _int_to_chinese(1) == "一"
        assert _int_to_chinese(10) == "十"
        assert _int_to_chinese(15) == "十五"
        assert _int_to_chinese(99) == "九十九"
