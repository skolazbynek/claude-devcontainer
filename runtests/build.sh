#!/usr/bin/env bash
# Build the runtests image. Run from anywhere.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${RUNTESTS_IMAGE:-runtests:latest}"
exec docker build -t "$IMAGE" "$here"
