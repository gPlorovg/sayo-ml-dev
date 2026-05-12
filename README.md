## Sayo ML Dev

Local tooling to scaffold STT models, build Docker images, and run a small **gRPC stand** for integration tests.

### Layout

| Path | Role |
|------|------|
| `wizard/` | Scaffold `models/<name>/` and adapter stub |
| `model_repository/` | `ModelRepository`, adapters (e.g. NeMo) |
| `stand/server.py` | gRPC `SayoService`: `HealthCheck`, `StreamingRecognize` |
| `stand/client.py` | gRPC test client (file / mic) |
| `proto/sayo.proto` | API contract; generated stubs in `proto/` |
| `model_build.py` | Build Docker images (`base` \| `model` \| `stand`) |
| `Makefile` | Shortcuts for builds and `run-stand` |

### Model directory (`models/<name>/`)

- `model.yaml` — `id`, `adapter`, `language_code`, `sample_rate`, `latency`, optional `runtime` (e.g. `chunk_duration_ms`, `audio_quantization`, `supports_interim_results`), `weights.artifacts`
- `requirements.lock`, `system-packages.txt`
- `weights/` — local weights (mounted into containers)

### Environment

```bash
uv sync
uv sync --group dev   # grpc_tools, ruff, sound libs for client / protoc
```

For a full locked runtime (includes `grpcio`), use `requirements.txt` as needed.

### Scaffold a new model
```bash
python -m wizard.cli --help
```
```bash
python -m wizard.cli
```

Regenerate Python stubs after editing `proto/sayo.proto`:

```bash
uv run python -m grpc_tools.protoc -I . --python_out=. --grpc_python_out=. proto/sayo.proto
```

### Build and run stand (Docker)

Build targets are separate: **base** → **model** → **stand** (model and stand depend on the previous image).

```bash
python model_build.py base
python model_build.py model <model_name>
python model_build.py stand <model_name>
```

Example (`nemo`):

```bash
python model_build.py base
python model_build.py model nemo
python model_build.py stand nemo
```

Or with **Make** (defaults `MODEL=nemo`; override with `make run-stand MODEL=nemo`):

```bash
make build-all          # base, model, stand in order
make run-stand          # same as docker run below
```

The model image keeps `apt` / `uv pip` layers when only `model.yaml` or the adapter change; rebuild with `make build-model` (or `python model_build.py model <name>`) without rebuilding base.

```bash
docker run --gpus all \
  -v /path/to/repo/models/nemo/weights:/app/models/nemo/weights \
  -p 50051:50051 \
  sayo-stand-nemo:latest \
  --model nemo --device cuda
```

The stand loads one model from `model.yaml`. **Resample audio on the client** so `StreamingConfig.sample_rate_hertz` matches the model (see `HealthCheck` / `ModelDescriptor`).

### Test client

One channel per run: `HealthCheck`, then `StreamingRecognize` using the model descriptor (sample rate, chunk duration, quantization). File and mic paths resample or chunk audio to match the server config.

```bash
uv run python stand/client.py --host 127.0.0.1 --port 50051 --audio path/to.wav
uv run python stand/client.py --host 127.0.0.1 --port 50051 --mic   # Ctrl+C to stop
```

```bash
make run-client-mic
make run-client-file FILE=path/to.wav    # optional: HOST=... PORT=...
```

`--model` / `-m` must match the stand’s `model_id` when set. `--mic-device`: PortAudio input index or name substring if the default input is wrong or unset. `--send-delay-ms`: delay between chunks in file (and default silence) mode. `STAND_HOST`, `STAND_PORT`, and `STAND_MODEL` can be used instead of `--host`, `--port`, and `--model`.

If neither `--audio` nor `--mic` is given, the client streams 5 seconds of silence. It prints a short summary block at exit.

Optional: record WAV under `test_audio/` with `utils/record_waw.py`.
