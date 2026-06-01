"""All constants and default configuration values for book_tts."""

from __future__ import annotations

# ── TTS defaults ──────────────────────────────────────────────────────────────
DEFAULT_BASE_URL: str = "https://api.xiaomimimo.com/v1"
DEFAULT_VOICE: str = "冰糖"
DEFAULT_STYLE: str = (
    "温柔、甜美、富有感情的女性声音，语速适中，吐字清晰，"
    "带有自然的抑扬顿挫，适合长时间有声书朗读。"
)
MAX_TTS_CHARS: int = 3000
DEFAULT_PARA_PAUSE_MS: int = 300

# ── Output ────────────────────────────────────────────────────────────────────
DEFAULT_OUTPUT_DIR: str = "audiobook_output"

# ── Audio output ──────────────────────────────────────────────────────────────
AUDIO_FORMAT: str = "mp3"
OUTPUT_FORMAT: str = "m4b"

# ── Concurrency / rate-limiting ───────────────────────────────────────────────
MAX_WORKERS: int = 10
DEFAULT_RPM_LIMIT: int = 90  # requests per minute

# ── Boilerplate detection ─────────────────────────────────────────────────────
SKIP_GUIDE_TYPES: frozenset[str] = frozenset({
    "toc",
    "titlepage",
    "copyright",
    "colophon",
    "dedication",
    "acknowledgments",
    "bibliography",
    "index",
    "glossary",
    "appendix",
})

SKIP_NAME_KEYWORDS: frozenset[str] = frozenset({
    "目录",
    "版权",
    "封面",
    "扉页",
    "序言",
    "前言",
    "后记",
    "附录",
    "参考文献",
    "索引",
    "词汇表",
    "致谢",
})

MIN_VISIBLE_CHARS: int = 30

# ── Sentence splitting ───────────────────────────────────────────────────────
MIN_MERGEABLE_CHARS: int = 10  # merge sentences shorter than this into adjacent
