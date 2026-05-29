"""Unit tests for book_tts.utils.file_utils."""

import json
from pathlib import Path

from book_tts.utils.file_utils import (
    check_ffmpeg,
    safe_json_load,
    safe_json_save,
    sanitize_filename,
)


class TestSanitizeFilename:
    """Tests for sanitize_filename."""

    def test_sanitize_filename_basic(self):
        """Removes OS-reserved characters."""
        result = sanitize_filename('Hello<>:"/\\|?*World')
        assert "<" not in result
        assert ">" not in result
        assert ":" not in result
        assert "/" not in result
        assert "\\" not in result

    def test_sanitize_filename_long(self):
        """Truncates to max_length."""
        long_name = "A" * 300
        result = sanitize_filename(long_name, max_length=200)
        assert len(result) <= 200

    def test_sanitize_filename_collapses_whitespace(self):
        """Collapses runs of whitespace and underscores."""
        result = sanitize_filename("hello   world___test")
        assert "  " not in result
        assert "___" not in result
        assert "_" in result

    def test_sanitize_filename_empty(self):
        """Empty or all-special-char names become 'untitled'."""
        result = sanitize_filename("<>:/\\")
        assert result == "untitled"


class TestSafeJson:
    """Tests for safe_json_load and safe_json_save."""

    def test_safe_json_save_load_roundtrip(self, tmp_path: Path):
        """Data survives a save→load round-trip."""
        path = tmp_path / "data.json"
        data = {"key": "值", "list": [1, 2, 3]}
        safe_json_save(path, data)
        loaded = safe_json_load(path)
        assert loaded == data

    def test_safe_json_load_missing(self, tmp_path: Path):
        """Loading a missing file returns the default."""
        path = tmp_path / "nonexistent.json"
        result = safe_json_load(path, default={"fallback": True})
        assert result == {"fallback": True}

    def test_safe_json_load_corrupt(self, tmp_path: Path):
        """Loading a corrupt JSON file returns the default."""
        path = tmp_path / "bad.json"
        path.write_text("not valid json {{{", encoding="utf-8")
        result = safe_json_load(path, default=None)
        assert result is None

    def test_safe_json_save_creates_dirs(self, tmp_path: Path):
        """safe_json_save creates parent directories as needed."""
        path = tmp_path / "sub" / "dir" / "data.json"
        safe_json_save(path, [1, 2, 3])
        assert path.exists()
        assert json.loads(path.read_text(encoding="utf-8")) == [1, 2, 3]


class TestCheckFfmpeg:
    """Tests for check_ffmpeg."""

    def test_check_ffmpeg_returns_bool(self):
        """check_ffmpeg returns a boolean."""
        result = check_ffmpeg()
        assert isinstance(result, bool)
