"""
Abstract base class for all STT model adapters.

Every new STT engine must subclass BaseSTTModel and implement:
  - load()              load weights into RAM / VRAM
  - transcribe()        single-shot offline inference
  - transcribe_stream() streaming (chunked) inference
  - unload()            free resources
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator

import numpy as np


# ── Data Containers ──────────────────────────────────────────────────────────


@dataclass
class STTConfig:
    """Configuration passed to a model adapter at load time."""

    model_id: str
    """Identifier understood by the underlying framework, e.g.
    ``"stt_en_fastconformer_hybrid_large_streaming_480ms"``."""

    language_code: str = "en"
    """BCP-47 language tag (``"en"``, ``"ru"``, …)."""

    sample_rate: int = 16_000
    """Expected sample rate of the input audio (Hz)."""

    device: str = "cuda"
    """PyTorch device string (``"cuda"``, ``"cuda:0"``, ``"cpu"``)."""

    extra: dict = field(default_factory=dict)
    """Arbitrary model-specific parameters (chunk_size, decoder_type, …)."""


@dataclass
class STTResult:
    """A single recognition result returned by the model."""

    transcript: str
    """Decoded text."""

    is_final: bool
    """``True`` if this is a complete / final hypothesis."""

    confidence: float = 0.0
    """Model confidence score in [0, 1] (if available)."""

    latency_ms: float = 0.0
    """Wall-clock time spent on this inference call (ms)."""

    metadata: dict = field(default_factory=dict)
    """Any extra info (RTF, decoder type, …)."""


# ── Abstract Base ────────────────────────────────────────────────────────────


class BaseSTTModel(ABC):
    """
    Abstract adapter interface for Speech-to-Text models.

    Lifecycle::

        adapter = SomeAdapter()
        adapter.load(config)
        result  = adapter.transcribe(audio_np)
        adapter.unload()
    """

    # ── Interface ────────────────────────────────────────────────────────

    @abstractmethod
    def load(self, config: STTConfig) -> None:
        """
        Load model weights into memory.

        Must be called once before :meth:`transcribe` /
        :meth:`transcribe_stream`.

        Parameters
        ----------
        config : STTConfig
            Model and runtime parameters.
        """

    @abstractmethod
    def transcribe(self, audio: np.ndarray) -> STTResult:
        """
        Transcribe a complete audio segment (offline mode).

        Parameters
        ----------
        audio : np.ndarray
            1-D float32 array, mono, at ``config.sample_rate`` Hz.

        Returns
        -------
        STTResult
        """

    @abstractmethod
    def transcribe_stream(
        self,
        audio_chunks: Iterator[np.ndarray],
    ) -> Iterator[STTResult]:
        """
        Generator-based streaming inference.

        For engines with native streaming (e.g. NeMo cache-aware) this
        delegates directly.  For engines without it (e.g. GigaAM) the
        adapter buffers internally and yields results chunk-by-chunk.

        Parameters
        ----------
        audio_chunks : Iterator[np.ndarray]
            Successive short segments of audio (e.g. 480 ms each).

        Yields
        ------
        STTResult
            Partial or final hypotheses as they become available.
        """

    @abstractmethod
    def unload(self) -> None:
        """Release model weights and free GPU / RAM."""

    # ── Properties ───────────────────────────────────────────────────────

    @property
    @abstractmethod
    def is_loaded(self) -> bool:
        """``True`` when the model is ready for inference."""

    @property
    @abstractmethod
    def model_info(self) -> dict:
        """Human-readable metadata dict (name, params, lang, …)."""

    # ── Convenience helpers (non-abstract) ───────────────────────────────

    def transcribe_timed(self, audio: np.ndarray) -> STTResult:
        """
        Same as :meth:`transcribe` but automatically fills
        ``result.latency_ms`` and ``result.metadata["rtf"]``.
        """
        t0 = time.perf_counter()
        result = self.transcribe(audio)
        elapsed = time.perf_counter() - t0
        duration_s = len(audio) / (
            self._config.sample_rate if hasattr(self, "_config") else 16_000
        )
        result.latency_ms = elapsed * 1000
        result.metadata["rtf"] = (
            round(elapsed / duration_s, 4) if duration_s > 0 else 0.0
        )
        return result
