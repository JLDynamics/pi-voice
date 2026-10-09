#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SWIFT_SUITE="bash macos/Voice/scripts/test.sh"
TS_SUITE="node --test 'pi-voice/src/*.test.ts'"
PY_SUITE="uv run pytest -q tests/test_contract.py tests/test_voice_prompt.py"

run_all_suites() {
  echo "Running Swift runtime tests..."
  eval "$SWIFT_SUITE"
  echo "Running TypeScript tests..."
  eval "$TS_SUITE"
  echo "Running Python contract tests..."
  eval "$PY_SUITE"
  echo "All contract suites passed."
}

if [[ "${1:-}" != "--mutate" ]]; then
  run_all_suites
  exit 0
fi

# List of files that will be mutated
MUTATED_FILES=(
  "macos/Voice/Sources/Session/HeadlessBridge.swift"
  "macos/Voice/Sources/Session/VoiceTools.swift"
  "macos/Voice/Sources/Session/PiJobTracker.swift"
  "macos/Voice/Sources/Session/VoiceRuntime.swift"
  "pi-voice/src/child.ts"
  "src/chatbot/LLM/voice_prompt.py"
)

# 1. Refuse to run if any file to mutate has uncommitted changes
for file in "${MUTATED_FILES[@]}"; do
  if ! git diff --quiet -- "$file" || ! git diff --cached --quiet -- "$file"; then
    echo "ERROR: Refusing to run --mutate because file has uncommitted changes: $file" >&2
    exit 1
  fi
done

TEMP_DIR=$(mktemp -d)
CURRENT_TARGET=""
CURRENT_BACKUP=""

cleanup() {
  if [[ -n "$CURRENT_TARGET" && -n "$CURRENT_BACKUP" && -f "$CURRENT_BACKUP" ]]; then
    cp "$CURRENT_BACKUP" "$CURRENT_TARGET"
    rm -f "$CURRENT_BACKUP"
  fi
  rm -rf "$TEMP_DIR"
}
trap cleanup EXIT INT TERM

