#!/usr/bin/env sh
# Build one image per process with only that process's packages (no provider SDK in the runtime image, no
# LangGraph in the gateways, no DB driver in the cell image).
set -eu
cd "$(dirname "$0")/../.."
TAG=${TAG:-dev}
b() { docker build -f deploy/docker/Dockerfile --target runtime-base --build-arg PACKAGES="$2" -t "aiagent/$1:$TAG" .; }
b api        "aiagent-api"
b runtime    "aiagent-runtime"
b gateways   "aiagent-gateways"
b sentinel   "aiagent-sentinel"
b migrate    "aiagent-core"
docker build -f deploy/docker/Dockerfile.cell -t "aiagent/cell:$TAG" .
