from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import sounddevice as sd
import soundfile as sf


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record microphone audio to WAV file")
    parser.add_argument(
        "--duration", type=float, default=5.0, help="Duration in seconds"
    )
    parser.add_argument(
        "--sample-rate", type=int, default=44100, help="Sample rate in Hz"
    )
    parser.add_argument("--channels", type=int, default=1, help="Number of channels")
    parser.add_argument("--device", default=None, help="Input device name or index")
    parser.add_argument(
        "--output-dir",
        default="test_audio",
        help="Directory where WAV files will be saved",
    )
    parser.add_argument(
        "--filename",
        default=None,
        help="Output file name (default: record_YYYYmmdd_HHMMSS.wav)",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    filename = args.filename or f"record_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
    output_path = output_dir / filename

    frames = int(args.duration * args.sample_rate)
    print(
        f"Recording {args.duration:.2f}s, {args.sample_rate}Hz, channels={args.channels}..."
    )
    recording = sd.rec(
        frames,
        samplerate=args.sample_rate,
        channels=args.channels,
        dtype="float32",
        device=args.device,
    )
    sd.wait()

    # Save valid WAV (PCM16), compatible with the gRPC client simulator.
    sf.write(
        str(output_path), recording, args.sample_rate, format="WAV", subtype="PCM_16"
    )
    print(f"Saved: {output_path.resolve()}")


if __name__ == "__main__":
    main()
