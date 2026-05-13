"""Sayo gRPC test client: HealthCheck → descriptor → stream. Mic uses the same pattern as OBS ``ASRGrpcClient`` (config ``Write``, then concurrent audio ``Write`` + response ``Read``)."""

# ruff: noqa: E402 — repo root is inserted into sys.path before `proto` (see block below).

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import argparse
import asyncio
import os
import time
from contextlib import suppress
from dataclasses import dataclass

import grpc
import grpc.aio
import numpy as np
import structlog

from proto import sayo_pb2, sayo_pb2_grpc

logger = structlog.get_logger("stand.client")

_CONNECTION_STATUS_LABELS_RU: dict[str, str] = {
    "config_accepted": "Проверка конфига…",
    "allocating_session": "Выделение сессии…",
    "actor_reserved": "Резервирование актора…",
    "session_opening": "Подключение к модели…",
    "connected": "Готово",
    "error": "Ошибка",
}


def _connection_status_from_metadata(meta: dict[str, str]) -> str | None:
    raw = meta.get("connection_status")
    return raw if raw else None


def _print_connection_status(meta: dict[str, str]) -> bool:
    """Return True if this message is a connection-status update."""
    status = _connection_status_from_metadata(meta)
    if not status:
        return False
    label = _CONNECTION_STATUS_LABELS_RU.get(status, status)
    session_id = meta.get("session_id", "")
    model_id = meta.get("model_id", "")
    actor_name = meta.get("actor_name", "")
    detail = meta.get("detail", "")
    suffix = ""
    if session_id:
        suffix += f" session_id={session_id}"
    if model_id:
        suffix += f" model_id={model_id}"
    if actor_name:
        suffix += f" actor={actor_name}"
    if detail:
        suffix += f" detail={detail}"
    print(f"{label}{suffix}")
    return True


def _log_streaming_result_if_new(
    last_sig: list[tuple[str, bool] | None],
    *,
    transcript: str,
    is_final: bool,
) -> None:
    """Log a streaming response only when it differs from the previous one (same server payload is often repeated)."""
    if not transcript:
        return
    sig = (transcript, bool(is_final))
    if last_sig[0] == sig:
        return
    last_sig[0] = sig
    logger.info("result", is_final=sig[1], text=transcript[:200])


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
    clipped = np.clip(np.asarray(audio_f32, dtype=np.float32), -1.0, 1.0)
    if quantization == sayo_pb2.AUDIO_QUANTIZATION_PCM_S16LE:
        return (clipped * 32767.0).astype(np.int16).tobytes()
    if quantization == sayo_pb2.AUDIO_QUANTIZATION_PCM_F32LE:
        return clipped.astype(np.float32, copy=False).tobytes()
    raise ValueError(f"Unsupported audio_quantization: {quantization}")


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


def descriptor_to_stream_settings(d: sayo_pb2.ModelDescriptor) -> ModelStreamSettings:
    if not d.model_id:
        raise RuntimeError("HealthCheck: ModelDescriptor.model_id is empty")
    if d.sample_rate_hertz <= 0:
        raise RuntimeError("HealthCheck: ModelDescriptor.sample_rate_hertz must be > 0")
    if d.chunk_duration_ms <= 0:
        raise RuntimeError("HealthCheck: ModelDescriptor.chunk_duration_ms must be > 0")
    if d.audio_quantization == sayo_pb2.AUDIO_QUANTIZATION_UNSPECIFIED:
        raise RuntimeError(
            "HealthCheck: ModelDescriptor.audio_quantization must not be UNSPECIFIED"
        )

    return ModelStreamSettings(
        model_id=d.model_id,
        language_code=d.language_code,
        sample_rate_hertz=int(d.sample_rate_hertz),
        audio_quantization=int(d.audio_quantization),
        chunk_duration_ms=int(d.chunk_duration_ms),
        interim_results=bool(d.supports_interim_results),
    )


async def fetch_model_stream_settings(
    stub: sayo_pb2_grpc.SayoServiceStub,
) -> ModelStreamSettings:
    health = await stub.HealthCheck(sayo_pb2.HealthCheckRequest())
    if not health.ready:
        raise RuntimeError(f"Server not ready: {health.message}")
    if not health.models:
        raise RuntimeError("HealthCheck returned no models")
    return descriptor_to_stream_settings(health.models[0])


