"""
Plugin simulator for SayoService.

Sends config + audio chunks over StreamingRecognize and prints results.
Uses HealthCheck / ModelDescriptor for stream parameters (same contract as stand server).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import grpc
import numpy as np
import structlog

try:
    from proto import sayo_pb2, sayo_pb2_grpc
except ModuleNotFoundError:
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from proto import sayo_pb2, sayo_pb2_grpc

logger = structlog.get_logger("stand.client")


@dataclass(frozen=True)
class ModelStreamSettings:
    model_id: str
    language_code: str
    sample_rate_hertz: int
    audio_quantization: int
    chunk_duration_ms: int
    interim_results: bool


def _resample(audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    if orig_sr == target_sr:
        return audio.astype(np.float32)
    if len(audio) == 0:
        return audio.astype(np.float32)
    ratio = target_sr / orig_sr
    new_len = int(len(audio) * ratio)
    indices = np.linspace(0, len(audio) - 1, new_len)
    return np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)


def encode_wire_chunk(audio_f32: np.ndarray, quantization: int) -> bytes:
    """Encode mono float32 samples roughly in [-1, 1] to wire bytes per proto quantization."""
    clipped = np.clip(np.asarray(audio_f32, dtype=np.float32), -1.0, 1.0)
    if quantization == sayo_pb2.AUDIO_QUANTIZATION_PCM_S16LE:
        pcm16 = (clipped * 32767.0).astype(np.int16)
        return pcm16.tobytes()
    if quantization == sayo_pb2.AUDIO_QUANTIZATION_PCM_F32LE:
        return clipped.astype(np.float32, copy=False).tobytes()
    return clipped.astype(np.float32, copy=False).tobytes()


def load_audio_as_float32_mono(audio_path: str, target_sr: int) -> np.ndarray:
    import soundfile as sf

    audio, sr = sf.read(audio_path, dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    return _resample(audio.astype(np.float32), int(sr), target_sr)


def float32_to_chunks(
    audio: np.ndarray, sample_rate: int, chunk_ms: int
) -> list[np.ndarray]:
    chunk_samples = max(1, int(sample_rate * chunk_ms / 1000))
    return [audio[i : i + chunk_samples] for i in range(0, len(audio), chunk_samples)]


def generate_silence_float32(duration_s: float, sample_rate: int) -> np.ndarray:
    n = int(sample_rate * duration_s)
    return np.zeros(max(0, n), dtype=np.float32)


def capture_microphone_float32(
    duration_s: float,
    sample_rate: int,
    device: str | int | None = None,
) -> np.ndarray:
    import sounddevice as sd

    total_samples = int(sample_rate * duration_s)
    logger.info(
        "Recording from microphone",
        duration_s=duration_s,
        sample_rate=sample_rate,
        device=device,
    )
    recording = sd.rec(
        total_samples,
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        device=device,
    )
    sd.wait()
    return np.asarray(recording, dtype=np.float32).reshape(-1)


async def fetch_model_stream_settings(
    stub: sayo_pb2_grpc.SayoServiceStub,
) -> ModelStreamSettings:
    health = await stub.HealthCheck(sayo_pb2.HealthCheckRequest())
    if not health.ready:
        raise RuntimeError(f"Server not ready: {health.message}")
    if not health.models:
        raise RuntimeError("HealthCheck returned no models")

    m = health.models[0]
    q = m.audio_quantization
    if q == sayo_pb2.AUDIO_QUANTIZATION_UNSPECIFIED:
        q = sayo_pb2.AUDIO_QUANTIZATION_PCM_F32LE
    chunk_ms = m.chunk_duration_ms if m.chunk_duration_ms > 0 else 100
    return ModelStreamSettings(
        model_id=m.model_id,
        language_code=m.language_code or "en",
        sample_rate_hertz=int(m.sample_rate_hertz)
        if m.sample_rate_hertz > 0
        else 16_000,
        audio_quantization=int(q),
        chunk_duration_ms=int(chunk_ms),
        interim_results=bool(m.supports_interim_results),
    )


def streaming_config_from_settings(
    settings: ModelStreamSettings,
    vad_threshold: float = 0.5,
    vad_min_silence_ms: int = 500,
) -> sayo_pb2.StreamingConfig:
    return sayo_pb2.StreamingConfig(
        model_id=settings.model_id,
        language_code=settings.language_code,
        interim_results=settings.interim_results,
        sample_rate_hertz=settings.sample_rate_hertz,
        audio_quantization=settings.audio_quantization,
        chunk_duration_ms=settings.chunk_duration_ms,
        vad_threshold=vad_threshold,
        vad_min_silence_duration_ms=vad_min_silence_ms,
    )


async def run_streaming_test(
    host: str,
    port: int,
    *,
    pcm_chunks_f32: list[np.ndarray] | None = None,
    mic_live: bool = False,
    mic_device: str | int | None = None,
    send_delay_ms: int = 0,
    model_id_cli: str | None = None,
    settings_override: ModelStreamSettings | None = None,
) -> list[dict]:
    target = f"{host}:{port}"
    channel = grpc.aio.insecure_channel(target)
    stub = sayo_pb2_grpc.SayoServiceStub(channel)

    settings = settings_override or await fetch_model_stream_settings(stub)
    if model_id_cli and model_id_cli != settings.model_id:
        await channel.close()
        raise SystemExit(
            f"--model {model_id_cli!r} does not match server model_id {settings.model_id!r}. "
            "Omit --model or use the id from HealthCheck."
        )

    logger.info(
        "Using model stream settings from HealthCheck",
        model_id=settings.model_id,
        language_code=settings.language_code,
        sample_rate_hertz=settings.sample_rate_hertz,
        audio_quantization=settings.audio_quantization,
        chunk_duration_ms=settings.chunk_duration_ms,
        interim_results=settings.interim_results,
    )

    cfg = streaming_config_from_settings(settings)

    async def request_generator_file():
        yield sayo_pb2.StreamingRecognizeRequest(config=cfg)
        for chunk in pcm_chunks_f32 or []:
            yield sayo_pb2.StreamingRecognizeRequest(
                audio_chunk=encode_wire_chunk(chunk, settings.audio_quantization),
            )
            if send_delay_ms > 0:
                await asyncio.sleep(send_delay_ms / 1000.0)

    async def request_generator_mic_live():
        import sounddevice as sd

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=128)
        chunk_samples = max(
            1, int(settings.sample_rate_hertz * settings.chunk_duration_ms / 1000)
        )
        started = False

        def on_audio(indata, frames, time_info, status):
            del frames, time_info
            if status:
                logger.warning("Microphone stream status", status=str(status))
            mono = np.asarray(indata[:, 0], dtype=np.float32).reshape(-1)
            wire = encode_wire_chunk(mono, settings.audio_quantization)
            try:
                loop.call_soon_threadsafe(queue.put_nowait, wire)
            except asyncio.QueueFull:
                logger.warning("Microphone queue full, dropping chunk")

        stream = sd.InputStream(
            samplerate=settings.sample_rate_hertz,
            channels=1,
            dtype="float32",
            blocksize=chunk_samples,
            device=mic_device,
            callback=on_audio,
        )
        stream.start()
        print(
            "Microphone live streaming (model config): "
            f"sr={settings.sample_rate_hertz}Hz, chunk={settings.chunk_duration_ms}ms, "
            f"quantization={settings.audio_quantization}. Press Ctrl+C to stop."
        )
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if not started:
                    yield sayo_pb2.StreamingRecognizeRequest(config=cfg)
                    started = True
                yield sayo_pb2.StreamingRecognizeRequest(audio_chunk=item)
        except asyncio.CancelledError:
            raise
        finally:
            with suppress(Exception):
                stream.stop()
                stream.close()
            with suppress(asyncio.QueueFull):
                queue.put_nowait(None)

    results: list[dict] = []
    t0 = time.perf_counter()
    try:
        gen = request_generator_mic_live() if mic_live else request_generator_file()
        call = stub.StreamingRecognize(gen)
        async for response in call:
            results.append(
                {
                    "transcript": response.transcript,
                    "is_final": response.is_final,
                    "confidence": response.confidence,
                    "metadata": dict(response.metadata),
                    "wall_s": round(time.perf_counter() - t0, 3),
                }
            )
            logger.info(
                "Streaming response",
                is_final=response.is_final,
                transcript_preview=(response.transcript or "")[:80],
            )
    except grpc.aio.AioRpcError as exc:
        logger.error("Streaming failed", code=str(exc.code()), details=exc.details())
    finally:
        await channel.close()

    logger.info("Streaming finished", responses=len(results))
    return results


async def main_async(args: argparse.Namespace) -> list[dict]:
    if args.mic and args.mic_live:
        raise SystemExit("Use either --mic or --mic-live, not both.")

    if args.mic_live:
        try:
            return await run_streaming_test(
                args.host,
                args.port,
                mic_live=True,
                mic_device=args.mic_device,
                send_delay_ms=0,
                model_id_cli=args.model,
            )
        except KeyboardInterrupt:
            print("\nStopped.")
            return []

    if args.mic:
        channel = grpc.aio.insecure_channel(f"{args.host}:{args.port}")
        stub = sayo_pb2_grpc.SayoServiceStub(channel)
        try:
            settings = await fetch_model_stream_settings(stub)
        finally:
            await channel.close()

        audio = capture_microphone_float32(
            args.mic_duration_s,
            settings.sample_rate_hertz,
            device=args.mic_device,
        )
        pcm_chunks = float32_to_chunks(
            audio,
            settings.sample_rate_hertz,
            settings.chunk_duration_ms,
        )
        return await run_streaming_test(
            args.host,
            args.port,
            pcm_chunks_f32=pcm_chunks,
            mic_live=False,
            send_delay_ms=args.send_delay_ms,
            model_id_cli=args.model,
            settings_override=settings,
        )
    if args.audio:
        channel = grpc.aio.insecure_channel(f"{args.host}:{args.port}")
        stub = sayo_pb2_grpc.SayoServiceStub(channel)
        try:
            settings = await fetch_model_stream_settings(stub)
        finally:
            await channel.close()

        audio = load_audio_as_float32_mono(args.audio, settings.sample_rate_hertz)
        pcm_chunks = float32_to_chunks(
            audio,
            settings.sample_rate_hertz,
            settings.chunk_duration_ms,
        )
        return await run_streaming_test(
            args.host,
            args.port,
            pcm_chunks_f32=pcm_chunks,
            mic_live=False,
            send_delay_ms=args.send_delay_ms,
            model_id_cli=args.model,
            settings_override=settings,
        )

    channel = grpc.aio.insecure_channel(f"{args.host}:{args.port}")
    stub = sayo_pb2_grpc.SayoServiceStub(channel)
    try:
        settings = await fetch_model_stream_settings(stub)
    finally:
        await channel.close()

    audio = generate_silence_float32(5.0, settings.sample_rate_hertz)
    pcm_chunks = float32_to_chunks(
        audio,
        settings.sample_rate_hertz,
        settings.chunk_duration_ms,
    )

    return await run_streaming_test(
        args.host,
        args.port,
        pcm_chunks_f32=pcm_chunks,
        mic_live=False,
        send_delay_ms=args.send_delay_ms,
        model_id_cli=args.model,
        settings_override=settings,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Sayo plugin simulator client")
    parser.add_argument("--host", default=os.environ.get("STAND_HOST", "localhost"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("STAND_PORT", "50051"))
    )
    parser.add_argument(
        "--model",
        "-m",
        default=os.environ.get("STAND_MODEL"),
        help="Optional; must match HealthCheck model_id if set",
    )
    parser.add_argument("--audio", "-a", default=None, help="Path to source audio file")
    parser.add_argument(
        "--mic",
        action="store_true",
        help="Record from microphone for a fixed duration, using model descriptor from HealthCheck",
    )
    parser.add_argument(
        "--mic-live",
        action="store_true",
        help="Stream microphone in real time until Ctrl+C, using model descriptor from HealthCheck",
    )
    parser.add_argument(
        "--mic-duration-s",
        type=float,
        default=5.0,
        help="Microphone recording duration when using --mic",
    )
    parser.add_argument(
        "--mic-device",
        default=None,
        help="Optional microphone device index or name for sounddevice",
    )
    parser.add_argument(
        "--send-delay-ms",
        type=int,
        default=0,
        help="Delay between chunks (file/silence/mic-record)",
    )
    args = parser.parse_args()

    results = asyncio.run(main_async(args))

    final_count = sum(1 for row in results if row["is_final"])
    last_final = next((row for row in reversed(results) if row["is_final"]), None)

    print("\n=== Plugin Sim Summary ===")
    print(f"Server:         {args.host}:{args.port}")
    print(f"Responses:      {len(results)}")
    print(f"Final results:  {final_count}")
    if last_final:
        print(f"Last transcript:{last_final['transcript'][:120]}")


if __name__ == "__main__":
    main()
