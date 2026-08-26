#!/usr/bin/env bash
# Build the graphqlserver image. Run from anywhere.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${GRAPHQL_IMAGE:-graphqlserver:latest}"
exec docker build -t "$IMAGE" "$here"
