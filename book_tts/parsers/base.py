"""Abstract base class for ebook parsers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List

if TYPE_CHECKING:
    from book_tts.models import ConversionProgress, ParseResult


class BaseBookParser(ABC):
    """Abstract base class that all ebook parsers must implement.

    Subclasses override ``parse`` and ``get_supported_formats`` to handle
    a specific ebook format (EPUB, MOBI, etc.).
    """

    @abstractmethod
    def parse(
        self,
        file_path: str | Path,
        progress_callback: Callable[[ConversionProgress], None] | None = None,
    ) -> ParseResult:
        """Parse an ebook file and return structured content."""
        ...

    @abstractmethod
    def get_supported_formats(self) -> List[str]:
        """Return a list of file extensions this parser supports (e.g. ``['.epub']``)."""
        ...

    def validate_file(self, file_path: str | Path) -> bool:
        """Check that *file_path* exists and has a supported extension."""
        path = Path(file_path)

        if not path.is_file():
            return False

        supported = {ext.lower() for ext in self.get_supported_formats()}
        return path.suffix.lower() in supported
