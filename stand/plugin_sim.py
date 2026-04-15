"""
Plugin simulator for SayoService.

Sends config + audio chunks over StreamingRecognize and prints results.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import time

import grpc
import numpy as np
import structlog

logger = structlog.get_logger("stand.plugin_sim")


def _load_proto_modules():
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


def _resample(audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    if orig_sr == target_sr:
        return audio.astype(np.float32)
    ratio = target_sr / orig_sr
    new_len = int(len(audio) * ratio)
    indices = np.linspace(0, len(audio) - 1, new_len)
    return np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)


def load_audio_as_pcm_chunks(
    audio_path: str,
    chunk_ms: int = 100,
    sample_rate: int = 48_000,
) -> list[bytes]:
    import soundfile as sf

    audio, sr = sf.read(audio_path, dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)

    audio = _resample(audio, sr, sample_rate)

    chunk_samples = int(sample_rate * chunk_ms / 1000)
    chunks: list[bytes] = []
    for start in range(0, len(audio), chunk_samples):
        chunks.append(audio[start : start + chunk_samples].tobytes())

    logger.info(
        "Prepared audio chunks",
        audio_path=audio_path,
        duration_s=round(len(audio) / sample_rate, 2) if sample_rate else 0.0,
        chunks=len(chunks),
        chunk_ms=chunk_ms,
    )
    return chunks


def generate_silence_chunks(
    duration_s: float = 5.0,
    chunk_ms: int = 100,
    sample_rate: int = 48_000,
) -> list[bytes]:
    chunk_samples = int(sample_rate * chunk_ms / 1000)
    total_samples = int(sample_rate * duration_s)
    silence = np.zeros(total_samples, dtype=np.float32)

    chunks: list[bytes] = []
    for start in range(0, total_samples, chunk_samples):
        chunks.append(silence[start : start + chunk_samples].tobytes())

    logger.info("Prepared silence chunks", duration_s=duration_s, chunks=len(chunks))
    return chunks


async def ping_test(host: str, port: int) -> bool:
    channel = grpc.aio.insecure_channel(f"{host}:{port}")
    stub = sayo_pb2_grpc.SayoServiceStub(channel)
    try:
        response = await stub.Ping(sayo_pb2.PingRequest())
        logger.info("Ping response", message=response.message)
        return True
    except grpc.aio.AioRpcError as exc:
        logger.error("Ping failed", code=str(exc.code()), details=exc.details())
        return False
    finally:
        await channel.close()


async def run_streaming_test(
    host: str,
    port: int,
    model_id: str,
    pcm_chunks: list[bytes],
    language_code: str = "en",
    sample_rate: int = 48_000,
    send_delay_ms: int = 0,
) -> list[dict]:
    channel = grpc.aio.insecure_channel(f"{host}:{port}")
    stub = sayo_pb2_grpc.SayoServiceStub(channel)

    async def request_generator():
        yield sayo_pb2.StreamingRecognizeRequest(
            config=sayo_pb2.StreamingConfig(
                model_id=model_id,
                language_code=language_code,
                sample_rate_hertz=sample_rate,
                audio_channel_count=1,
                interim_results=True,
            )
        )

        for chunk in pcm_chunks:
            yield sayo_pb2.StreamingRecognizeRequest(audio_content=chunk)
            if send_delay_ms > 0:
                await asyncio.sleep(send_delay_ms / 1000.0)

    results: list[dict] = []
    started = time.perf_counter()
    try:
        stream = stub.StreamingRecognize(request_generator())
        async for response in stream:
            elapsed = time.perf_counter() - started
            payload = {
                "transcript": response.transcript,
                "is_final": response.is_final,
                "confidence": response.confidence,
                "metadata": dict(response.metadata),
                "wall_s": round(elapsed, 3),
            }
            results.append(payload)
            logger.info(
                "Streaming response received",
                is_final=response.is_final,
                confidence=response.confidence,
                transcript_preview=response.transcript[:80],
            )
    except grpc.aio.AioRpcError as exc:
        logger.error(
            "Streaming call failed", code=str(exc.code()), details=exc.details()
        )
    finally:
        await channel.close()

    logger.info(
        "Streaming finished",
        elapsed_s=round(time.perf_counter() - started, 3),
        responses=len(results),
    )
    return results


async def main_async(args):
    if not await ping_test(args.host, args.port):
        raise SystemExit(1)

    if args.audio:
        chunks = load_audio_as_pcm_chunks(
            args.audio,
            chunk_ms=args.chunk_ms,
            sample_rate=args.sample_rate,
        )
    else:
        chunks = generate_silence_chunks(
            duration_s=5.0,
            chunk_ms=args.chunk_ms,
            sample_rate=args.sample_rate,
        )

    results = await run_streaming_test(
        host=args.host,
        port=args.port,
        model_id=args.model,
        pcm_chunks=chunks,
        language_code=args.lang,
        sample_rate=args.sample_rate,
        send_delay_ms=args.send_delay_ms,
    )

    final_count = sum(1 for row in results if row["is_final"])
    last_final = next((row for row in reversed(results) if row["is_final"]), None)

    print("\n=== Plugin Sim Summary ===")
    print(f"Server:         {args.host}:{args.port}")
    print(f"Model ID:       {args.model}")
    print(f"Responses:      {len(results)}")
    print(f"Final results:  {final_count}")
    if last_final:
        print(f"Last transcript:{last_final['transcript'][:120]}")

    return results


def main():
    parser = argparse.ArgumentParser(description="Sayo plugin simulator client")
    parser.add_argument("--host", default=os.environ.get("STAND_HOST", "localhost"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("STAND_PORT", "50051"))
    )
    parser.add_argument(
        "--model",
        "-m",
        default=os.environ.get("STAND_MODEL", "nemo"),
        help="Model ID sent in StreamingConfig",
    )
    parser.add_argument("--audio", "-a", default=None, help="Path to source audio file")
    parser.add_argument("--lang", default="en", help="Language code")
    parser.add_argument(
        "--sample-rate", type=int, default=48_000, help="Reported input sample rate"
    )
    parser.add_argument(
        "--chunk-ms", type=int, default=100, help="Chunk size in milliseconds"
    )
    parser.add_argument(
        "--send-delay-ms", type=int, default=0, help="Delay between chunks"
    )
    args = parser.parse_args()

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