def streaming_config_proto(settings: ModelStreamSettings) -> sayo_pb2.StreamingConfig:
    return sayo_pb2.StreamingConfig(
        model_id=settings.model_id,
        language_code=settings.language_code,
        interim_results=settings.interim_results,
        sample_rate_hertz=settings.sample_rate_hertz,
        audio_quantization=settings.audio_quantization,
        chunk_duration_ms=settings.chunk_duration_ms,
        vad_threshold=0.5,
        vad_min_silence_duration_ms=500,
    )


def _request_config(
    cfg: sayo_pb2.StreamingConfig,
) -> sayo_pb2.StreamingRecognizeRequest:
    r = sayo_pb2.StreamingRecognizeRequest()
    r.config.CopyFrom(cfg)
    return r


def _request_audio(wire: bytes) -> sayo_pb2.StreamingRecognizeRequest:
    r = sayo_pb2.StreamingRecognizeRequest()
    r.audio_chunk = wire
    return r


def _input_device_candidates(
    sd: object, mic_device: str | int | None
) -> list[str | int]:
    """Pick input devices to try: explicit; else default; else ranked (mic-like before line-in)."""
    if mic_device is not None:
        return [mic_device]

    seen: set[int] = set()
    out: list[str | int] = []

    default_in, _ = sd.default.device
    if default_in is not None and int(default_in) >= 0:
        d = int(default_in)
        out.append(d)
        seen.add(d)

    ranked: list[tuple[int, int]] = []
    for i, info in enumerate(sd.query_devices()):
        if int(info.get("max_input_channels", 0)) <= 0:
            continue
        idx = int(i)
        if idx in seen:
            continue
        name = str(info.get("name", "")).lower()
        if any(
            x in name
            for x in (
                "line in",
                "stereo mix",
                "wave out",
                "loopback",
                "what u hear",
            )
        ):
            rank = 80
        elif any(
            x in name
            for x in (
                "microphone",
                " mic",
                "(mic",
                "headset",
                "array",
                "usb",
                "webcam",
                "capture",
            )
        ):
            rank = 0
        else:
            rank = 40
        ranked.append((rank, idx))

    ranked.sort(key=lambda t: (t[0], t[1]))
    for _, idx in ranked:
        out.append(idx)

    if not out:
        raise RuntimeError(
            "No input audio device found (default input unset). "
            "Enable a microphone in Windows or pass --mic-device. "
            'List: python -c "import sounddevice as sd; print(sd.query_devices())"'
        )
    return out


def _device_capture_rates_hz(info: dict) -> list[int]:
    """Sample rates to try when opening capture (device default first, then common values)."""
    rates: list[int] = []
    raw = info.get("default_samplerate")
    if raw is not None:
        try:
            v = int(round(float(raw)))
            if v > 0:
                rates.append(v)
        except (TypeError, ValueError):
            pass
    for r in (48_000, 44_100, 96_000, 32_000, 24_000, 16_000, 8_000):
        if r not in rates:
            rates.append(r)
    return rates


