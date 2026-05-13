## Sayo ML Dev

Local tooling to scaffold speech-to-text (STT) models, build Docker images, and run a small **gRPC stand** for integration tests.

A Russian overview is in [`README.ru.md`](README.ru.md).

### Contents

- [What this repository provides](#what-this-repository-provides)
- [Requirements](#requirements)
- [Repository layout](#repository-layout)
- [Model package (`models/<name>/`)](#model-package-modelsname)
- [Environment (Python / uv)](#environment-python--uv)
- [Scaffold a new model (wizard)](#scaffold-a-new-model-wizard)
- [Protocol buffers (regenerate stubs)](#protocol-buffers-regenerate-stubs)
- [Docker images and build order](#docker-images-and-build-order)
- [`model_build.py` reference](#model_buildpy-reference)
- [Makefile shortcuts](#makefile-shortcuts)
- [Run the stand](#run-the-stand)
- [Stand server options](#stand-server-options)
- [gRPC behaviour (short)](#grpc-behaviour-short)
- [Test client](#test-client)
- [Push / pull model images](#push--pull-model-images)
- [Optional utilities](#optional-utilities)
- [Troubleshooting](#troubleshooting)

---

### What this repository provides

| Area | Purpose |
|------|---------|
| **Wizard** (`python -m wizard.cli`) | Creates `models/<name>/` (config, dependency stubs, `weights/`) and optionally a new adapter under `model_repository/adapters/`. |
| **Model repository** | `ModelRepository` reads `model.yaml`, resolves weight paths, imports `model_repository.adapters.<adapter>`, and instantiates a `BaseSTTModel` subclass. |
| **Stand** (`stand/server.py`) | Single-model gRPC server: `HealthCheck`, `StreamingRecognize`. |
| **Client** (`stand/client.py`) | Integration-style client: `HealthCheck` → stream file, microphone, or silence; resamples and chunks audio to match the descriptor. |
| **Build tooling** (`model_build.py`, `Makefile`) | Build `base` → `model` → `stand` images; push/pull model images and extract files back into the repo. |

---

### Requirements

| Tool | Notes |
|------|--------|
| **Python** | `>= 3.12` (see `pyproject.toml`). |
| **uv** | Recommended for syncing dependencies (`uv sync`). |
| **Docker** | Required for image builds and for `pull-model` extraction. |
| **GNU Make** (optional) | Used by `Makefile`. On Windows, Git Bash is fine; native `make` may differ. |
| **GPU** (optional) | `make run-stand` uses `docker run --gpus all`. Use `--device cpu` or adjust the run command if you have no NVIDIA GPU / toolkit. |

---

### Repository layout

| Path | Role |
|------|------|
| `wizard/` | Scaffold `models/<name>/` and adapter stub (`templates/`). |
| `model_repository/` | `ModelRepository`, `BaseSTTModel` / `STTConfig` / `STTResult`, adapters under `adapters/`. |
| `models/<name>/` | Per-model package: `model.yaml`, locks, weights. |
| `stand/server.py` | gRPC `SayoService`: `HealthCheck`, `StreamingRecognize`. |
| `stand/client.py` | gRPC test client (file / mic / silence). |
| `proto/sayo.proto` | API contract; Python stubs in `proto/` (regenerate after edits; Docker stand regenerates inside the image). |
| `model_build.py` | Build/push/pull Docker images. |
| `Docker.base`, `Docker.model`, `Docker.stand` | Image definitions (layered: base → model → stand). |
| `Makefile` | Shortcuts for builds, stand, client, push/pull. |
| `utils/record_waw.py` | Optional WAV recorder for tests. |

---

### Model package (`models/<name>/`)

Each model is a directory `models/<name>/` where `<name>` matches the directory name passed to `model_build.py model <name>` and to the stand as `--model <name>`.

| File / directory | Purpose |
|------------------|---------|
| **`model.yaml`** | Main config. **Required keys:** `id` (string, logical model id for clients), `adapter` (string, Python module name under `model_repository/adapters/`). Common keys: `description`, `language_code`, `sample_rate`, `latency`, `runtime`, `weights`. Any other top-level keys are passed into the adapter as `STTConfig.extra`. |
| **`runtime` (optional, mapping)** | Hints for clients via `HealthCheck` / `ModelDescriptor`: e.g. `chunk_duration_ms` (default `560`), `audio_quantization` (`pcm_f32le` or `pcm_s16le`), `supports_interim_results` (default `true`). |
| **`weights` (optional)** | Under `weights:`, list `artifacts` with `- path: "weights/..."` relative to the model directory. `ModelRepository` checks that paths exist when you call `validate_files()`. |
| **`requirements.lock`** | Python packages installed **inside the model image** (`uv pip install --system`). |
| **`system-packages.txt`** | Optional Debian packages (one per line, `#` comments allowed); installed in the model image. |
| **`weights/`** | Weight files (e.g. `.nemo`); copied into the model image. |

**Wizard naming rules:** model and adapter names must match `^[a-z0-9_-]+$` (lowercase Latin letters, digits, hyphen, underscore).

Example layout for a model named `nemo`:

```text
models/nemo/
  model.yaml
  requirements.lock
  system-packages.txt
  weights/
    sayo.nemo
```

---

### Environment (Python / uv)

From the repository root:

```bash
uv sync
uv sync --group dev   # grpc_tools, ruff, sounddevice/soundfile for client and protoc
```

The `dev` group is enough for local stub regeneration and for `stand/client.py` (file/mic).

For a **fully pinned** dependency export (includes `grpcio` and other packages), use `requirements.txt` if you prefer pip-only workflows:

```bash
pip install -r requirements.txt
```

---

### Scaffold a new model (wizard)

```bash
python -m wizard.cli --help
```

**Interactive (recommended):**

```bash
python -m wizard.cli
# or Russian UI strings:
python -m wizard.cli --lang ru
```

**Non-interactive:**

```bash
python -m wizard.cli --name whisper-large --adapter whisper
```

The wizard creates (when missing): `models/<name>/model.yaml`, `requirements.lock`, `system-packages.txt`, `weights/`, and `model_repository/adapters/<adapter>.py` if that adapter file does not exist yet.

---

### Protocol buffers (regenerate stubs)

After editing `proto/sayo.proto`, regenerate Python stubs in the repo root:

```bash
uv run python -m grpc_tools.protoc -I . --python_out=. --grpc_python_out=. proto/sayo.proto
```

The **stand Docker image** runs `grpc_tools.protoc` during build so stubs match the image’s protobuf/grpc versions; you still need local stubs for editor checks and for running `stand/server.py` outside Docker without import errors.

---

### Docker images and build order

Images are layered:

1. **`sayo-base:latest`** (from `Docker.base`) — Python base + `uv` + build essentials.
2. **`sayo-model-<name>:latest`** (from `Docker.model`) — installs `system-packages.txt` and `requirements.lock`, copies `models/<name>/` (including weights), copies **one** adapter file chosen from `model.yaml`’s `adapter:` field, plus shared `model_repository` modules.
3. **`sayo-stand-<name>:latest`** (from `Docker.stand`) — starts from the model image, adds `stand/` and `proto/`, generates gRPC code in the image, entrypoint `python -m stand.server`.

**Rebuild tips:** Changing only `model.yaml`, adapter code, or weights usually requires rebuilding **model** (and then **stand**). You typically do **not** need to rebuild **base** unless the base image definition changes.

---

### `model_build.py` reference

All commands are run from the repository root (where `Docker.*` files live).

| Command | Purpose |
|---------|---------|
| `python model_build.py base` | Build base image. `--base-image` (default `sayo-base:latest`). |
| `python model_build.py model <model_name>` | Build model image. `--base-image`, `--model-image` (default `sayo-model-<name>:latest`). Reads `adapter` from `models/<name>/model.yaml`. |
| `python model_build.py stand <model_name>` | Build stand image. `--model-image` (default `sayo-model-<name>:latest`), `--stand-image` (default `sayo-stand-<name>:latest`). |
| `python model_build.py push-model <model_name> --to <remote>` | Tag local model image and `docker push`. Optional `--from` for a non-default local tag. |
| `python model_build.py pull-model --from <ref>` | `docker pull` (unless `--no-pull`), optional retag to `sayo-model-<name>:latest`, extract `models/<name>/` and `model_repository/adapters/<adapter>.py`. See flags below. |

**`pull-model` flags:**

| Flag | Meaning |
|------|---------|
| `--from <ref>` | **Required.** Registry image, digest, or local image id/tag. |
| `--as <name>` | Model directory under `/app/models` when the image contains more than one. |
| `--local-tag <tag>` | Override default `sayo-model-<name>:latest` retag. |
| `--no-retag` | Do not create the local `sayo-model-*` tag (pass `--model-image` to `stand` build). |
| `--overwrite` | Replace existing `models/<name>/` and adapter file. |
| `--no-pull` | Use a local image only (`docker pull` skipped); ref must exist locally. |

---

### Makefile shortcuts

```bash
make help
```

| Target | Role |
|--------|------|
| `make build-base` | `model_build.py base` |
| `make build-model` | `model_build.py model` with `MODEL`, `BASE_IMAGE`, `MODEL_IMAGE` |
| `make build-stand` | `model_build.py stand` with `MODEL`, `MODEL_IMAGE`, `STAND_IMAGE` |
| `make build-all` | base, then model, then stand |
| `make run-stand` | `docker run` with GPU, maps `PORT` → `50051`, runs `--device cuda` |
| `make run-client-mic` / `make run-client-file` | Run `stand/client.py` via `uv run` |

**Variables:** `MODEL` (default `nemo`), `BASE_IMAGE`, `MODEL_IMAGE`, `STAND_IMAGE`, `PORT`, `HOST`, `FILE`, `TO`, `FROM`, `EXTRACT_MODEL`, `OVERWRITE`, `NO_RETAG`, `NO_PULL`, `LOCAL_TAG`.

**Windows / GNU Make:** use `EXTRACT_MODEL=...` for `pull-model`, **not** `AS=...` (GNU Make reserves `AS` for the assembler).

---

### Run the stand

**Docker (after `build-all` or equivalent):**

```bash
docker run --rm --gpus all \
  -p 50051:50051 \
  sayo-stand-nemo:latest \
  --model nemo --device cuda
```

**CPU example:**

```bash
docker run --rm -p 50051:50051 sayo-stand-nemo:latest --model nemo --device cpu
```

**Makefile:**

```bash
make run-stand          # MODEL=nemo by default
make run-stand MODEL=gigaam PORT=50052
```

---

### Stand server options

Entrypoint: `python -m stand.server` (see `Docker.stand`).

| CLI | Environment variable | Default | Description |
|-----|----------------------|---------|-------------|
| `--model` / `-m` | `STAND_MODEL` | `nemo` | Subdirectory under `models/` (must contain `model.yaml`). |
| `--device` / `-d` | `STAND_DEVICE` | `cpu` | Device string passed to the adapter (e.g. `cuda`, `cuda:0`). |
| `--port` / `-p` | `STAND_PORT` | `50051` | gRPC listen port (container internal; map with `-p host:50051`). |
| `--models-dir` | `STAND_MODELS_DIR` | `<repo>/models` | Root directory containing model folders. |

The stand loads **exactly one** model. Clients must send `StreamingConfig.sample_rate_hertz` equal to the model’s `sample_rate` (the server rejects mismatches and asks clients to resample).

---

### gRPC behaviour (short)

- **`HealthCheck`:** returns readiness and a single `ModelDescriptor` (sample rate, chunk duration, PCM format, interim flag, etc.) derived from `model.yaml` / `runtime`.
- **`StreamingRecognize`:** bidirectional stream. First message must be **`StreamingConfig`**; following messages are **raw mono PCM** chunks (`audio_chunk`). The server decodes to float32 and forwards chunks to the adapter’s streaming API.

Full message definitions: `proto/sayo.proto`.

---

### Test client

Flow: connect → `HealthCheck` → derive stream settings → `StreamingRecognize` with config first, then audio.

```bash
uv run python stand/client.py --host 127.0.0.1 --port 50051 --audio path/to.wav
uv run python stand/client.py --host 127.0.0.1 --port 50051 --mic   # Ctrl+C to stop
```

```bash
make run-client-mic
make run-client-file FILE=path/to.wav    # optional: HOST=... PORT=...
```

| Option | Environment | Description |
|--------|-------------|-------------|
| `--host` | `STAND_HOST` | Server host (default `localhost`). |
| `--port` | `STAND_PORT` | Server port (default `50051`). |
| `--model` / `-m` | `STAND_MODEL` | If set, must equal `ModelDescriptor.model_id` from `HealthCheck`. |
| `--audio` / `-a` | — | Audio file path (read with **soundfile**; resampled to model rate). |
| `--mic` | — | Stream from microphone until interrupted. |
| `--mic-device` | — | PortAudio device index or name substring if the default input is wrong. |
| `--send-delay-ms` | — | Pause between chunks for file/silence mode (simulates slower senders). |

If **neither** `--audio` nor `--mic` is given, the client sends **5 seconds of silence** and prints a short summary at exit.

---

### Push / pull model images

Inside the **model** image:

- Model tree: `/app/models/<name>/` (includes `model.yaml` and `weights/`).
- Adapter: `/app/model_repository/adapters/<adapter>.py` (single file).

**Push** (retag local `sayo-model-<name>:latest`, then push):

```bash
python model_build.py push-model nemo --to ghcr.io/myorg/sayo-model-nemo:1.0.0
# optional: --from sayo-model-nemo:other
```

```bash
make push-model MODEL=nemo TO=ghcr.io/myorg/sayo-model-nemo:1.0.0
```

**Pull** (pull from registry unless `--no-pull`, optionally retag, extract into repo):

```bash
python model_build.py pull-model --from ghcr.io/myorg/sayo-model-nemo:1.0.0
python model_build.py pull-model --from ghcr.io/myorg/sayo-model-nemo:1.0.0 --as nemo
python model_build.py pull-model --from ghcr.io/myorg/sayo-model-nemo:1.0.0 --overwrite
python model_build.py pull-model --from ghcr.io/myorg/sayo-model-nemo:1.0.0 --no-retag
python model_build.py stand nemo --model-image ghcr.io/myorg/sayo-model-nemo:1.0.0
python model_build.py pull-model --from ghcr.io/myorg/sayo-model-nemo:1.0.0 --no-pull
python model_build.py pull-model --from abc123def456 --no-pull --as gigaam
```

```bash
make pull-model FROM=ghcr.io/myorg/sayo-model-nemo:1.0.0
make pull-model FROM=ghcr.io/myorg/sayo-model-nemo:1.0.0 EXTRACT_MODEL=nemo OVERWRITE=1
make pull-model FROM=ghcr.io/myorg/sayo-model-nemo:1.0.0 NO_PULL=1
```

---

### Optional utilities

Record a WAV under `test_audio/` (typical use: build a short clip for `run-client-file`):

```bash
uv run python utils/record_waw.py
```

---

### Troubleshooting

| Symptom | What to check |
|---------|----------------|
| **`sample_rate_hertz` error** from stand | Client must resample to the model’s `sample_rate` in `model.yaml`; the test client does this automatically from `HealthCheck`. |
| **Mic client cannot open device** | Try `--mic-device` with index or name substring; list devices: `python -c "import sounddevice as sd; print(sd.query_devices())"`. |
| **`make run-stand` fails on GPU** | Install NVIDIA Container Toolkit or run `docker run` without `--gpus` and use `--device cpu`. |
| **`pull-model` refuses to overwrite** | Pass `--overwrite` or remove conflicting paths under `models/<name>/` and the adapter file. |
| **Import errors for `proto`** | Run `uv sync --group dev` and regenerate stubs after `sayo.proto` changes. |
| **Stale Python stubs in Docker** | Stand image rebuilds stubs; rebuild `stand` after proto edits. |

---

### Related documents

- [`README.ru.md`](README.ru.md) — Russian mirror of this guide.
- [`SayoOverview.md`](SayoOverview.md) — high-level Sayo project context (optional).
- [`SAYO_ML_ROADMAP.md`](SAYO_ML_ROADMAP.md) — ML training / data prep notes (optional).
