# ============================================================================
# {name} — model configuration
# ============================================================================
# Adapter: model_repository/adapters/{adapter}.py
# ============================================================================

# Metadata
id: "{id}"
adapter: "{adapter}"
description: "{description}"

# Model properties
language_code: "{language_code}"
sample_rate: {sample_rate}
streaming: {streaming}

latency: 0.0

# Dependencies
dependencies:
  packages: []
    # - "torch>=2.1.0"
  system_packages: []
  weights: []
    # - "weights/model.pt"

# Runtime hints
runtime:
  min_vram_gb: 0.0
  max_batch_size: 1
  quantization: null
