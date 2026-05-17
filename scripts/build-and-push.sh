#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Build and push a decypharr Docker image.

Usage:
  ./scripts/build-and-push.sh <image> [tag]

Arguments:
  image   Full image name (examples: myuser/decypharr, ghcr.io/myorg/decypharr)
  tag     Optional image tag (default: latest)

Environment variables:
  PLATFORMS         Optional buildx platforms (default: linux/amd64)
  DOCKERFILE        Optional Dockerfile path (default: Dockerfile)
  BUILD_CONTEXT     Optional build context (default: .)

Examples:
  ./scripts/build-and-push.sh myuser/decypharr
  ./scripts/build-and-push.sh myuser/decypharr 0.3.1
  PLATFORMS=linux/amd64,linux/arm64 ./scripts/build-and-push.sh myuser/decypharr 0.3.1

Notes:
  - Run 'docker login' first for your target registry.
  - If docker buildx is available, this script uses buildx --push.
  - If buildx is not available, it falls back to docker build + docker push.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -lt 1 || $# -gt 2 ]]; then
  usage
  exit 1
fi

IMAGE_NAME="$1"
IMAGE_TAG="${2:-latest}"
FULL_IMAGE="${IMAGE_NAME}:${IMAGE_TAG}"
PLATFORMS="${PLATFORMS:-linux/amd64}"
DOCKERFILE="${DOCKERFILE:-Dockerfile}"
BUILD_CONTEXT="${BUILD_CONTEXT:-.}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is not installed or not in PATH" >&2
  exit 1
fi

if [[ ! -f "$DOCKERFILE" ]]; then
  echo "Dockerfile not found at: $DOCKERFILE" >&2
  exit 1
fi

echo "Building and pushing image: $FULL_IMAGE"
echo "Dockerfile: $DOCKERFILE"
echo "Build context: $BUILD_CONTEXT"
echo "Platforms: $PLATFORMS"

if docker buildx version >/dev/null 2>&1; then
  docker buildx build \
    --platform "$PLATFORMS" \
    -f "$DOCKERFILE" \
    -t "$FULL_IMAGE" \
    --push \
    "$BUILD_CONTEXT"
else
  echo "docker buildx not found; falling back to docker build + docker push"

  if [[ "$PLATFORMS" != "linux/amd64" ]]; then
    echo "Warning: fallback build only supports local daemon architecture; ignoring PLATFORMS=$PLATFORMS"
  fi

  docker build -f "$DOCKERFILE" -t "$FULL_IMAGE" "$BUILD_CONTEXT"
  docker push "$FULL_IMAGE"
fi

echo "Done: $FULL_IMAGE"
