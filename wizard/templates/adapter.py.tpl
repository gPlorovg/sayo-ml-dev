"""
{class_name} — STT adapter.

TODO: Describe the model and its capabilities.

Requires:
    TODO: list pip packages
"""

from __future__ import annotations

import logging
import time
from typing import Iterator

import numpy as np

from ..base import BaseSTTModel, STTConfig, STTResult
from ..registry import ModelRegistry

logger = logging.getLogger(__name__)


@ModelRegistry.register("{adapter}")
class {class_name}(BaseSTTModel):
    """TODO: Adapter description."""

    def __init__(self) -> None:
        self._model = None
        self._config: STTConfig | None = None

    # ── Interface ────────────────────────────────────────────────────

    def load(self, config: STTConfig) -> None:
        self._config = config
        logger.info("Loading model '%s' on device '%s'...",
                    config.model_id, config.device)

        # TODO: Load your model here
        # self._model = ...

        logger.info("Model loaded.")

    def transcribe(self, audio: np.ndarray) -> STTResult:
        self._assert_loaded()

        t0 = time.perf_counter()

        # TODO: Implement transcription
        text = ""

        elapsed = time.perf_counter() - t0
        duration_s = len(audio) / self._config.sample_rate
        rtf = elapsed / duration_s if duration_s > 0 else 0.0

        return STTResult(
            transcript=text,
            is_final=True,
            confidence=0.0,
            latency_ms=elapsed * 1000,
            metadata={{"adapter": "{adapter}", "rtf": round(rtf, 4)}},
        )

    def transcribe_stream(
        self,
        audio_chunks: Iterator[np.ndarray],
    ) -> Iterator[STTResult]:
        """Chunk-by-chunk inference (implement if model supports streaming)."""
        self._assert_loaded()

        chunk_idx = 0
        for chunk in audio_chunks:
            chunk_idx += 1
            t0 = time.perf_counter()

            # TODO: Implement streaming transcription
            text = ""

            elapsed = time.perf_counter() - t0
            yield STTResult(
                transcript=text,
                is_final=False,
                confidence=0.0,
                latency_ms=elapsed * 1000,
                metadata={{"adapter": "{adapter}", "chunk_idx": chunk_idx}},
            )

    def unload(self) -> None:
        if self._model is not None:
            del self._model
            self._model = None
            logger.info("Model unloaded.")
        self._config = None

    # ── Properties ───────────────────────────────────────────────────

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def model_info(self) -> dict:
        return {{
            "name": "{class_name}",
            "streaming": False,
        }}

    # ── Internal ─────────────────────────────────────────────────────

    def _assert_loaded(self) -> None:
        if self._model is None:
            raise RuntimeError(
                "{class_name} is not loaded. Call load() first."
            )
