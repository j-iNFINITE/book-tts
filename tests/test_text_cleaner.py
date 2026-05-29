"""Unit tests for book_tts.parsers.text_cleaner."""

from book_tts.parsers.text_cleaner import TextCleaner


class TestTextCleanerClean:
    """Tests for TextCleaner.clean() pipeline."""

    def test_clean_empty(self):
        """Empty string returns empty."""
        cleaner = TextCleaner()
        assert cleaner.clean("") == ""
        assert cleaner.clean("   ") == ""

    def test_clean_brackets(self):
        """Removes [] () （） 【】 《》 brackets and their contents/pairs."""
        cleaner = TextCleaner()
        result = cleaner.clean("这是[注释]测试（括号）【方括号】")
        assert result == "这是测试括号方括号"

    def test_clean_whitespace(self):
        """Normalizes multiple spaces and newlines."""
        cleaner = TextCleaner()
        result = cleaner._normalize_whitespace("hello   world\n\n\nfoo")
        assert "  " not in result
        assert result == "hello world\nfoo"

    def test_clean_cjk_spaces(self):
        """Removes spaces between CJK characters (Calibre HTML artifact)."""
        cleaner = TextCleaner()
        result = cleaner._fix_internal_spaces("小泉 纯一郎")
        assert result == "小泉纯一郎"

    def test_clean_digit_spaces(self):
        """Removes spaces between digits and between digits and CJK."""
        cleaner = TextCleaner()
        result = cleaner._fix_internal_spaces("200 8 年")
        assert result == "2008年"

    def test_clean_punctuation(self):
        """Collapses repeated Chinese punctuation."""
        cleaner = TextCleaner()
        result = cleaner._normalize_punctuation("好。。。。。。")
        assert result == "好……"

    def test_clean_urls_protected(self):
        """URLs are preserved during the full cleaning pipeline."""
        cleaner = TextCleaner()
        url = "https://example.com/path?q=1"
        result = cleaner.clean(f"Visit {url} for more info")
        assert url in result

    def test_clean_citation_detection(self):
        """Short citation fragments are detected and filtered."""
        assert TextCleaner._is_citation("北京大学出版社") is True
        assert TextCleaner._is_citation("W·H·I·布里克") is True
        assert TextCleaner._is_citation(
            "这是一段很长的正文内容，讲述了故事的来龙去脉，包含了许多细节和人物描写。"
        ) is False

    def test_clean_number_conversion(self):
        """Roman numerals in chapter context are converted to Chinese."""
        cleaner = TextCleaner()
        result = cleaner.clean("Chapter IV")
        assert result == "Chapter 四"

    def test_clean_paragraphs(self):
        """List cleaning drops empty and citation paragraphs."""
        cleaner = TextCleaner()
        paragraphs = [
            "第一段内容。",
            "",
            "北京大学出版社",
            "这是一段很长的正文内容，讲述了故事的来龙去脉，包含了许多细节描写和人物刻画，内容非常丰富。",
        ]
        result = cleaner.clean_paragraphs(paragraphs)
        assert len(result) == 2
        assert "第一段" in result[0]
        assert "很长" in result[1]

    def test_clean_repeated_english_punctuation(self):
        """Collapses repeated English punctuation."""
        cleaner = TextCleaner()
        result = cleaner._clean_english("Really!!! No way???")
        assert result == "Really! No way?"

    def test_clean_chinese_annotation_prefix(self):
        """Removes Chinese annotation markers at line start."""
        cleaner = TextCleaner()
        result = cleaner._clean_chinese("注: 这是一条注释")
        assert result == "这是一条注释"
