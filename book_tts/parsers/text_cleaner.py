"""TTS text normalization and cleaning pipeline.

Operates on plain text only — no SML awareness.  SML token protection
and restoration is handled by the TTS layer (:class:`ParagraphSynthesizer`).
"""

from __future__ import annotations

import re
from typing import List

from book_tts.parsers.number_converter import convert_all

try:
    from tn.chinese.normalizer import Normalizer as ZhNormalizer

    _WETEXT_AVAILABLE = True
except ImportError:
    _WETEXT_AVAILABLE = False
    ZhNormalizer = None  # type: ignore[assignment,misc]

# Bracket removal: each pattern matches a full bracket pair including contents.
_BRACKET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"[（）]"),          # Chinese parentheses
    re.compile(r"[《》]"),          # Chinese book title marks
    re.compile(r"[【】]"),          # Chinese black brackets
    re.compile(r"\[.*?\]"),        # Square brackets with contents
    re.compile(r"\(.*?\)"),        # Parentheses with contents
]

_MULTI_SPACE = re.compile(r"[ \t]+")
_MULTI_NEWLINE = re.compile(r"\n{2,}")

# Patterns for fixing Calibre HTML span artifacts
_SPACE_BETWEEN_CJK = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")
_SPACE_BETWEEN_DIGITS = re.compile(r"(?<=\d)\s+(?=\d)")
_SPACE_BETWEEN_DIGIT_CJK = re.compile(r"(?<=\d)\s+(?=[\u4e00-\u9fff])")
_SPACE_BETWEEN_CJK_DIGIT = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=\d)")
_SPACE_BETWEEN_CJK_PUNCT = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[，。！？、；：\u201c\u201d\u2018\u2019《》【】（）])")
_SPACE_BETWEEN_PUNCT_CJK = re.compile(r"(?<=[，。！？、；：\u201c\u201d\u2018\u2019《》【】（）])\s+(?=[\u4e00-\u9fff])")

# URL/email protection patterns (MOSS-TTS-Nano style)
_URL_PATTERN = re.compile(r"https?://[^\s]+|www\.[^\s]+|[\w.]+@[\w.]+\.\w+")
_PLACEHOLDER_PREFIX = "\x00PROTECTED_"


