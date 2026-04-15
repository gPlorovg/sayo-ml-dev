"""
gRPC test stand server.

Loads one model repository entry and exposes SayoService.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import time
from pathlib import Path

import grpc
import numpy as np
import structlog

from model_repository.model_repository import ModelRepository

logger = structlog.get_logger("stand.server")

ROOT = Path(__file__).resolve().parents[1]


# TODO: определиться с расположением скомплированных proto файлов
def _load_proto_modules():
    """Try several common generated proto package locations."""
    module_roots = [
        os.environ.get("SAYO_PROTO_PY_PACKAGE", "").strip(),
        "generated",
        "proto.generated",
        "stand.generated",
        "",
    ]

    for mod_root in module_roots:
        if not mod_root:
            candidates = ["sayo_pb2", "sayo_pb2_grpc"]
        else:
            candidates = [f"{mod_root}.sayo_pb2", f"{mod_root}.sayo_pb2_grpc"]

        try:
            pb2 = importlib.import_module(candidates[0])
            pb2_grpc = importlib.import_module(candidates[1])
            return pb2, pb2_grpc
        except ImportError:
            continue

    raise RuntimeError(
        "Could not import generated gRPC modules (sayo_pb2, sayo_pb2_grpc). "
        "Set SAYO_PROTO_PY_PACKAGE or place generated modules on PYTHONPATH."
    )


sayo_pb2, sayo_pb2_grpc = _load_proto_modules()


class StandSayoService(sayo_pb2_grpc.SayoServiceServicer):
    def __init__(self, adapter, sample_rate: int = 16_000):
        self._adapter = adapter
        self._sample_rate = sample_rate

    # TODO: заменить на health check
    async def Ping(self, request, context):
        return sayo_pb2.PingResponse(message="pong")

    async def StreamingRecognize(self, request_iterator, context):
        config = None
        audio_buffer = bytearray()
        chunk_count = 0

        try:
            async for request in request_iterator:
                if request.HasField("config"):
                    config = request.config
                    logger.info(
                        "Config received",
                        model_id=config.model_id,
                        language_code=config.language_code,
                        sample_rate_hertz=config.sample_rate_hertz,
                        dry_run=config.dry_run,
                    )
                    continue

                if not request.HasField("audio_content"):
                    continue

                if config is None:
                    await context.abort(
                        grpc.StatusCode.INVALID_ARGUMENT,
                        "First streaming message must be config",
                    )
                    return

                chunk_count += 1
                # TODO: убрать dry_run
                if config.dry_run:
                    yield sayo_pb2.StreamingRecognizeResponse(
                        transcript="",
                        is_final=False,
                        confidence=0.0,
                        metadata={"mode": "dry_run", "chunks": str(chunk_count)},
                    )
                    continue

                audio_buffer.extend(request.audio_content)

                # Expect float32 mono chunks; process each ~1 second.
                samples_in_buffer = len(audio_buffer) // 4
                if samples_in_buffer < self._sample_rate:
                    continue

                audio_np = np.frombuffer(bytes(audio_buffer), dtype=np.float32)
                audio_buffer.clear()

                t0 = time.perf_counter()
                result = self._adapter.transcribe(audio_np)
                elapsed = time.perf_counter() - t0

                yield sayo_pb2.StreamingRecognizeResponse(
                    transcript=result.transcript,
                    is_final=False,
                    confidence=result.confidence,
                    metadata={
                        "latency_ms": f"{elapsed * 1000:.1f}",
                        "chunks": str(chunk_count),
                    },
                )
            # TODO: VAD будет изменять размер чанков для транскрибации
            if config is not None and not config.dry_run and audio_buffer:
                audio_np = np.frombuffer(bytes(audio_buffer), dtype=np.float32)
                t0 = time.perf_counter()
                result = self._adapter.transcribe(audio_np)
                elapsed = time.perf_counter() - t0
                yield sayo_pb2.StreamingRecognizeResponse(
                    transcript=result.transcript,
                    is_final=True,
                    confidence=result.confidence,
                    metadata={
                        "latency_ms": f"{elapsed * 1000:.1f}",
                        "total_chunks": str(chunk_count),
                    },
                )
            else:
                yield sayo_pb2.StreamingRecognizeResponse(
                    transcript="",
                    is_final=True,
                    confidence=0.0,
                    metadata={"total_chunks": str(chunk_count)},
                )

        except Exception as exc:
            logger.exception("StreamingRecognize failed", error=str(exc))
            raise


async def serve(model_name: str, device: str, port: int, models_dir: str):
    model_repo = ModelRepository.from_model_dir(models_dir, model_name)

    logger.info("Loading model", model=model_name, device=device)
    adapter = model_repo.create_adapter(device=device, auto_load=True)

    server = grpc.aio.server()
    sayo_pb2_grpc.add_SayoServiceServicer_to_server(
        StandSayoService(adapter, sample_rate=model_repo.entry.sample_rate),
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
