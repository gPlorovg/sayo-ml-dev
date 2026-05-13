# Sayo ML Dev — Docker builds and stand run
# Requires: Docker, Python, GNU Make (Git Bash on Windows is fine).

MODEL        ?= nemo
BASE_IMAGE   ?= sayo-base:latest
MODEL_IMAGE  ?= sayo-model-$(MODEL):latest
STAND_IMAGE  ?= sayo-stand-$(MODEL):latest
PORT         ?= 50051
HOST         ?= 127.0.0.1
# run-client-file: path to WAV/audio (required), e.g. FILE=path/to.wav
# push-model:  TO=registry/sayo-model-nemo:1.0.0  (optional FROM=... for non-default local tag)
# pull-model:  FROM=registry/sayo-model-nemo:1.0.0  (optional EXTRACT_MODEL=name; OVERWRITE=1; NO_RETAG=1; NO_PULL=1; LOCAL_TAG=...)

.PHONY: help build-base build-model build-stand build-all run-stand run-client-mic run-client-file push-model pull-model

help:
	@echo "Targets:"
	@echo "  make build-base   MODEL=$(MODEL)     - Docker.base"
	@echo "  make build-model  MODEL=$(MODEL)     - Docker.model (includes weights by default)"
	@echo "  make build-stand  MODEL=$(MODEL)     - Docker.stand"
	@echo "  make build-all    MODEL=$(MODEL)     - base, then model, then stand"
	@echo "  make push-model   MODEL=$(MODEL) TO=...   - tag local model image and docker push"
	@echo "  make pull-model   FROM=...          - pull, retag, extract models/<name>/ + adapter"
	@echo "  make run-stand         MODEL=$(MODEL)     - run stand container"
	@echo "  make run-client-mic    HOST=$(HOST) PORT=$(PORT)  - test client (microphone)"
	@echo "  make run-client-file   FILE=path/to.wav   - test client (audio file)"
	@echo "Variables: MODEL, BASE_IMAGE, MODEL_IMAGE, STAND_IMAGE, PORT, HOST, FILE, TO, FROM, EXTRACT_MODEL, OVERWRITE, NO_RETAG, NO_PULL, LOCAL_TAG"

build-base:
	python model_build.py base --base-image $(BASE_IMAGE)

build-model:
	python model_build.py model $(MODEL) --base-image $(BASE_IMAGE) --model-image $(MODEL_IMAGE)

build-stand:
	python model_build.py stand $(MODEL) --model-image $(MODEL_IMAGE) --stand-image $(STAND_IMAGE)

build-all: build-base build-model build-stand

push-model:
	$(if $(strip $(TO)),,$(error TO is required, e.g. make push-model MODEL=nemo TO=ghcr.io/org/sayo-model-nemo:1.0.0))
	python model_build.py push-model $(MODEL) --to "$(TO)" $(if $(strip $(FROM)),--from "$(FROM)",)

pull-model:
	$(if $(strip $(FROM)),,$(error FROM is required, e.g. make pull-model FROM=ghcr.io/org/sayo-model-nemo:1.0.0))
	python model_build.py pull-model --from "$(FROM)" $(if $(strip $(EXTRACT_MODEL)),--as $(EXTRACT_MODEL),) $(if $(filter 1,$(OVERWRITE)),--overwrite,) $(if $(filter 1,$(NO_RETAG)),--no-retag,) $(if $(filter 1,$(NO_PULL)),--no-pull,) $(if $(strip $(LOCAL_TAG)),--local-tag "$(LOCAL_TAG)",)

run-stand:
	docker run --rm --gpus all \
		-p $(PORT):50051 \
		$(STAND_IMAGE) --model $(MODEL) --device cuda

run-client-mic:
	uv run python stand/client.py --host $(HOST) --port $(PORT) --mic

run-client-file:
	$(if $(strip $(FILE)),,$(error FILE is required, e.g. make run-client-file FILE=path/to.wav))
	uv run python stand/client.py --host $(HOST) --port $(PORT) --audio "$(FILE)"
