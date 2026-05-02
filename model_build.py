#!/usr/bin/env python3
"""Cross-platform Docker image builds: base, model, or stand."""

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


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def cmd_base(args: argparse.Namespace) -> None:
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


def cmd_model(args: argparse.Namespace) -> None:
    repo_root = _repo_root()
    model_yaml = repo_root / "models" / args.model_name / "model.yaml"
    if not model_yaml.exists():
        raise FileNotFoundError(f"model.yaml not found: {model_yaml}")

    adapter_name = _read_adapter_name(model_yaml)
    model_image = args.model_image or f"sayo-model-{args.model_name}:latest"

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


def cmd_stand(args: argparse.Namespace) -> None:
    model_image = args.model_image or f"sayo-model-{args.model_name}:latest"
    stand_image = args.stand_image or f"sayo-stand-{args.model_name}:latest"

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


def _print_run_hint(model_name: str, stand_image: str) -> None:
    print("Done! Example run:")
    print(
        "docker run "
        f"-v /path/to/weights:/app/models/{model_name}/weights "
        f"-p 50051:50051 {stand_image} --model {model_name}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build sayo-base, sayo-model-<name>, or sayo-stand-<name> images."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_base = sub.add_parser("base", help="Build Docker.base only.")
    p_base.add_argument("--base-image", default="sayo-base:latest")
    p_base.set_defaults(func=cmd_base)

    p_model = sub.add_parser(
        "model", help="Build Docker.model for one model (needs base image)."
    )
    p_model.add_argument("model_name", help="Directory name under models/")
    p_model.add_argument("--base-image", default="sayo-base:latest")
    p_model.add_argument(
        "--model-image",
        default=None,
        help="Image tag (default: sayo-model-<name>:latest)",
    )
    p_model.set_defaults(func=cmd_model)

    p_stand = sub.add_parser(
        "stand",
        help="Build Docker.stand only (needs model image).",
    )
    p_stand.add_argument(
        "model_name", help="Directory name under models/ (for default tags)"
    )
    p_stand.add_argument(
        "--model-image",
        default=None,
        help="FROM image for stand (default: sayo-model-<name>:latest)",
    )
    p_stand.add_argument(
        "--stand-image",
        default=None,
        help="Output tag (default: sayo-stand-<name>:latest)",
    )
    p_stand.set_defaults(func=cmd_stand)

    args = parser.parse_args()
    args.func(args)

    if args.command == "stand":
        si = args.stand_image or f"sayo-stand-{args.model_name}:latest"
        _print_run_hint(args.model_name, si)
    elif args.command == "model":
        print(
            "Next: build stand with "
            f"'python model_build.py stand {args.model_name}' "
            "(or set --model-image if you used a custom model tag)."
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
