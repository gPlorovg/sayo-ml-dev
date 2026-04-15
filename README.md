## Sayo ML Dev

Local tooling to scaffold STT models, build Docker images, and run a small **gRPC stand** for integration tests.

### Layout

| Path | Role |
|------|------|
| `wizard/` | Scaffold `models/<name>/` and adapter stub |
| `model_repository/` | `ModelRepository`, adapters (e.g. NeMo) |
| `stand/server.py` | gRPC `SayoService`: `HealthCheck`, `StreamingRecognize` |
| `stand/client.py` | Test client (WAV / mic → stream) |
| `proto/sayo.proto` | API contract; generated stubs in `proto/` |
| `model_build.py` | Build model image and stand image |

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

```bash
python model_build.py <model_name>
```

Example:

```bash
python model_build.py nemo
```

```bash
docker run --gpus all \
  -v /path/to/repo/models/nemo/weights:/app/models/nemo/weights \
  -p 50051:50051 \
  sayo-stand-nemo:latest \
  --model nemo --device cuda
```

The stand loads one model from `model.yaml`. **Resample audio on the client** so `StreamingConfig.sample_rate_hertz` matches the model (see `HealthCheck` / `ModelDescriptor`).

### Test client

Uses `HealthCheck` → first `ModelDescriptor` for sample rate, chunk duration, and quantization.

```bash
uv run python stand/client.py --host 127.0.0.1 --port 50051 --audio path/to.wav
uv run python stand/client.py --host 127.0.0.1 --port 50051 --mic --mic-duration-s 5
uv run python stand/client.py --host 127.0.0.1 --port 50051 --mic-live
```

Optional: record WAV to `test_audio/` with `utils/record_waw.py`.
