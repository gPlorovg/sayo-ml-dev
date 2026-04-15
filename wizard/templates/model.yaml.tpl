# ============================================================================
# {name} — model configuration
# ============================================================================
# Adapter: model_repository/adapters/{adapter}.py
# ============================================================================

# Metadata
schema_version: 1
id: "{id}"
adapter: "{adapter}"
description: "{description}"

# Model properties
language_code: "{language_code}"
sample_rate: {sample_rate}

# Optional latency hint in milliseconds
latency: 0.0

# Model artifacts.
# Dependencies are declared in files next to this config:
#   - requirements.lock
#   - system-packages.txt
weights:
  revision: "v1"
  artifacts:
    # - path: "weights/model.safetensors"
    #   uri: "https://example.com/model.safetensors"
    #   sha256: "<checksum>"
    #   required: true

# Runtime hints
runtime:
  min_vram_gb: 0.0
  max_batch_size: 1
  quantization: null
