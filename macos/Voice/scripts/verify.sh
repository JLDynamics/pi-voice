#!/usr/bin/env bash
# API and live-turn verify for the headless Voice backend (see scripts/README.md).
# Start the backend first with ./run-browser.sh.
# Usage:
#   ./macos/Voice/scripts/verify.sh [--research] [--skip-talk]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$ROOT"
exec uv run python "$ROOT/scripts/verify-voice.py" "$@"
