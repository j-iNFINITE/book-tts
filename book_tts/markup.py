"""Markup injection: maps structural boundaries to SML tokens.

Parsers emit chapters with :class:`BoundaryType` metadata.  This module
converts those boundaries to SML tokens that survive the cleaning pipeline
and guide the multi-pass sentence splitter.
"""

from __future__ import annotations

from typing import List

from book_tts.models import BoundaryType
from book_tts.tts.sml import SML_BREAK, SML_PAUSE


_BOUNDARY_TO_TOKEN = {
    BoundaryType.PARAGRAPH: SML_BREAK,
    BoundaryType.SECTION: SML_PAUSE,
}


class MarkupInjector:
    """Inject SML tokens into a paragraph list based on boundary metadata.

    Usage::

        injector = MarkupInjector()
        tagged = injector.inject(paragraphs, boundaries)
        # tagged is a list of str with [break]/[pause] markers
    """

    def inject(
        self,
        paragraphs: list[str],
        boundaries: list[BoundaryType] | None = None,
    ) -> list[str]:
        """Return *paragraphs* with SML tokens inserted at boundary positions.

        If *boundaries* is ``None`` or empty, a ``PARAGRAPH`` boundary is
        assumed between every pair of consecutive paragraphs.
        """
        if not paragraphs:
            return []

        if not boundaries:
            boundaries = [BoundaryType.NONE] + [BoundaryType.PARAGRAPH] * (len(paragraphs) - 1)

        result: list[str] = []
        for i, para in enumerate(paragraphs):
            text = para.strip()
            if not text:
                continue

            btype = boundaries[i] if i < len(boundaries) else BoundaryType.PARAGRAPH

            # Prepend pause token for section boundaries (except first para).
            if btype == BoundaryType.SECTION and result:
                text = f"{SML_PAUSE} {text}"

            # Append break token for paragraph boundaries (except last para).
            if i < len(paragraphs) - 1:
                next_btype = boundaries[i + 1] if i + 1 < len(boundaries) else BoundaryType.PARAGRAPH
                if next_btype in (BoundaryType.PARAGRAPH, BoundaryType.SECTION):
                    text = f"{text} {SML_BREAK}"

            result.append(text)
        return result