class TextCleaner:
    """Clean and normalize plain text for TTS synthesis.

    Parameters
    ----------
    language:
        Hint for language-specific cleaning rules.  Accepts ``"zh"``,
        ``"en"`` or ``"auto"`` (default).
    """

    def __init__(self, language: str = "auto") -> None:
        self.language = language
        if _WETEXT_AVAILABLE:
            try:
                self._zh_normalizer = ZhNormalizer(remove_erhua=False)
            except Exception:
                self._zh_normalizer = None
        else:
            self._zh_normalizer = None

    def clean(self, text: str) -> str:
        """Run the full cleaning pipeline on *text* and return the result.

        *text* must be plain — SML tokens are stripped by bracket removal.
        Callers with SML-tagged text should run ``protect_sml_tokens()``
        before calling this method.
        """
        if not text:
            return ""

        text, protected = self._protect_spans(text)
        text = self._remove_brackets(text)
        text = self._normalize_whitespace(text)
        text = self._fix_internal_spaces(text)
        text = self._normalize_punctuation(text)
        text = self._clean_chinese(text)
        text = self._clean_english(text)
        text = convert_all(text)
        text = self._restore_spans(text, protected)

        return text.strip()

    def clean_paragraphs(self, paragraphs: List[str]) -> List[str]:
        """Clean a list of paragraphs, dropping any that become empty or are citations."""
        result: list[str] = []
        for raw in paragraphs:
            cleaned = self.clean(raw)
            if not cleaned:
                continue
            if self._is_citation(cleaned):
                continue
            result.append(cleaned)
        return result

    @staticmethod
    def _is_citation(text: str) -> bool:
        """Return True if *text* is a citation/footnote fragment that TTS should skip.

        Detects:
          - Short bibliographic fragments (< 30 chars) with mixed CJK/Latin
          - Publisher info (city: publisher, year)
          - Editor/collector names with initials
          - Page/source references
        """
        if len(text) > 50:
            return False

        text_lower = text.lower()

        citation_keywords = [
            "出版社", "出版", "编辑", "收藏家", "选自",
            "伦敦", "纽约", "北京", "上海", "东京",
            "press", "publishing", "publisher",
            "editor", "edited by", "translated by",
            "volume", "vol.", "chapter", "chap.",
            "page", "pp.", "pages",
            "记者", "报道",
        ]
        if any(kw in text_lower for kw in citation_keywords):
            return True

        # Mixed CJK + Latin initials (e.g., "W·H·I·布里克")
        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", text))
        has_latin = bool(re.search(r"[A-Za-z]", text))
        has_initials = bool(re.search(r"[A-Z]·[A-Z]", text))
        if has_cjk and has_latin and len(text) < 40:
            return True
        if has_initials and len(text) < 30:
            return True

        # Year + page references (e.g., "175－176页1871", "1911年")
        has_page_ref = bool(re.search(r"\d+[－\-]+\d+\s*[页年]", text))
        if has_page_ref:
            return True

        # Pure year + publisher pattern (e.g., "伦敦：麦克米兰，1907")
        year_publisher = bool(re.search(r"[：:].+[，,]\s*\d{4}", text))
        if year_publisher and len(text) < 40:
            return True

        return False

    @staticmethod
    def _remove_brackets(text: str) -> str:
        for pattern in _BRACKET_PATTERNS:
            text = pattern.sub("", text)
        return text

    @staticmethod
    def _normalize_whitespace(text: str) -> str:
        text = _MULTI_SPACE.sub(" ", text)
        text = _MULTI_NEWLINE.sub("\n", text)
        lines = [line.rstrip() for line in text.split("\n")]
        return "\n".join(lines)

    @staticmethod
    def _fix_internal_spaces(text: str) -> str:
        """Remove spaces/newlines that break up words for TTS.

        Calibre HTML often inserts spaces between <span> elements:
          '200 8 年' → '2008年'
          '小泉 纯一郎' → '小泉纯一郎'
          '2001 年' → '2001年'
        """
        text = _SPACE_BETWEEN_CJK.sub("", text)
        text = _SPACE_BETWEEN_DIGITS.sub("", text)
        text = _SPACE_BETWEEN_DIGIT_CJK.sub("", text)
        text = _SPACE_BETWEEN_CJK_DIGIT.sub("", text)
        text = _SPACE_BETWEEN_CJK_PUNCT.sub("", text)
        text = _SPACE_BETWEEN_PUNCT_CJK.sub("", text)
        return text

    @staticmethod
    def _protect_spans(text: str) -> tuple[str, dict[str, str]]:
        """Replace URLs/emails with placeholders to prevent corruption during cleaning."""
        protected: dict[str, str] = {}
        counter = 0

        def _replace(match: re.Match[str]) -> str:
            nonlocal counter
            key = f"{_PLACEHOLDER_PREFIX}{counter:04d}\x00"
            protected[key] = match.group(0)
            counter += 1
            return key

        text = _URL_PATTERN.sub(_replace, text)
        return text, protected

    @staticmethod
    def _restore_spans(text: str, protected: dict[str, str]) -> str:
        """Restore URLs/emails from placeholders."""
        for key, value in protected.items():
            text = text.replace(key, value)
        return text

    @staticmethod
    def _normalize_punctuation(text: str) -> str:
        text = re.sub(r"。{3,}", "……", text)
        text = re.sub(r"！{2,}", "！", text)
        text = re.sub(r"？{2,}", "？", text)
        return text

    @staticmethod
    def _clean_chinese(text: str) -> str:
        """Remove Chinese annotation prefix markers only at line start.

        Matches patterns like: "注:", "注释:", "释:", "译:", "注[1]"
        """
        text = re.sub(r"^[注释译]+([:：]|\s*\[\d+\])\s*", "", text, flags=re.MULTILINE)
        return text

    @staticmethod
    def _clean_english(text: str) -> str:
        # Collapse repeated punctuation: "!!!" → "!"
        text = re.sub(r"([!?.])\1+", r"\1", text)
        return text
