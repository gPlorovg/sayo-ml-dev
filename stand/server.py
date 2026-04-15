"""
gRPC test stand server.

Loads one model repository entry and exposes SayoService.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from contextlib import suppress
from queue import Queue
from pathlib import Path
from typing import Any

import grpc
import numpy as np
import structlog

from model_repository.model_repository import ModelRepository
from proto import sayo_pb2, sayo_pb2_grpc

logger = structlog.get_logger("stand.server")

ROOT = Path(__file__).resolve().parents[1]


class StandSayoService(sayo_pb2_grpc.SayoServiceServicer):
    def __init__(
        self,
        adapter,
        model_entry: Any,
    ):
        self._adapter = adapter
        self._model_entry = model_entry
        self._sample_rate = model_entry.sample_rate

    async def HealthCheck(self, request, context):
        runtime = (
            self._model_entry.runtime
            if isinstance(self._model_entry.runtime, dict)
            else {}
        )
        default_quantization = runtime.get("audio_quantization", "pcm_f32le")
        quantization = (
            sayo_pb2.AUDIO_QUANTIZATION_PCM_S16LE
            if str(default_quantization).lower() == "pcm_s16le"
            else sayo_pb2.AUDIO_QUANTIZATION_PCM_F32LE
        )
        descriptor = sayo_pb2.ModelDescriptor(
            model_id=self._model_entry.model_id,
            description=self._model_entry.description,
            language_code=self._model_entry.language_code,
            sample_rate_hertz=self._model_entry.sample_rate,
            audio_quantization=quantization,
            chunk_duration_ms=int(runtime.get("chunk_duration_ms", 560)),
            supports_interim_results=bool(
                runtime.get("supports_interim_results", True)
            ),
            latency_ms=float(self._model_entry.latency),
        )
        return sayo_pb2.HealthCheckResponse(
            ready=True,
            message="stand is ready",
            models=[descriptor],
        )

    @staticmethod
    def _chunk_to_f32(audio_chunk: bytes, quantization: int) -> np.ndarray:
        if quantization == sayo_pb2.AUDIO_QUANTIZATION_PCM_S16LE:
            pcm16 = np.frombuffer(audio_chunk, dtype=np.int16)
            return (pcm16.astype(np.float32) / 32768.0).copy()

        # Default/fallback: treat chunk as float32 little-endian PCM.
        return np.frombuffer(audio_chunk, dtype=np.float32).copy()

    @staticmethod
    def _safe_response(
        transcript: object,
        is_final: object,
        confidence: object,
        metadata: dict[str, object] | None = None,
    ) -> sayo_pb2.StreamingRecognizeResponse:
        safe_meta: dict[str, str] = {}
        if metadata:
            for key, value in metadata.items():
                safe_meta[str(key)] = str(value)

        return sayo_pb2.StreamingRecognizeResponse(
            transcript="" if transcript is None else str(transcript),
            is_final=bool(is_final),
            confidence=float(confidence) if confidence is not None else 0.0,
            metadata=safe_meta,
        )

    async def StreamingRecognize(self, request_iterator, context):
        config = None
        chunk_count = 0
        input_queue: Queue[np.ndarray | None] = Queue()
        result_queue: asyncio.Queue[Any | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        worker_task: asyncio.Task | None = None

        def run_adapter_stream() -> None:
            def chunk_iter():
                while True:
                    item = input_queue.get()
                    if item is None:
                        break
                    yield item

            try:
                for result in self._adapter.transcribe_stream(chunk_iter()):
                    loop.call_soon_threadsafe(result_queue.put_nowait, result)
            except Exception as exc:
                loop.call_soon_threadsafe(result_queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(result_queue.put_nowait, None)

        async def emit_ready_results() -> list[sayo_pb2.StreamingRecognizeResponse]:
            ready: list[sayo_pb2.StreamingRecognizeResponse] = []
            while True:
                try:
                    result = result_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                if result is None:
                    break
                if isinstance(result, Exception):
                    raise result

                transcript = getattr(result, "transcript", "")
                is_final = getattr(result, "is_final", False)
                confidence = getattr(result, "confidence", 0.0)
                if not transcript and not is_final:
                    continue

                ready.append(
                    self._safe_response(
                        transcript=transcript,
                        is_final=is_final,
                        confidence=confidence,
                        metadata={
                            "latency_ms": f"{getattr(result, 'latency_ms', 0.0):.1f}",
                            "chunks": chunk_count,
                            "model_id": self._model_entry.model_id,
                            "stream_mode": "incremental",
                            **(getattr(result, "metadata", {}) or {}),
                        },
                    )
                )
            return ready

        try:
            async for request in request_iterator:
                if request.HasField("config"):
                    if config is not None:
                        await context.abort(
                            grpc.StatusCode.INVALID_ARGUMENT,
                            "Config must be sent only once as first message",
                        )
                        return

                    config = request.config
                    logger.info(
                        "Config received",
                        model_id=config.model_id,
                        language_code=config.language_code,
                        sample_rate_hertz=config.sample_rate_hertz,
                        interim_results=config.interim_results,
                        chunk_duration_ms=config.chunk_duration_ms,
                    )

                    if (
                        config.model_id
                        and config.model_id != self._model_entry.model_id
                    ):
                        await context.abort(
                            grpc.StatusCode.INVALID_ARGUMENT,
                            f"Unknown model_id '{config.model_id}' for this stand",
                        )
                        return
                    if (
                        config.sample_rate_hertz
                        and config.sample_rate_hertz != self._sample_rate
                    ):
                        await context.abort(
                            grpc.StatusCode.INVALID_ARGUMENT,
                            "sample_rate_hertz must match model sample rate. "
                            "Resample audio on client side before streaming.",
                        )
                        return
                    worker_task = asyncio.create_task(
                        asyncio.to_thread(run_adapter_stream)
                    )
                    continue

                if not request.HasField("audio_chunk"):
                    continue

                if config is None:
                    await context.abort(
                        grpc.StatusCode.INVALID_ARGUMENT,
                        "First streaming message must be config",
                    )
                    return

                chunk_count += 1
                input_queue.put(
                    self._chunk_to_f32(
                        request.audio_chunk,
                        config.audio_quantization,
                    )
                )
                for item in await emit_ready_results():
                    yield item

            if config is None:
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "Config was not received",
                )
                return

            input_queue.put(None)
            if worker_task is None:
                await context.abort(
                    grpc.StatusCode.INTERNAL,
                    "Adapter streaming worker was not initialized",
                )
                return

            done = False
            while not done:
                result = await result_queue.get()
                if result is None:
                    done = True
                    continue
                if isinstance(result, Exception):
                    raise result
                transcript = getattr(result, "transcript", "")
                is_final = getattr(result, "is_final", False)
                confidence = getattr(result, "confidence", 0.0)
                if not transcript and not is_final:
                    continue
                yield self._safe_response(
                    transcript=transcript,
                    is_final=is_final,
                    confidence=confidence,
                    metadata={
                        "latency_ms": f"{getattr(result, 'latency_ms', 0.0):.1f}",
                        "chunks": chunk_count,
                        "model_id": self._model_entry.model_id,
                        "stream_mode": "incremental",
                        **(getattr(result, "metadata", {}) or {}),
                    },
                )

            await worker_task

        except Exception as exc:
            logger.exception("StreamingRecognize failed", error=str(exc))
            raise
        finally:
            input_queue.put(None)
            if worker_task is not None and not worker_task.done():
                worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await worker_task


async def serve(model_name: str, device: str, port: int, models_dir: str):
    model_repo = ModelRepository.from_model_dir(models_dir, model_name)
    entry = model_repo.entry

    logger.info("Loading model", model=model_name, device=device)
    adapter = model_repo.create_adapter(device=device, auto_load=True)

    server = grpc.aio.server()
    sayo_pb2_grpc.add_SayoServiceServicer_to_server(
        StandSayoService(adapter, model_entry=entry),
        server,
    )

    listen_addr = f"[::]:{port}"
    server.add_insecure_port(listen_addr)
    logger.info("Stand server listening", address=listen_addr)

    await server.start()
    try:
        await server.wait_for_termination()
    finally:
        adapter.unload()
        logger.info("Model unloaded")


def main():
    parser = argparse.ArgumentParser(description="Sayo gRPC test stand server")
    parser.add_argument(
        "--model",
        "-m",
        default=os.environ.get("STAND_MODEL", "nemo"),
        help="Model key from models/<name>/model.yaml",
    )
    parser.add_argument(
        "--device",
        "-d",
        default=os.environ.get("STAND_DEVICE", "cpu"),
        help="Execution device (cpu/cuda/cuda:0)",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=int(os.environ.get("STAND_PORT", "50051")),
        help="gRPC listen port",
    )
    parser.add_argument(
        "--models-dir",
        default=os.environ.get("STAND_MODELS_DIR", str(ROOT / "models")),
        help="Path to models directory",
    )
    args = parser.parse_args()

    asyncio.run(serve(args.model, args.device, args.port, args.models_dir))


if __name__ == "__main__":
    main()
