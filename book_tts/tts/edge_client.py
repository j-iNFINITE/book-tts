"""Edge TTS client - free fallback when no API key provided."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class EdgeTTSClient:
    """Microsoft Edge TTS client (free, no API key required).

    Provides the same ``synthesize`` / ``close`` / context-manager interface
    as :class:`MiMoTTSClient` so the pipeline can swap them transparently.

    Parameters
    ----------
    voice:
        Edge TTS voice name.  Defaults to ``zh-CN-XiaoyiNeural`` (Chinese
        female, good for non-fiction).
    """

    DEFAULT_VOICE = "zh-CN-XiaoyiNeural"

    def __init__(self, voice: str = "") -> None:
        self._voice = voice or self.DEFAULT_VOICE

    # ── Same public interface as MiMoTTSClient ──────────────────────────────

    def synthesize(
        self,
        text: str,
        voice: Optional[str] = None,
        style: Optional[str] = None,
        audio_format: str = "mp3",
    ) -> bytes:
        """Synthesize *text* to audio bytes using Edge TTS.

        Parameters
        ----------
        text:
            Text to speak.  Must not be empty.
        voice:
            Override the default voice for this call only.
        style:
            Accepted for interface compatibility but ignored by Edge TTS.
        audio_format:
            Audio container format (Edge TTS always produces mp3 internally).

        Returns
        -------
        bytes
            Raw audio data (mp3).
        """
        import edge_tts  # deferred – optional dependency

        if not text.strip():
            raise ValueError("text must not be empty")

        voice = voice or self._voice

        async def _synthesize() -> bytes:
            communicate = edge_tts.Communicate(text, voice)
            audio_data = b""
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_data += chunk["data"]
            return audio_data

        return asyncio.run(_synthesize())

    def close(self) -> None:
        """No resources to clean up."""

    def __enter__(self) -> EdgeTTSClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
