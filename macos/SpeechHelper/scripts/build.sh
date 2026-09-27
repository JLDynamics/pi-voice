#!/usr/bin/env bash
# Build the on-device speech-to-text helper the Python pipeline shells out to.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="${1:-$ROOT/build/speech-helper}"
SDK="$(xcrun --sdk macosx --show-sdk-path)"
ARCH="$(uname -m)"
# SpeechAnalyzer/SpeechTranscriber are macOS 26+. The Voice app targets 14.0,
# but this binary is only reachable through the native STT backend, which needs
# the newer engine anyway.
TARGET="${ARCH}-apple-macos26.0"

mkdir -p "$(dirname "$OUT")"
touch "$(dirname "$OUT")/.metadata_never_index"
xcrun swiftc \
  -sdk "$SDK" \
  -target "$TARGET" \
  -parse-as-library \
  -O \
  -framework AVFoundation \
  -framework Speech \
  "$ROOT/Sources/main.swift" \
  -o "$OUT"

echo "Built $OUT"
