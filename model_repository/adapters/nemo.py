"""NeMo FastConformer adapter with cache-aware streaming."""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass
from typing import Iterator

import numpy as np
import structlog

from ..base import BaseSTTModel, STTConfig, STTResult

logger = structlog.get_logger(__name__)


def _require_nemo():
    try:
        import nemo.collections.asr as nemo_asr  # noqa: F401

        return nemo_asr
    except ImportError as exc:
        raise ImportError(
            "NVIDIA NeMo is required for NemoAdapter. "
            "Install: pip install nemo_toolkit[asr]"
        ) from exc


@dataclass
class _StreamingState:
    cache_last_channel: object = None
    cache_last_time: object = None
    cache_last_channel_len: object = None
    cache_pre_encode: object = None
    pre_encode_cache_size: int = 0
    previous_hypotheses: object = None
    pred_out_stream: object = None
    preprocessor: object = None


class NemoAdapter(BaseSTTModel):
    """NeMo adapter using conformer_stream_step for true streaming."""

    def __init__(self) -> None:
        self._model = None
        self._config: STTConfig | None = None
        self._stream: _StreamingState | None = None
        self._decoder_type = "ctc"
        self._chunk_ms = 560
        self._chunk_samples = 8960
        self._stream_transcript_max_chars = 150

    def load(self, config: STTConfig) -> None:
        nemo_asr = _require_nemo()
        import torch
        from nemo.collections.asr.models.ctc_bpe_models import EncDecCTCModelBPE
        from omegaconf import OmegaConf

        self._config = config
        self._decoder_type = str(config.extra.get("decoder_type", "ctc"))
        self._chunk_ms = int(config.extra.get("chunk_ms", 560))
        self._chunk_samples = int(
            config.extra.get(
                "chunk_samples",
                int(config.sample_rate * self._chunk_ms / 1000),
            )
        )
        _stmc = config.extra.get("stream_transcript_max_chars", 150)
        self._stream_transcript_max_chars = int(_stmc) if _stmc is not None else 150

        weights = config.extra.get("weights", [])
        weight_path = weights[0] if isinstance(weights, list) and weights else None

        logger.info(
            "Loading NeMo model",
            model_id=config.model_id,
            device=config.device,
            decoder_type=self._decoder_type,
            chunk_ms=self._chunk_ms,
            chunk_samples=self._chunk_samples,
            stream_transcript_max_chars=self._stream_transcript_max_chars,
            weight_path=weight_path,
        )

        if weight_path:
            self._model = nemo_asr.models.ASRModel.restore_from(
                restore_path=weight_path
            )
        else:
            self._model = nemo_asr.models.ASRModel.from_pretrained(
                model_name=config.model_id
            )

        self._model = self._model.to(config.device)
        self._model.change_decoding_strategy(decoder_type=self._decoder_type)
        self._model.eval()

        cfg = copy.deepcopy(self._model._cfg)
        OmegaConf.set_struct(cfg.preprocessor, False)
        cfg.preprocessor.dither = 0.0
        cfg.preprocessor.pad_to = 0
        cfg.preprocessor.normalize = "None"

        preprocessor = EncDecCTCModelBPE.from_config_dict(cfg.preprocessor)
        preprocessor.to(self._model.device)

        cache_last_channel, cache_last_time, cache_last_channel_len = (
            self._model.encoder.get_initial_cache_state(batch_size=1)
        )
        pre_encode_cache_size = self._model.encoder.streaming_cfg.pre_encode_cache_size[
            1
        ]
        num_channels = self._model.cfg.preprocessor.features
        cache_pre_encode = torch.zeros(
            (1, num_channels, pre_encode_cache_size),
            device=self._model.device,
        )

        self._stream = _StreamingState(
            cache_last_channel=cache_last_channel,
            cache_last_time=cache_last_time,
            cache_last_channel_len=cache_last_channel_len,
            cache_pre_encode=cache_pre_encode,
            pre_encode_cache_size=pre_encode_cache_size,
            preprocessor=preprocessor,
        )

        logger.info("NeMo model loaded", model_id=config.model_id)

    def transcribe(self, audio: np.ndarray) -> STTResult:
        self._assert_loaded()
        if self._config is None:
            raise RuntimeError("Adapter config is missing.")

        audio_f32 = np.asarray(audio, dtype=np.float32)
        t0 = time.perf_counter()
        text = self._transcribe_array(audio_f32)
        elapsed = time.perf_counter() - t0
        duration_s = (
            len(audio_f32) / self._config.sample_rate
            if self._config.sample_rate > 0
            else 0.0
        )
        rtf = elapsed / duration_s if duration_s > 0 else 0.0

        return STTResult(
            transcript=text,
            is_final=True,
            confidence=0.0,
            latency_ms=elapsed * 1000,
            metadata={
                "adapter": "nemo",
                "decoder_type": self._decoder_type,
                "rtf": round(rtf, 4),
            },
        )

    def transcribe_stream(
        self,
        audio_chunks: Iterator[np.ndarray],
    ) -> Iterator[STTResult]:
        self._assert_loaded()
        import torch

        self._reset_streaming_cache()
        if self._stream is None:
            raise RuntimeError("Streaming state is not initialized.")

        buffer = np.array([], dtype=np.float32)
        chunk_idx = 0
        final_full_text = ""

        for incoming in audio_chunks:
            incoming_f32 = np.asarray(incoming, dtype=np.float32)
            buffer = np.concatenate([buffer, incoming_f32])

            while len(buffer) >= self._chunk_samples:
                chunk_np = buffer[: self._chunk_samples]
                buffer = buffer[self._chunk_samples :]
                chunk_idx += 1

                t0 = time.perf_counter()
                full_text = self._stream_step(
                    torch.tensor(chunk_np, dtype=torch.float32)
                )
                elapsed = time.perf_counter() - t0
                final_full_text = full_text

                yield STTResult(
                    transcript=self._clip_stream_transcript(full_text),
                    is_final=False,
                    confidence=0.0,
                    latency_ms=elapsed * 1000,
                    metadata={
                        "adapter": "nemo",
                        "mode": "streaming",
                        "chunk_idx": chunk_idx,
                        "full_text": full_text,
                    },
                )

        if len(buffer) > 0:
            import torch

            if len(buffer) < self._chunk_samples:
                buffer = np.pad(
                    buffer, (0, self._chunk_samples - len(buffer)), mode="constant"
                )

            t0 = time.perf_counter()
            full_text = self._stream_step(torch.tensor(buffer, dtype=torch.float32))
            elapsed = time.perf_counter() - t0
            final_full_text = full_text

            yield STTResult(
                transcript=self._clip_stream_transcript(full_text),
                is_final=True,
                confidence=0.0,
                latency_ms=elapsed * 1000,
                metadata={
                    "adapter": "nemo",
                    "mode": "streaming_tail",
                    "full_text": full_text,
                },
            )
        elif final_full_text:
            # No tail chunk, but stream had processed chunks.
            yield STTResult(
                transcript=self._clip_stream_transcript(final_full_text),
                is_final=True,
                confidence=0.0,
                latency_ms=0.0,
                metadata={
                    "adapter": "nemo",
                    "mode": "final",
                    "full_text": final_full_text,
                    "chunks": chunk_idx,
                },
            )

        self._reset_streaming_cache()

    def unload(self) -> None:
        if self._model is not None:
            try:
                import torch

                del self._model
                self._model = None
                self._stream = None
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except ImportError:
                self._model = None
                self._stream = None

            logger.info("NeMo model unloaded")
        self._config = None

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def model_info(self) -> dict:
        return {
            "name": "NemoAdapter",
            "model_id": self._config.model_id if self._config else None,
            "decoder_type": self._decoder_type,
            "chunk_ms": self._chunk_ms,
            "chunk_samples": self._chunk_samples,
            "stream_transcript_max_chars": self._stream_transcript_max_chars,
            "loaded": self.is_loaded,
            "streaming": True,
        }

    def _clip_stream_transcript(self, text: str) -> str:
        n = self._stream_transcript_max_chars
        if n <= 0 or len(text) <= n:
            return text
        return text[-n:]

    def _stream_step(self, audio: "object") -> str:
        import torch

        if self._stream is None:
            raise RuntimeError("Streaming state is not initialized.")

        stream_state = self._stream
        device = self._model.device

        audio_signal = audio.unsqueeze(0).to(device)
        audio_signal_len = torch.tensor(
            [audio.numel()], dtype=torch.float32, device=device
        )
        processed_signal, processed_signal_length = stream_state.preprocessor(
            input_signal=audio_signal,
            length=audio_signal_len,
        )

        processed_signal = torch.cat(
            [stream_state.cache_pre_encode, processed_signal], dim=-1
        )
        processed_signal_length += stream_state.cache_pre_encode.shape[1]
        stream_state.cache_pre_encode = processed_signal[
            :, :, -stream_state.pre_encode_cache_size :
        ]

        with torch.no_grad():
            (
                stream_state.pred_out_stream,
                transcribed_texts,
                stream_state.cache_last_channel,
                stream_state.cache_last_time,
                stream_state.cache_last_channel_len,
                stream_state.previous_hypotheses,
            ) = self._model.conformer_stream_step(
                processed_signal=processed_signal,
                processed_signal_length=processed_signal_length,
                cache_last_channel=stream_state.cache_last_channel,
                cache_last_time=stream_state.cache_last_time,
                cache_last_channel_len=stream_state.cache_last_channel_len,
                keep_all_outputs=False,
                previous_hypotheses=stream_state.previous_hypotheses,
                previous_pred_out=stream_state.pred_out_stream,
                drop_extra_pre_encoded=None,
                return_transcription=True,
            )

        return self._extract_first_text(transcribed_texts)

    def _reset_streaming_cache(self) -> None:
        import torch

        if self._stream is None or self._model is None:
            return

        stream_state = self._stream
        (
            stream_state.cache_last_channel,
            stream_state.cache_last_time,
            stream_state.cache_last_channel_len,
        ) = self._model.encoder.get_initial_cache_state(batch_size=1)

        num_channels = self._model.cfg.preprocessor.features
        stream_state.cache_pre_encode = torch.zeros(
            (1, num_channels, stream_state.pre_encode_cache_size),
            device=self._model.device,
        )
        stream_state.previous_hypotheses = None
        stream_state.pred_out_stream = None

    def _transcribe_array(self, audio: np.ndarray) -> str:
        hypotheses = self._model.transcribe(audio=[audio], batch_size=1)
        return self._extract_first_text(hypotheses)

    @staticmethod
    def _extract_first_text(hypotheses: object) -> str:
        """Extract plain transcript from various NeMo hypothesis formats."""
        if hypotheses is None:
            return ""

        first = hypotheses[0] if isinstance(hypotheses, list) else hypotheses
        if first is None:
            return ""

        text = getattr(first, "text", None)
        if isinstance(text, str):
            return text

        return str(first)

    def _assert_loaded(self) -> None:
        if self._model is None:
            raise RuntimeError("NemoAdapter is not loaded. Call load() first.")