async def _mic_streaming_readwrite(
    stub: sayo_pb2_grpc.SayoServiceStub,
    settings: ModelStreamSettings,
    cfg: sayo_pb2.StreamingConfig,
    mic_device: str | int | None,
) -> list[dict]:
    """Same layout as OBS ASRGrpcClient: Write(config), then concurrent Write(audio) + Read()."""
    import sounddevice as sd

    call = stub.StreamingRecognize()
    await call.wait_for_connection()
    await call.write(_request_config(cfg))

    loop = asyncio.get_running_loop()
    q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=256)
    chunk_ms = settings.chunk_duration_ms
    chunk_model = max(1, int(settings.sample_rate_hertz * chunk_ms / 1000))
    model_sr = settings.sample_rate_hertz

    stream = None
    last_audio_err: BaseException | None = None
    picked_sr: int | None = None
    picked_label = ""

    for device in _input_device_candidates(sd, mic_device):
        try:
            info = sd.query_devices(device)
        except sd.PortAudioError as exc:
            last_audio_err = exc
            logger.warning("input_device_query_failed", device=device, error=str(exc))
            continue

        for device_sr in _device_capture_rates_hz(info):
            block_dev = max(128, int(device_sr * chunk_ms / 1000))
            state: dict[str, np.ndarray] = {"buf": np.zeros(0, dtype=np.float32)}

            def on_audio(
                indata,
                frames,
                _time_info,
                status,
                *,
                st: dict[str, np.ndarray] = state,
                dsr: int = device_sr,
                msr: int = model_sr,
                cm: int = chunk_model,
            ):
                if status:
                    logger.warning("Microphone status", status=str(status))
                mono_dev = np.asarray(indata[:, 0], dtype=np.float32).reshape(-1)
                mono_model = _resample(mono_dev, dsr, msr)
                st["buf"] = np.concatenate([st["buf"], mono_model])
                while len(st["buf"]) >= cm:
                    piece = st["buf"][:cm].copy()
                    st["buf"] = st["buf"][cm:]
                    wire = encode_wire_chunk(piece, settings.audio_quantization)
                    try:
                        loop.call_soon_threadsafe(q.put_nowait, wire)
                    except asyncio.QueueFull:
                        logger.warning("Microphone queue full, dropping chunk")

            try:
                stream = sd.InputStream(
                    device=device,
                    samplerate=device_sr,
                    channels=1,
                    dtype="float32",
                    blocksize=block_dev,
                    callback=on_audio,
                )
                picked_sr = device_sr
                dev_index = info.get("index", device)
                dev_name = str(info.get("name", ""))
                picked_label = f"[{dev_index}] {dev_name}"
                logger.info(
                    "input_device",
                    device_index=dev_index,
                    device_name=dev_name,
                    device_samplerate=device_sr,
                    blocksize=block_dev,
                    model_samplerate=model_sr,
                    chunk_samples_model=chunk_model,
                )
                break
            except sd.PortAudioError as exc:
                last_audio_err = exc
                logger.warning(
                    "input_device_open_failed",
                    device=device,
                    device_sr=device_sr,
                    blocksize=block_dev,
                    error=str(exc),
                )
        if stream is not None:
            break

    if stream is None:
        raise RuntimeError(
            "Could not open any input device for capture. "
            f"Tried model rate {model_sr} Hz and common device rates. Last error: {last_audio_err}. "
            "Try --mic-device with an index or name from "
            '`python -c "import sounddevice as sd; print(sd.query_devices())"`'
        ) from last_audio_err

    try:
        stream.start()
    except Exception as exc:
        with suppress(Exception):
            call.cancel()
        raise RuntimeError(f"Microphone failed to start: {exc}") from exc
    print(
        f"Input device: {picked_label}\n"
        f"Mic capture {picked_sr} Hz → model {model_sr} Hz, chunk {chunk_ms} ms — Ctrl+C to stop."
    )

    results: list[dict] = []
    t0 = time.perf_counter()
    last_result_sig: list[tuple[str, bool] | None] = [None]

    async def sender() -> None:
        try:
            while True:
                wire = await q.get()
                if wire is None:
                    break
                await call.write(_request_audio(wire))
        finally:
            with suppress(Exception):
                stream.stop()
                stream.close()
            with suppress(Exception):
                await call.done_writing()

    async def receiver() -> None:
        while True:
            msg = await call.read()
            if msg is grpc.aio.EOF:
                await q.put(None)
                break
            meta = dict(msg.metadata)
            if _print_connection_status(meta):
                continue
            results.append(
                {
                    "transcript": msg.transcript,
                    "is_final": msg.is_final,
                    "confidence": msg.confidence,
                    "metadata": meta,
                    "wall_s": round(time.perf_counter() - t0, 3),
                }
            )
            _log_streaming_result_if_new(
                last_result_sig,
                transcript=msg.transcript,
                is_final=msg.is_final,
            )

    try:
        await asyncio.gather(sender(), receiver())
    except asyncio.CancelledError:
        with suppress(Exception):
            call.cancel()
        with suppress(asyncio.QueueFull):
            q.put_nowait(None)
        print("\nStopped.")
    except grpc.aio.AioRpcError as exc:
        logger.error(
            "StreamingRecognize failed", code=str(exc.code()), details=exc.details()
        )
        with suppress(Exception):
            call.cancel()
        with suppress(asyncio.QueueFull):
            q.put_nowait(None)
    except Exception:
        with suppress(Exception):
            call.cancel()
        with suppress(asyncio.QueueFull):
            q.put_nowait(None)
        raise

    logger.info("done", responses=len(results))
    return results


