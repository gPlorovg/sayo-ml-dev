#!/usr/bin/env python3
"""Cross-platform Docker image builds: base, model, stand, push-model, pull-model."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _read_adapter_name_from_lines(lines: list[str]) -> str:
    for raw_line in lines:
        line = raw_line.strip()
        if not line.startswith("adapter:"):
            continue
        _, value = line.split(":", 1)
        adapter = value.strip().strip('"').strip("'")
        if adapter:
            return adapter
    raise ValueError("failed to read 'adapter' from model.yaml content")


def _read_adapter_name(model_yaml: Path) -> str:
    try:
        return _read_adapter_name_from_lines(
            model_yaml.read_text(encoding="utf-8").splitlines()
        )
    except ValueError as exc:
        raise ValueError(f"failed to read 'adapter' from {model_yaml}") from exc


def _run(command: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(command))
    subprocess.run(command, check=True, cwd=cwd)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def _run_capture(command: list[str], *, cwd: Path | None = None) -> str:
    print("+", " ".join(command))
    proc = subprocess.run(
        command,
        check=True,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def _infer_single_model_name_in_image(image_ref: str) -> str:
    """Return the sole directory name under /app/models in the image."""
    out = _run_capture(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            image_ref,
            "-c",
            "ls -1 /app/models",
        ],
    )
    names = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if len(names) != 1:
        raise ValueError(
            "expected exactly one directory under /app/models in the image; "
            f"got {names!r}. Pass --as <name> explicitly."
        )
    return names[0]


def _model_yaml_from_image(image_ref: str, model_name: str) -> str:
    return _run_capture(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "cat",
            image_ref,
            f"/app/models/{model_name}/model.yaml",
        ],
    )


def _require_local_image(image_ref: str) -> None:
    """Fail fast with a clear message if the ref does not resolve locally."""
    proc = subprocess.run(
        ["docker", "image", "inspect", image_ref],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise FileNotFoundError(
            f"No local Docker image {image_ref!r}. "
            "Use an explicit tag (repo/name:tag), a digest (repo/name@sha256:...), "
            "or the image ID from `docker images`. Untagged images must be referenced "
            "by ID or digest, or tagged first, e.g. "
            "`docker tag <image_id> yvdik/sayo-model-gigaam:latest`."
        )


def cmd_push_model(args: argparse.Namespace) -> None:
    src = args.from_image or f"sayo-model-{args.model_name}:latest"
    print(f"=== docker tag {src} -> {args.to} ===")
    _run(["docker", "tag", src, args.to])
    print(f"=== docker push {args.to} ===")
    _run(["docker", "push", args.to])


def cmd_pull_model(args: argparse.Namespace) -> None:
    repo_root = _repo_root()
    image_ref = args.from_ref.strip()
    if not args.no_pull:
        print(f"=== docker pull {image_ref} ===")
        _run(["docker", "pull", image_ref])
    else:
        print(f"=== skip docker pull (--no-pull); using local image {image_ref} ===")
        _require_local_image(image_ref)

    model_name = (args.as_name or _infer_single_model_name_in_image(image_ref)).strip()
    if not model_name:
        raise ValueError("model name is empty")

    local_tag = (args.local_tag or f"sayo-model-{model_name}:latest").strip()
    if not args.no_retag:
        print(f"=== docker tag {image_ref} -> {local_tag} (local dev tag) ===")
        _run(["docker", "tag", image_ref, local_tag])

    yaml_text = _model_yaml_from_image(image_ref, model_name)
    adapter_name = _read_adapter_name_from_lines(yaml_text.splitlines())

    dest_model = repo_root / "models" / model_name
    dest_adapter = repo_root / "model_repository" / "adapters" / f"{adapter_name}.py"

    if dest_model.exists() and not args.overwrite:
        raise FileExistsError(
            f"refuse to overwrite existing model dir (use --overwrite): {dest_model}"
        )
    if dest_adapter.exists() and not args.overwrite:
        raise FileExistsError(
            f"refuse to overwrite existing adapter (use --overwrite): {dest_adapter}"
        )

    if args.overwrite:
        if dest_model.is_dir():
            shutil.rmtree(dest_model)
        elif dest_model.exists():
            dest_model.unlink()
        if dest_adapter.exists():
            dest_adapter.unlink()

    dest_model.parent.mkdir(parents=True, exist_ok=True)
    (repo_root / "model_repository" / "adapters").mkdir(parents=True, exist_ok=True)

    ctr = f"sayo-model-pull-{os.getpid()}"
    try:
        print(f"=== docker create --name {ctr} (extract files) ===")
        _run(["docker", "create", "--name", ctr, image_ref])
        print(f"=== docker cp {ctr}:/app/models/{model_name} -> models/ ===")
        _run(
            [
                "docker",
                "cp",
                f"{ctr}:/app/models/{model_name}",
                str(repo_root / "models"),
            ],
        )
        print(
            f"=== docker cp adapter -> model_repository/adapters/{adapter_name}.py ==="
        )
        _run(
            [
                "docker",
                "cp",
                f"{ctr}:/app/model_repository/adapters/{adapter_name}.py",
                str(dest_adapter),
            ],
        )
    finally:
        subprocess.run(["docker", "rm", "-f", ctr], check=False)

    print(f"Extracted model to {dest_model}")
    print(f"Extracted adapter to {dest_adapter}")
    if not args.no_retag:
        print(f"Local image tag for builds: {local_tag}")
    print(
        f"Next: edit files, then e.g. python model_build.py stand {model_name}"
        + ("" if not args.no_retag else f" --model-image {image_ref}")
    )


def cmd_base(args: argparse.Namespace) -> None:
    repo_root = _repo_root()
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
        ],
        cwd=repo_root,
    )


def cmd_model(args: argparse.Namespace) -> None:
    repo_root = _repo_root()
    model_dir = repo_root / "models" / args.model_name
    model_yaml = model_dir / "model.yaml"
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
        ],
        cwd=repo_root,
    )


def cmd_stand(args: argparse.Namespace) -> None:
    repo_root = _repo_root()
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
        ],
        cwd=repo_root,
    )


def _print_run_hint(model_name: str, stand_image: str) -> None:
    print("Done! Example run:")
    print(f"docker run -p 50051:50051 {stand_image} --model {model_name}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build/push/pull sayo-base, sayo-model-<name>, or sayo-stand-<name> Docker images."
        )
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

    p_push = sub.add_parser(
        "push-model",
        help="Tag a local sayo-model-<name> image and push it to a registry.",
    )
    p_push.add_argument(
        "model_name",
        help="Directory name under models/ (used for default local tag sayo-model-<name>:latest)",
    )
    p_push.add_argument(
        "--from",
        dest="from_image",
        default=None,
        help="Local image to push (default: sayo-model-<name>:latest)",
    )
    p_push.add_argument(
        "--to",
        required=True,
        help="Remote image reference, e.g. ghcr.io/org/sayo-model-nemo:1.0.0",
    )
    p_push.set_defaults(func=cmd_push_model)

    p_pull = sub.add_parser(
        "pull-model",
        help=(
            "Pull a registry model image, optionally retag for local builds, extract "
            "models/<name>/ and model_repository/adapters/<adapter>.py into the repo."
        ),
    )
    p_pull.add_argument(
        "--from",
        dest="from_ref",
        required=True,
        help=(
            "Image ref: registry with tag/digest, or local image ID. "
            "Untagged images: use ID/digest or `docker tag <id> repo/name:tag` first."
        ),
    )
    p_pull.add_argument(
        "--as",
        dest="as_name",
        default=None,
        help="Model directory name under /app/models (default: sole child of /app/models)",
    )
    p_pull.add_argument(
        "--local-tag",
        default=None,
        help="Tag to apply after pull (default: sayo-model-<name>:latest)",
    )
    p_pull.add_argument(
        "--no-retag",
        action="store_true",
        help="Do not docker tag the pulled image to --local-tag",
    )
    p_pull.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing models/<name>/ and adapter .py if present",
    )
    p_pull.add_argument(
        "--no-pull",
        action="store_true",
        help="Do not run docker pull (use when the image ref already exists locally)",
    )
    p_pull.set_defaults(func=cmd_pull_model)

    args = parser.parse_args()
    args.func(args)

    if args.command == "stand":
        si = args.stand_image or f"sayo-stand-{args.model_name}:latest"
        _print_run_hint(args.model_name, si)
    elif args.command == "model":
        hint = (
            "Next: build stand with "
            f"'python model_build.py stand {args.model_name}' "
            "(or set --model-image if you used a custom model tag)."
        )
        print(hint)
    elif args.command == "push-model":
        print(
            "Done. Pull elsewhere with: python model_build.py pull-model --from <same-ref>"
        )
    elif args.command == "pull-model":
        pass

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: command failed with exit code {exc.returncode}", file=sys.stderr)
        raise SystemExit(exc.returncode)
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
