"""MiMo TTS client using OpenAI-compatible chat completions API."""

from __future__ import annotations

import base64
import logging
import random
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from book_tts.models import TTSConfig

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 60


class MiMoTTSClient:
    """HTTP client for the MiMo TTS API (OpenAI chat completions compatible).

    Supports automatic rotation across multiple API keys and retries on
    transient failures.

    Parameters
    ----------
    config:
        TTS configuration including API keys, voice, style, and base URL.
    """

    def __init__(self, config: TTSConfig) -> None:
        if not config.api_keys:
            raise ValueError("At least one API key is required")

        self._config = config
        self._keys = list(config.api_keys)
        # Random start distributes load across keys across instances
        self._start_index = random.randrange(len(self._keys))
        self._session = self._build_session()

    def synthesize(
        self,
        text: str,
        voice: Optional[str] = None,
        style: Optional[str] = None,
        audio_format: str = "wav",
    ) -> bytes:
        """Synthesize *text* to audio and return raw audio bytes.

        Uses OpenAI-compatible chat completions API with audio output.

        Raises
        ------
        RuntimeError
            If all API keys are exhausted without a successful response.
        """
        if not text.strip():
            raise ValueError("text must not be empty")

        voice = voice or self._config.voice
        style = style or self._config.style
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"

        last_error: Optional[Exception] = None

        for offset in range(len(self._keys)):
            idx = (self._start_index + offset) % len(self._keys)
            api_key = self._keys[idx]

            try:
                response = self._session.post(
                    url,
                    json={
                        "model": "mimo-v2.5-tts",
                        "audio": {"format": audio_format, "voice": voice},
                        "messages": [
                            {"role": "user", "content": style},
                            {"role": "assistant", "content": text},
                        ],
                    },
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=_DEFAULT_TIMEOUT,
                )
                response.raise_for_status()
                logger.debug("TTS request succeeded with key index %d", idx)

                # Extract audio from response
                data = response.json()
                audio_data = data["choices"][0]["message"]["audio"]["data"]
                return base64.b64decode(audio_data)

            except requests.RequestException as exc:
                last_error = exc
                logger.warning(
                    "TTS request failed with key index %d: %s", idx, exc
                )
                continue
            except (KeyError, IndexError, ValueError) as exc:
                last_error = exc
                logger.warning(
                    "Failed to parse TTS response with key index %d: %s", idx, exc
                )
                continue

        raise RuntimeError(
            f"All {len(self._keys)} API keys failed. Last error: {last_error}"
        )

    @staticmethod
    def _build_session() -> requests.Session:
        session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["POST"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session
