"""
Local STT bench runner.

This utility runs offline or simulated streaming inference using the
per-model repository layout under models/<name>/model.yaml.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import structlog

from model_repository.model_repository import ModelRepository

logger = structlog.get_logger("stand.runner")

ROOT = Path(__file__).resolve().parents[1]


def _resample(audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    if orig_sr == target_sr:
        return audio.astype(np.float32)
    ratio = target_sr / orig_sr
    new_len = int(len(audio) * ratio)
    indices = np.linspace(0, len(audio) - 1, new_len)
    return np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)


def load_audio(path: str, target_sr: int) -> np.ndarray:
    """Load an audio file and convert to mono float32 at target_sr."""
    try:
        import soundfile as sf

        audio, sr = sf.read(path, dtype="float32", always_2d=False)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        return _resample(audio, sr, target_sr)
    except ImportError:
        pass

    try:
        import torchaudio

        waveform, sr = torchaudio.load(path)
        if waveform.shape[0] > 1:
            waveform = waveform.mean(dim=0, keepdim=True)
        audio = waveform.squeeze().numpy()
        return _resample(audio, sr, target_sr)
    except ImportError:
        pass

    from scipy.io import wavfile

    sr, audio = wavfile.read(path)
    audio = audio.astype(np.float32)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if audio.size and audio.max() > 1.0:
        audio /= 32768.0
    return _resample(audio, sr, target_sr)


def chunk_audio(audio: np.ndarray, chunk_ms: int, sample_rate: int):
    chunk_samples = int(sample_rate * chunk_ms / 1000)
    for start in range(0, len(audio), chunk_samples):
        yield audio[start : start + chunk_samples]


def print_results_table(results: list[dict]):
    if not results:
        print("(no results)")
        return

    headers = list(results[0].keys())
    widths = {h: len(h) for h in headers}
    for row in results:
        for h in headers:
            widths[h] = max(widths[h], len(str(row.get(h, ""))))

    print("\n" + " | ".join(h.ljust(widths[h]) for h in headers))
    print("-+-".join("-" * widths[h] for h in headers))
    for row in results:
        print(" | ".join(str(row.get(h, "")).ljust(widths[h]) for h in headers))
    print()


def _list_models(models_dir: str) -> None:
    root = Path(models_dir)
    names = (
        sorted(
            d.name for d in root.iterdir() if d.is_dir() and (d / "model.yaml").exists()
        )
        if root.exists()
        else []
    )
    if not names:
        print("No models discovered.")
        return

    print("Discovered models:")
    for name in names:
        entry = ModelRepository.from_model_dir(models_dir, name).entry
        print(
            f"  - {name} (adapter={entry.adapter}, lang={entry.language_code}, rate={entry.sample_rate})"
        )


def main():
    parser = argparse.ArgumentParser(
        description="Run local benchmark against model repository"
    )
    parser.add_argument(
        "--models-dir",
        default=str(ROOT / "models"),
        help="Path to models directory",
    )
    parser.add_argument(
        "--model",
        "-m",
        default="nemo",
        help="Model name from models/<name>/model.yaml",
    )
    parser.add_argument(
        "--audio",
        "-a",
        default=None,
        help="Path to audio file. If omitted, silence is generated.",
    )
    parser.add_argument(
        "--model-id",
        default=None,
        help="Override model_id from model.yaml for this run",
    )
    parser.add_argument(
        "--lang",
        default=None,
        help="Optional language override passed in extra config",
    )
    parser.add_argument(
        "--device",
        default="cpu",
        help="Execution device (cpu/cuda/cuda:0)",
    )
    parser.add_argument(
        "--streaming",
        "-s",
        action="store_true",
        help="Run chunked streaming simulation",
    )
    parser.add_argument(
        "--chunk-ms",
        type=int,
        default=480,
        help="Chunk size for streaming mode in milliseconds",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print structured JSON output",
    )
    parser.add_argument(
        "--list",
        "-l",
        action="store_true",
        help="List models and exit",
    )
    args = parser.parse_args()

    if args.list:
        _list_models(args.models_dir)
        return

    model_repo = ModelRepository.from_model_dir(args.models_dir, args.model)
    entry = model_repo.entry

    sample_rate = entry.sample_rate
    if args.audio:
        logger.info("Loading audio", path=args.audio)
        audio = load_audio(args.audio, target_sr=sample_rate)
    else:
        logger.info("No audio given, generating silence", duration_s=5)
        audio = np.zeros(sample_rate * 5, dtype=np.float32)

    duration_s = len(audio) / sample_rate if sample_rate > 0 else 0.0
    logger.info(
        "Audio loaded",
        duration_s=round(duration_s, 2),
        samples=len(audio),
        sample_rate=sample_rate,
    )

    extra_overrides = {}
    if args.lang:
        extra_overrides["language_code_override"] = args.lang
    extra_overrides["chunk_size_ms"] = args.chunk_ms

    logger.info("Loading model", model=entry.name, adapter=entry.adapter)
    t_load = time.perf_counter()
    adapter = model_repo.create_adapter(
        device=args.device,
        auto_load=True,
        model_id=args.model_id,
        extra_overrides=extra_overrides,
    )
    load_sec = time.perf_counter() - t_load

    results: list[dict] = []
    if args.streaming:
        logger.info("Running streaming simulation", chunk_ms=args.chunk_ms)
        t0 = time.perf_counter()
        for result in adapter.transcribe_stream(
            chunk_audio(audio, args.chunk_ms, sample_rate)
        ):
            results.append(
                {
                    "transcript": result.transcript[:80],
                    "is_final": result.is_final,
                    "latency_ms": f"{result.latency_ms:.1f}",
                    "wall_ms": f"{(time.perf_counter() - t0) * 1000:.0f}",
                }
            )
    else:
        logger.info("Running offline transcription")
        result = adapter.transcribe_timed(audio)
        results.append(
            {
                "transcript": result.transcript[:120],
                "is_final": result.is_final,
                "latency_ms": f"{result.latency_ms:.1f}",
                "rtf": result.metadata.get("rtf", "n/a"),
            }
        )

    summary = {
        "model": entry.name,
        "adapter": entry.adapter,
        "model_id": args.model_id or entry.model_id,
        "sample_rate": sample_rate,
        "audio_sec": round(duration_s, 2),
        "load_sec": round(load_sec, 3),
        "mode": "streaming" if args.streaming else "offline",
    }

    if args.json:
        print(
            json.dumps(
                {"summary": summary, "results": results}, indent=2, ensure_ascii=False
            )
        )
    else:
        print("\n=== Summary ===")
        for key, value in summary.items():
            print(f"  {key:12s}: {value}")
        print_results_table(results)

    adapter.unload()


if __name__ == "__main__":
    main()
