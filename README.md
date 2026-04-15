## Sayo ML Dev Tooling

Local-first tooling for ML developers to prepare and test STT models with a minimal gRPC stand.

### What is included

- `wizard`: scaffolds a model directory and adapter template.
- `model_repository`: unified interface (`ModelRepository`) used by adapters and stand.
- `stand`: local gRPC server layer over a per-model image.

This repository currently focuses on ML developer workflow and local integration testing.
`prod-runtime` is intentionally out of scope here.

### Model directory contract

Each model lives in `models/<model_name>/` and contains:

- `model.yaml` - model metadata, adapter name, runtime hints, and `weights.artifacts`.
- `requirements.lock` - Python runtime deps for this model.
- `system-packages.txt` - optional OS packages (one per line).
- `weights/` - local folder for model weights during local runs.

### Create a model scaffold

```bash
python -m wizard.cli
```

### Build images (base -> per-model -> stand)

```bash
./model_build.sh <model_name>
```

Example:

```bash
./model_build.sh nemo
```

### Run local gRPC stand

```bash
docker run \
  -v /path/to/weights:/app/models/<model_name>/weights \
  -p 50051:50051 \
  sayo-stand-<model_name>:latest \
  --model <model_name>
```
