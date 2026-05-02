# Sayo ML Dev — Docker builds and stand run
# Requires: Docker, Python, GNU Make (Git Bash on Windows is fine).

MODEL        ?= nemo
BASE_IMAGE   ?= sayo-base:latest
MODEL_IMAGE  ?= sayo-model-$(MODEL):latest
STAND_IMAGE  ?= sayo-stand-$(MODEL):latest
PORT         ?= 50051
# Host path to weights (mount as /app/models/<MODEL>/weights)
WEIGHTS      ?= $(CURDIR)/models/$(MODEL)/weights

.PHONY: help build-base build-model build-stand build-all run-stand

help:
	@echo "Targets:"
	@echo "  make build-base   MODEL=$(MODEL)     - Docker.base"
	@echo "  make build-model  MODEL=$(MODEL)     - Docker.model"
	@echo "  make build-stand  MODEL=$(MODEL)     - Docker.stand"
	@echo "  make build-all    MODEL=$(MODEL)     - base, then model, then stand"
	@echo "  make run-stand    MODEL=$(MODEL)     - run stand container"
	@echo "Variables: MODEL, BASE_IMAGE, MODEL_IMAGE, STAND_IMAGE, PORT, WEIGHTS"

build-base:
	python model_build.py base --base-image $(BASE_IMAGE)

build-model:
	python model_build.py model $(MODEL) --base-image $(BASE_IMAGE) --model-image $(MODEL_IMAGE)

build-stand:
	python model_build.py stand $(MODEL) --model-image $(MODEL_IMAGE) --stand-image $(STAND_IMAGE)

build-all: build-base build-model build-stand

run-stand:
	docker run --rm --gpus all \
		-v "$(WEIGHTS):/app/models/$(MODEL)/weights" \
		-p $(PORT):50051 \
		$(STAND_IMAGE) --model $(MODEL) --device cuda
