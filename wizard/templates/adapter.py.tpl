"""
{class_name} — STT adapter.

Implement this adapter by wiring your model framework into:
  - load(): initialize model and load weights
  - transcribe(): offline inference -> STTResult
  - transcribe_stream(): streaming inference
"""

from __future__ import annotations

from typing import Iterator

import numpy as np
import structlog

from ..base import BaseSTTModel, STTConfig, STTResult

logger = structlog.get_logger(__name__)


class {class_name}(BaseSTTModel):
    """Adapter scaffold for model type '{adapter}'."""

    def __init__(self) -> None:
        self._model = None
        self._config: STTConfig | None = None

    # ── Interface ────────────────────────────────────────────────────

    def load(self, config: STTConfig) -> None:
        self._config = config
        logger.info("Loading model", model_id=config.model_id, device=config.device)

        # TODO: Load your model here
        # self._model = ...

        # Fail fast for unimplemented scaffolds
        raise NotImplementedError(
            "Adapter '{class_name}' is scaffold-only. "
            "Implement load() and remove this exception."
        )
        logger.info("Model loaded", model_id=config.model_id)

    def transcribe(self, audio: np.ndarray) -> STTResult:
        self._assert_loaded()
        raise NotImplementedError(
            "Implement transcribe() for adapter '{class_name}'."
        )

    def transcribe_stream(
        self,
        audio_chunks: Iterator[np.ndarray],
    ) -> Iterator[STTResult]:
        """Chunk-by-chunk inference."""
        self._assert_loaded()
        raise NotImplementedError(
            "Implement transcribe_stream() for adapter '{class_name}'."
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

    # ── Internal ─────────────────────────────────────────────────────

    def _assert_loaded(self) -> None:
        if self._model is None:
            raise RuntimeError(
                "{class_name} is not loaded. Call load() first."
            )
