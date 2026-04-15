#!/usr/bin/env bash
set -e

# Build pipeline:
#   1) base image (shared runtime)
#   2) per-model image (adapter + dependencies + model config)
#   3) stand image (gRPC server layer over per-model)

if [ -z "$1" ]; then
    echo "Usage: ./model_build.sh <model_name>"
    echo "Example: ./model_build.sh my_model"
    exit 1
fi

MODEL_NAME=$1
BASE_IMAGE="sayo-base:latest"
MODEL_IMAGE="sayo-model-${MODEL_NAME}:latest"
STAND_IMAGE="sayo-stand-${MODEL_NAME}:latest"
ADAPTER_NAME=$(sed -n 's/^adapter:[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "models/${MODEL_NAME}/model.yaml")

if [ -z "${ADAPTER_NAME}" ]; then
    echo "ERROR: failed to read 'adapter' from models/${MODEL_NAME}/model.yaml"
    exit 1
fi

echo "=== Building Base Image (${BASE_IMAGE}) ==="
docker build -f Docker.base -t ${BASE_IMAGE} .

echo "=== Building Model Image (${MODEL_IMAGE}) for ${MODEL_NAME} ==="
docker build \
    --build-arg BASE_IMAGE=${BASE_IMAGE} \
    --build-arg MODEL_NAME=${MODEL_NAME} \
    --build-arg ADAPTER_NAME=${ADAPTER_NAME} \
    -f Docker.model \
    -t ${MODEL_IMAGE} .

echo "=== Building Stand Image (${STAND_IMAGE}) for ${MODEL_NAME} ==="
docker build \
    --build-arg MODEL_IMAGE=${MODEL_IMAGE} \
    -f Docker.stand \
    -t ${STAND_IMAGE} .

echo "Done! You can now run:"
echo "docker run -v /path/to/weights:/app/models/${MODEL_NAME}/weights -p 50051:50051 ${STAND_IMAGE} --model ${MODEL_NAME}"
