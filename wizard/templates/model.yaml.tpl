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

# Dependencies
dependencies:
  # Python dependencies required by this model adapter.
  packages: []
    # - "torch>=2.1.0"
  # Optional file with additional python dependencies (relative to model dir).
  requirements_file: null
  system_packages: []
  # Local weight artifacts (relative paths) for embedded mode.
  weights:
    # - "weights/model.pt"
  # Optional remote artifacts for on-demand download.
  remote_weights:
    # - uri: "https://example.com/model.pt"
    #   sha256: "<checksum>"
    #   filename: "model.pt"

# Runtime hints
runtime:
  min_vram_gb: 0.0
  max_batch_size: 1
  quantization: null
