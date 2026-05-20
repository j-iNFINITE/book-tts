"""Chinese number/symbol to words conversion for TTS-friendly output.

Converts Roman numerals, years, and common symbols to spoken Chinese forms
so the TTS engine pronounces them naturally.
"""

from __future__ import annotations

import re

# ── Digit mapping ────────────────────────────────────────────────────────────
_DIGIT_MAP: dict[str, str] = {
    "0": "零", "1": "一", "2": "二", "3": "三", "4": "四",
    "5": "五", "6": "六", "7": "七", "8": "八", "9": "九",
}

# Valid Roman numeral pattern (strict, standalone).
_ROMAN_RE = re.compile(
    r"(?<!\w)(M{0,3})(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})(?!\w)"
)

# Roman → integer mapping.
_ROMAN_VALUES: list[tuple[str, int]] = [
    ("M", 1000), ("CM", 900), ("D", 500), ("CD", 400),
    ("C", 100), ("XC", 90), ("L", 50), ("XL", 40),
    ("X", 10), ("IX", 9), ("V", 5), ("IV", 4), ("I", 1),
]

# Chapter/volume markers in Chinese and English.
_CHAPTER_PATTERN = re.compile(
    r"(第|Chapter\s*|CHAPTER\s*|VOLUME\s*|Volume\s*|"
    r"Part\s*|PART\s*|Book\s*|BOOK\s*)"
    r"([IVXLCDM]+)"
    r"([章节卷部篇]|\.?)",
    re.IGNORECASE,
)

# Common acronyms that look like Roman numerals but aren't.
_ROMAN_FALSE_POSITIVES: frozenset[str] = frozenset({
    "I", "V", "X",  # single letters — too ambiguous
    "MI", "DI", "VI", "LI", "CI",  # could be abbreviations
})


def _roman_to_int(roman: str) -> int:
    """Convert a valid Roman numeral string to integer."""
    result = 0
    i = 0
    s = roman.upper()
    while i < len(s):
        for r, val in _ROMAN_VALUES:
            if s[i:i + len(r)] == r:
                result += val
                i += len(r)
                break
        else:
            return 0
    return result


def _int_to_chinese(n: int) -> str:
    """Convert a positive integer to Chinese numeral string."""
    if n <= 0:
        return str(n)
    if n <= 10:
        return ["", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十"][n]
    if n < 20:
        return "十" + ("", "一", "二", "三", "四", "五", "六", "七", "八", "九")[n - 10]
    if n < 100:
        tens = n // 10
        ones = n % 10
        return _DIGIT_MAP[str(tens)] + "十" + (
            "" if ones == 0 else _DIGIT_MAP[str(ones)]
        )
    if n < 1000:
        hundreds = n // 100
        rest = n % 100
        result = _DIGIT_MAP[str(hundreds)] + "百"
        if rest == 0:
            return result
        if rest < 10:
            result += "零"
        result += _int_to_chinese(rest)
        return result
    # For larger numbers, fall back to digit-by-digit reading.
    return "".join(_DIGIT_MAP.get(d, d) for d in str(n))


def _convert_roman_numerals(text: str) -> str:
    """Convert Roman numerals to Chinese in chapter-like contexts."""
    def _repl_chapter(m: re.Match) -> str:
        prefix = m.group(1)
        roman = m.group(2)
        suffix = m.group(3)
        n = _roman_to_int(roman)
        if n > 0:
            cn = _int_to_chinese(n)
            # Normalise suffix: if English prefix like "Chapter", use Chinese suffix
            if prefix.lower().startswith(("chapter", "volume", "part", "book")):
                if suffix == ".":
                    return f"{prefix}{cn}"
                return f"{prefix}{cn}"
            return f"{prefix}{cn}{suffix}"
        return m.group(0)

    text = _CHAPTER_PATTERN.sub(_repl_chapter, text)
    return text


def _convert_years(text: str) -> str:
    """Convert 4-digit years followed by 年 to spoken Chinese form."""
    def _repl_year(m: re.Match) -> str:
        digits = m.group(1)
        if not digits.isdigit():
            return m.group(0)
        # Special case: 2000-2009 → 二零零X年
        if digits.startswith("20") and digits[2] == "0":
            spoken = "二零" + "".join(_DIGIT_MAP.get(d, d) for d in digits[2:])
        else:
            spoken = "".join(_DIGIT_MAP.get(d, d) for d in digits)
        return spoken + "年"

    text = re.sub(r"(\d{4})年", _repl_year, text)
    return text


def _convert_symbols(text: str) -> str:
    """Convert common symbols to Chinese words.

    Only converts symbols surrounded by CJK text or spaces, avoiding URLs.
    """
    # Protect URLs from symbol conversion.
    url_pattern = re.compile(r"https?://[^\s]+|www\.[^\s]+")
    urls: dict[str, str] = {}
    for i, m in enumerate(url_pattern.finditer(text)):
        key = f"\x00URL_{i}\x00"
        urls[key] = m.group(0)
        text = text.replace(m.group(0), key, 1)

    # Percent: 50% → 百分之五十
    def _repl_percent(m: re.Match) -> str:
        digits = m.group(1)
        if digits.endswith("."):
            digits = digits[:-1]
        try:
            n = int(digits) if "." not in digits else float(digits)
            if isinstance(n, int):
                return "百分之" + _int_to_chinese(n)
            else:
                return "百分之" + "点".join(
                    _int_to_chinese(int(p)) if p.isdigit() else p
                    for p in digits.split(".")
                )
        except ValueError:
            return m.group(0)

    text = re.sub(r"(\d+(?:\.\d+)?)\s*%", _repl_percent, text)

    # Plus / minus / equals — only when between CJK chars or digits with CJK context
    text = re.sub(
        r"(?<=[一-鿿\d\s])\+(?=[一-鿿\d\s])",
        "加", text,
    )
    text = re.sub(
        r"(?<=[一-鿿\d\s])-(?=[一-鿿\d\s])",
        "减", text,
    )
    text = re.sub(
        r"(?<=[一-鿿\d\s])×(?=[一-鿿\d\s])",
        "乘以", text,
    )
    text = re.sub(
        r"(?<=[一-鿿\d\s])=(?=[一-鿿\d\s])",
        "等于", text,
    )
    text = re.sub(
        r"(?<=[一-鿿\d\s])&(?=[一-鿿\d\s])",
        "和", text,
    )

    # Restore URLs.
    for key, url in urls.items():
        text = text.replace(key, url, 1)

    return text


def convert_all(text: str) -> str:
    """Apply all number/symbol conversions for Chinese TTS."""
    text = _convert_roman_numerals(text)
    text = _convert_years(text)
    text = _convert_symbols(text)
    return text
