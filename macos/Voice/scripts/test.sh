#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$(mktemp -d "${TMPDIR:-/tmp}/voice-tests.XXXXXX")"
trap 'rm -rf "$OUT"' EXIT
export CLANG_MODULE_CACHE_PATH="${CLANG_MODULE_CACHE_PATH:-/tmp/chatbot-swift-cache}"
xcrun swiftc -parse-as-library -framework AppKit \
  "$ROOT"/Sources/Session/{VoiceRuntime,VoiceTools,LocalService,LocalServiceStarter,VoiceSession,MockVoiceBackend,HeadlessBridge,PiJobTracker}.swift \
  "$ROOT/Tests/RuntimeTests.swift" -o "$OUT/runtime-tests"
"$OUT/runtime-tests"
