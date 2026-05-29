# Book TTS

Convert ebooks (EPUB, MOBI, Markdown) to audiobooks using TTS synthesis.

## Features

- **Multi-format input**: EPUB, MOBI/AZW, Markdown
- **MiMo TTS**: High-quality Chinese TTS via OpenAI-compatible API
- **Smart text preprocessing**: Number normalization, CJK-Latin spacing, punctuation cleanup, bracket removal
- **Chapter-aware**: Automatic chapter detection, boilerplate filtering (TOC, copyright, preface, etc.)
- **Speech markup**: SML tokens (`[break]`, `[pause]`) for natural pacing between paragraphs and sections
- **Concurrent synthesis**: Parallel paragraph processing with rate limiting (up to 90 RPM)
- **Web GUI**: Gradio-based interface for easy use
- **Dry run mode**: Preview parsed chapters without calling TTS

## Installation

```bash
# Basic install
pip install book-tts

# With WeTextProcessing (better Chinese text normalization)
pip install book-tts[wetext]

# All extras
pip install book-tts[dev,wetext]
```

### Prerequisites

- Python 3.10+
- FFmpeg (required for audio processing)
  - Ubuntu/Debian: `sudo apt install ffmpeg`
  - macOS: `brew install ffmpeg`
  - Windows: Download from https://ffmpeg.org/download.html

## Quick Start

### CLI Usage

```bash
# Convert with API key
export MIMO_TTS_API_KEYS="your-api-key"
book-tts mybook.epub

# With multiple API keys (for load balancing)
export MIMO_TTS_API_KEYS="key1,key2,key3"
book-tts mybook.epub

# Dry run (preview chapters without TTS)
book-tts mybook.epub --dry-run

# Launch web GUI
book-tts --gui
```

### GUI Usage

```bash
book-tts --gui
```

Opens a web interface at http://localhost:7860 where you can:

1. Upload ebook files (EPUB, MOBI, AZW, Markdown)
2. Select chapters to convert
3. Configure TTS settings (voice, style, API keys)
4. Monitor conversion progress with ETA
5. Download generated MP3 files

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `MIMO_TTS_API_KEYS` | Comma-separated API keys | (none) |

### CLI Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `input` | Input ebook file or directory | (required) |
| `--output` | Output directory | `audiobook_output` |
| `--voice` | TTS voice name | `冰糖` |
| `--style` | TTS style description | (see below) |
| `--base-url` | TTS API endpoint | `https://api.xiaomimimo.com/v1` |
| `--api-key` | Single API key | (none) |
| `--api-keys` | Multiple API keys (space-separated) | (none) |
| `--dry-run` | Preview parsed chapters without TTS | false |
| `--gui` | Launch Gradio web interface | false |
| `--verbose`, `-v` | Enable debug logging | false |

### Default Voice Style

The default style is a warm, sweet, expressive female voice with moderate pace and clear articulation, suitable for long-form audiobook reading:

> 温柔、甜美、富有感情的女性声音，语速适中，吐字清晰，带有自然的抑扬顿挫，适合长时间有声书朗读。

### Supported Voices

Voice names are passed directly to the MiMo TTS API. Use `冰糖` or other available voice identifiers from your TTS provider.

## Architecture

```
book_tts/
├── main.py               # CLI entry point and argument parsing
├── pipeline.py           # Conversion orchestration (parse → synthesize → merge)
├── config.py             # Constants and default values
├── models.py             # Data models (Chapter, BookMetadata, TTSConfig, etc.)
├── markup.py             # SML token injection based on structural boundaries
├── parsers/
│   ├── base.py           # Abstract parser interface
│   ├── epub_parser.py    # EPUB extraction via ebooklib
│   ├── mobi_parser.py    # MOBI/AZW extraction
│   ├── markdown_parser.py
│   ├── text_cleaner.py   # Text normalization pipeline
│   └── number_converter.py
├── tts/
│   ├── client.py         # MiMo TTS HTTP client (OpenAI-compatible)
│   ├── synthesizer.py    # Paragraph-level synthesis with batching
│   ├── rate_limiter.py   # Token bucket rate limiter
│   └── sml.py            # Speech markup token definitions and handling
├── audio/
│   └── merger.py         # MP3 chapter merging via pydub
├── gui/
│   ├── app.py            # Gradio web interface
│   ├── components.py     # Reusable UI components
│   └── state.py          # GUI conversion state management
└── utils/
    ├── progress.py       # Thread-safe progress tracking
    ├── history.py        # Voice/style usage history
    └── file_utils.py     # File system helpers, FFmpeg check
```

### Pipeline Flow

1. **Parse**: Extract text from ebook, detect chapters, filter boilerplate (TOC, copyright, preface, index, etc.)
2. **Clean**: Normalize text (numbers, brackets, CJK spacing, punctuation), inject SML tokens
3. **Synthesize**: Convert paragraphs to audio via MiMo TTS API with concurrent workers
4. **Merge**: Concatenate paragraph audio into per-chapter MP3 files with paragraph pauses

### Boilerplate Filtering

The parser automatically skips non-content chapters based on:

- EPUB guide types: `toc`, `titlepage`, `copyright`, `colophon`, `dedication`, `acknowledgments`, `bibliography`, `index`, `glossary`, `appendix`
- Name keywords: `目录`, `版权`, `封面`, `扉页`, `序言`, `前言`, `后记`, `附录`, `参考文献`, `索引`, `词汇表`, `致谢`

## Development

```bash
# Clone repository
git clone https://github.com/yourusername/book_tts.git
cd book_tts

# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Run linting
ruff check book_tts/
mypy book_tts/

# Format code
black book_tts/
```

## License

MIT License - see LICENSE file
