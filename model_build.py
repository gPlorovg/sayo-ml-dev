#!/usr/bin/env python3
"""Cross-platform image build pipeline for a single model."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _read_adapter_name(model_yaml: Path) -> str:
    for raw_line in model_yaml.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.startswith("adapter:"):
            continue
        _, value = line.split(":", 1)
        adapter = value.strip().strip('"').strip("'")
        if adapter:
            return adapter
    raise ValueError(f"failed to read 'adapter' from {model_yaml}")


def _run(command: list[str]) -> None:
    print("+", " ".join(command))
    subprocess.run(command, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build base, per-model and stand Docker images."
    )
    parser.add_argument("model_name", help="Model directory name under models/")
    parser.add_argument("--base-image", default="sayo-base:latest")
    parser.add_argument("--model-image", default=None)
    parser.add_argument("--stand-image", default=None)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent
    model_dir = repo_root / "models" / args.model_name
    model_yaml = model_dir / "model.yaml"
    if not model_yaml.exists():
        raise FileNotFoundError(f"model.yaml not found: {model_yaml}")

    adapter_name = _read_adapter_name(model_yaml)
    model_image = args.model_image or f"sayo-model-{args.model_name}:latest"
    stand_image = args.stand_image or f"sayo-stand-{args.model_name}:latest"

    print(f"=== Building Base Image ({args.base_image}) ===")
    _run(
        [
            "docker",
            "build",
            "-f",
            "Docker.base",
            "-t",
            args.base_image,
            ".",
        ]
    )

    print(f"=== Building Model Image ({model_image}) for {args.model_name} ===")
    _run(
        [
            "docker",
            "build",
            "--build-arg",
            f"BASE_IMAGE={args.base_image}",
            "--build-arg",
            f"MODEL_NAME={args.model_name}",
            "--build-arg",
            f"ADAPTER_NAME={adapter_name}",
            "-f",
            "Docker.model",
            "-t",
            model_image,
            ".",
        ]
    )

    print(f"=== Building Stand Image ({stand_image}) for {args.model_name} ===")
    _run(
        [
            "docker",
            "build",
            "--build-arg",
            f"MODEL_IMAGE={model_image}",
            "-f",
            "Docker.stand",
            "-t",
            stand_image,
            ".",
        ]
    )

    print("Done! You can now run:")
    print(
        "docker run "
        f"-v /path/to/weights:/app/models/{args.model_name}/weights "
        f"-p 50051:50051 {stand_image} --model {args.model_name}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: command failed with exit code {exc.returncode}", file=sys.stderr)
        raise SystemExit(exc.returncode)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