async def streaming_recognize(
    stub: sayo_pb2_grpc.SayoServiceStub,
    settings: ModelStreamSettings,
    *,
    pcm_chunks_f32: list[np.ndarray] | None = None,
    mic_live: bool = False,
    mic_device: str | int | None = None,
    send_delay_ms: int = 0,
    model_id_filter: str | None = None,
) -> list[dict]:
    if model_id_filter and model_id_filter != settings.model_id:
        raise SystemExit(
            f"--model {model_id_filter!r} does not match HealthCheck model_id {settings.model_id!r}. "
            "Omit --model or pass the id from the descriptor."
        )

    logger.info(
        "StreamingRecognize",
        model_id=settings.model_id,
        sample_rate_hertz=settings.sample_rate_hertz,
        audio_quantization=settings.audio_quantization,
        chunk_duration_ms=settings.chunk_duration_ms,
    )

    cfg = streaming_config_proto(settings)

    async def request_file():
        yield _request_config(cfg)
        for chunk in pcm_chunks_f32 or []:
            yield _request_audio(encode_wire_chunk(chunk, settings.audio_quantization))
            if send_delay_ms > 0:
                await asyncio.sleep(send_delay_ms / 1000.0)

    if mic_live:
        return await _mic_streaming_readwrite(stub, settings, cfg, mic_device)

    results: list[dict] = []
    t0 = time.perf_counter()
    last_result_sig: list[tuple[str, bool] | None] = [None]
    gen = request_file()
    connected = False
    try:
        async for response in stub.StreamingRecognize(gen):
            meta = dict(response.metadata)
            if _print_connection_status(meta):
                if meta.get("connection_status") == "connected":
                    connected = True
                # Do not treat status-stream updates as recognition results.
                continue
            results.append(
                {
                    "transcript": response.transcript,
                    "is_final": response.is_final,
                    "confidence": response.confidence,
                    "metadata": meta,
                    "wall_s": round(time.perf_counter() - t0, 3),
                }
            )
            _log_streaming_result_if_new(
                last_result_sig,
                transcript=response.transcript,
                is_final=response.is_final,
            )
    except grpc.aio.AioRpcError as exc:
        logger.error(
            "StreamingRecognize failed", code=str(exc.code()), details=exc.details()
        )
    finally:
        if not mic_live and not connected:
            logger.warning(
                "stream_never_connected",
                hint="Server did not emit connection_status=connected before stream ended.",
            )

    logger.info("done", responses=len(results))
    return results


async def main_async(args: argparse.Namespace) -> list[dict]:
    target = f"{args.host}:{args.port}"
    channel = grpc.aio.insecure_channel(target)
    try:
        stub = sayo_pb2_grpc.SayoServiceStub(channel)
        settings = await fetch_model_stream_settings(stub)

        if args.mic:
            try:
                return await streaming_recognize(
                    stub,
                    settings,
                    mic_live=True,
                    mic_device=args.mic_device,
                    model_id_filter=args.model,
                )
            except KeyboardInterrupt:
                print("\nStopped.")
                return []

        if args.audio:
            audio = load_audio_as_float32_mono(args.audio, settings.sample_rate_hertz)
            pcm_chunks = float32_to_chunks(
                audio,
                settings.sample_rate_hertz,
                settings.chunk_duration_ms,
            )
            return await streaming_recognize(
                stub,
                settings,
                pcm_chunks_f32=pcm_chunks,
                send_delay_ms=args.send_delay_ms,
                model_id_filter=args.model,
            )

        silence = generate_silence_float32(5.0, settings.sample_rate_hertz)
        pcm_chunks = float32_to_chunks(
            silence,
            settings.sample_rate_hertz,
            settings.chunk_duration_ms,
        )
        return await streaming_recognize(
            stub,
            settings,
            pcm_chunks_f32=pcm_chunks,
            send_delay_ms=args.send_delay_ms,
            model_id_filter=args.model,
        )
    finally:
        await channel.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Sayo gRPC test client")
    parser.add_argument("--host", default=os.environ.get("STAND_HOST", "localhost"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("STAND_PORT", "50051"))
    )
    parser.add_argument(
        "--model",
        "-m",
        default=os.environ.get("STAND_MODEL"),
        help="If set, must equal ModelDescriptor.model_id from HealthCheck",
    )
    parser.add_argument("--audio", "-a", default=None, help="WAV/audio file path")
    parser.add_argument(
        "--mic",
        action="store_true",
        help="Stream microphone until Ctrl+C",
    )
    parser.add_argument(
        "--mic-device",
        default=None,
        help="PortAudio input: device index or name substring (needed if default input is unset)",
    )
    parser.add_argument(
        "--send-delay-ms",
        type=int,
        default=0,
        help="Delay between file/silence chunks (ms)",
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