# Mutation table: label | file | literal_to_replace | replacement | suite_command
MUTATIONS=(
  'Swift HeadlessBridge emit key "item_id" -> "itemId"|macos/Voice/Sources/Session/HeadlessBridge.swift|"item_id": itemId ?? ""|"itemId": itemId ?? ""|bash macos/Voice/scripts/test.sh'
  'Swift HeadlessBridge inbound "speak" -> "speech"|macos/Voice/Sources/Session/HeadlessBridge.swift|string("speak")|string("speech")|bash macos/Voice/scripts/test.sh'
  'Swift VoiceTools HeadlessTools "spawn_thinking" -> "spawn_think"|macos/Voice/Sources/Session/VoiceTools.swift|static let spawnThinking = "spawn_thinking"|static let spawnThinking = "spawn_think"|bash macos/Voice/scripts/test.sh'
  'Swift PiJobTracker "[FINAL]" (finalChannel) -> "[DONE]"|macos/Voice/Sources/Session/PiJobTracker.swift|return "[FINAL] Partial findings|return "[DONE] Partial findings|bash macos/Voice/scripts/test.sh'
  'Swift PiJobTracker case superseded -> case replaced|macos/Voice/Sources/Session/PiJobTracker.swift|case superseded|case replaced|bash macos/Voice/scripts/test.sh'
  'Swift VoiceRuntime "VOICE_HISTORY" -> "VOICE_HIST"|macos/Voice/Sources/Session/VoiceRuntime.swift|static let variable = "VOICE_HISTORY"|static let variable = "VOICE_HIST"|bash macos/Voice/scripts/test.sh'
  'TS child.ts parseLine object.item_id -> object.itemId|pi-voice/src/child.ts|const itemId = typeof object.item_id === "string" ? object.item_id : "";|const itemId = typeof object.itemId === "string" ? object.itemId : "";|node --test '\''pi-voice/src/*.test.ts'\'''
  'TS child.ts toVoice muted } -> mute: muted }|pi-voice/src/child.ts|muted }|mute: muted }|node --test '\''pi-voice/src/*.test.ts'\'''
  'TS child.ts JOB_UPDATE_STATUSES "dropped" -> "declined"|pi-voice/src/child.ts|"dropped"|"declined"|node --test '\''pi-voice/src/*.test.ts'\'''
  'TS child.ts VOICE_HISTORY -> VOICE_HIST|pi-voice/src/child.ts|VOICE_HISTORY|VOICE_HIST|node --test '\''pi-voice/src/*.test.ts'\'''
  'Python voice_prompt "[STATUS]" -> "[STATE]"|src/chatbot/LLM/voice_prompt.py|[STATUS] is silent background progress — do not speak it. Asked how it'\''s going, answer from the latest [STATUS]|[STATE] is silent background progress — do not speak it. Asked how it'\''s going, answer from the latest [STATE]|uv run pytest -q tests/test_contract.py tests/test_voice_prompt.py'
)

RESULTS=()
ANY_SURVIVED=0
TOTAL_MUTATIONS=${#MUTATIONS[@]}
MUTATION_INDEX=0

echo "Starting contract mutation tests (${TOTAL_MUTATIONS} mutations)..."

for entry in "${MUTATIONS[@]}"; do
  MUTATION_INDEX=$((MUTATION_INDEX + 1))
  IFS='|' read -r label file target replacement suite_cmd <<< "$entry"

  CURRENT_TARGET="$file"
  CURRENT_BACKUP="$TEMP_DIR/backup_$(basename "$file")_$MUTATION_INDEX"
  cp "$CURRENT_TARGET" "$CURRENT_BACKUP"

  # Replace exactly one occurrence of string literal; fail loudly if count != 1
  python3 -c '
import sys
file_path, target, replacement = sys.argv[1], sys.argv[2], sys.argv[3]
with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()
count = content.count(target)
if count != 1:
    sys.stderr.write(f"ERROR: literal {target!r} found {count} times (expected exactly 1) in {file_path}\n")
    sys.exit(2)
new_content = content.replace(target, replacement, 1)
with open(file_path, "w", encoding="utf-8") as f:
    f.write(new_content)
' "$CURRENT_TARGET" "$target" "$replacement"

  RUN_LOG="$TEMP_DIR/run_${MUTATION_INDEX}.log"
  SUITE_EXIT=0
  eval "$suite_cmd" > "$RUN_LOG" 2>&1 || SUITE_EXIT=$?

  # Restore immediately
  cp "$CURRENT_BACKUP" "$CURRENT_TARGET"
  rm -f "$CURRENT_BACKUP"
  CURRENT_TARGET=""
  CURRENT_BACKUP=""

  if [[ $SUITE_EXIT -ne 0 ]]; then
    RESULTS+=("PASS|$label|$file")
  else
    RESULTS+=("SURVIVED|$label|$file")
    ANY_SURVIVED=1
    echo "MUTATION SURVIVED: $label" >&2
    echo "--- Log output ---" >&2
    cat "$RUN_LOG" >&2
    echo "------------------" >&2
  fi
done

echo ""
echo "=========================================================================================="
printf "%-10s | %-56s | %s\n" "RESULT" "MUTATION" "FILE"
echo "------------------------------------------------------------------------------------------"
for res in "${RESULTS[@]}"; do
  IFS='|' read -r status label file <<< "$res"
  printf "%-10s | %-56s | %s\n" "$status" "$label" "$file"
done
echo "=========================================================================================="

if [[ $ANY_SURVIVED -ne 0 ]]; then
  echo "FAILURE: One or more mutations survived!" >&2
  exit 1
else
  echo "SUCCESS: All $TOTAL_MUTATIONS mutations caught."
  exit 0
fi
