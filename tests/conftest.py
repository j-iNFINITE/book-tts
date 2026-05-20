"""Shared test fixtures and configuration."""
import os
import sys
from pathlib import Path

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# Test data paths
TEST_DIR = Path(__file__).parent
PROJECT_DIR = TEST_DIR.parent
TEST_EPUB = PROJECT_DIR / "test.epub"
TEST_MOBI = PROJECT_DIR / "test2.mobi"


@pytest.fixture
def test_epub_path():
    """Return path to test EPUB file."""
    if not TEST_EPUB.exists():
        pytest.skip(f"Test EPUB not found: {TEST_EPUB}")
    return str(TEST_EPUB)


@pytest.fixture
def test_mobi_path():
    """Return path to test MOBI file."""
    if not TEST_MOBI.exists():
        pytest.skip(f"Test MOBI not found: {TEST_MOBI}")
    return str(TEST_MOBI)


@pytest.fixture
def sample_metadata():
    """Return sample BookMetadata."""
    from book_tts.models import BookMetadata
    return BookMetadata(
        title="Test Book",
        author="Test Author",
        language="zh",
        description="A test book",
        publisher="Test Publisher",
        publication_date="2024-01-01"
    )


@pytest.fixture
def sample_chapter():
    """Return sample Chapter."""
    from book_tts.models import Chapter
    return Chapter(
        index=0,
        title="Test Chapter",
        paragraphs=("First paragraph.", "Second paragraph.", "Third paragraph."),
        source_file="test.html",
        word_count=9
    )


@pytest.fixture
def tts_config():
    """Return test TTS config."""
    from book_tts.models import TTSConfig
    return TTSConfig(
        api_keys=["test_key_1", "test_key_2"],
        voice="冰糖",
        style="Test style",
        base_url="https://api.xiaomimimo.com/v1",
        rpm_limit=90
    )
