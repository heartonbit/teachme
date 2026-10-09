#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${PYTHON:-python3}

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    printf 'Error: %s was not found. Install Python 3.10 or later first.\n' "$PYTHON" >&2
    exit 1
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    printf 'Error: Python 3.10 or later is required.\n' >&2
    exit 1
fi

chmod +x "$PROJECT_DIR/run.sh"

if [ ! -f "$PROJECT_DIR/.env" ]; then
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    chmod 600 "$PROJECT_DIR/.env"
    printf 'Created %s/.env from .env.example. Add your LLM API settings before running.\n' "$PROJECT_DIR"
else
    printf 'Keeping existing %s/.env unchanged.\n' "$PROJECT_DIR"
fi

mkdir -p "$PROJECT_DIR/result" "$PROJECT_DIR/logs"
"$PYTHON" -m compileall -q "$PROJECT_DIR/src"

printf '\nTeach Me is installed.\n'
printf '1. Edit %s/.env and set LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL.\n' "$PROJECT_DIR"
printf '2. Run: %s/run.sh --debug\n' "$PROJECT_DIR"