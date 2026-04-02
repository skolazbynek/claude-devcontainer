#!/bin/bash
SCRIPT_DIR="$(dirname "$(realpath "${BASH_SOURCE[0]}")")"
CLD_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"
VENV="$CLD_ROOT/.venv/bin/python"
if [ ! -x "$VENV" ]; then
    echo "Error: venv not found at $VENV. Run 'poetry install' in $CLD_ROOT" >&2
    exit 1
fi
exec "$VENV" -m cld.mcp.graphql
