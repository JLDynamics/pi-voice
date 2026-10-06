#!/usr/bin/env bash
# Start the single supported realtime backend:
# on-device macOS STT -> Responses API -> Siri TTS.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Prefer this checkout's sources so a shared .venv (git worktree) still runs
# the code you just edited.
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

CHATBOT_ENV="$HOME/.config/chatbot/env"
if [[ -f "$CHATBOT_ENV" ]]; then
  saved_openrouter="${OPENROUTER_API_KEY:-}"
  # shellcheck disable=SC1090
  source "$CHATBOT_ENV"
  [[ -n "$saved_openrouter" ]] && OPENROUTER_API_KEY="$saved_openrouter"
fi

MODEL="${MODEL:-z-ai/glm-5.3-flash}"
PORT="${PORT:-8766}"
# none | minimal | low | medium | high. Low keeps replies quick while still
# letting the model reason briefly about whether it needs to search.
REASONING_EFFORT="${REASONING_EFFORT:-low}"
BATCH_SENTENCES="${BATCH_SENTENCES:-3}"
CHAT_SIZE="${CHAT_SIZE:-100}"
DEFAULT_PROMPT='You are an AI conversation partner: perceptive, relaxed, warm, and quietly playful. You enjoy exploring ideas and have something thoughtful to contribute. Speak with the ease of someone comfortable in the conversation.'
PROMPT="${PROMPT:-$DEFAULT_PROMPT}"
VAD_THRESH="${VAD_THRESH:-0.6}"
VAD_MIN_SILENCE_MS="${VAD_MIN_SILENCE_MS:-2000}"
VAD_MIN_SPEECH_MS="${VAD_MIN_SPEECH_MS:-384}"
VAD_SPEECH_PAD_MS="${VAD_SPEECH_PAD_MS:-500}"
VAD_SHORT_SEGMENT_MERGE_MS="${VAD_SHORT_SEGMENT_MERGE_MS:-0}"
# Transcription runs on this Mac, through Apple's on-device engine. The engine
# has no auto-detect, so this fixes the spoken language.
STT_LOCALE="${STT_LOCALE:-en-US}"

if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
  echo "Error: OPENROUTER_API_KEY is not set. Run ./set-keys.sh first." >&2
  exit 1
fi

if [[ -x "$ROOT/.venv/bin/chatbot" ]]; then
  CHATBOT_BIN="$ROOT/.venv/bin/chatbot"
elif command -v chatbot >/dev/null 2>&1; then
  CHATBOT_BIN="chatbot"
else
  echo "Error: chatbot is not installed. Run: uv sync" >&2
  exit 1
fi

SPEECH_HELPER="$ROOT/macos/SpeechHelper/build/speech-helper"
if [[ ! -x "$SPEECH_HELPER" ]]; then
  echo "Building the on-device speech helper..."
  "$ROOT/macos/SpeechHelper/scripts/build.sh" >/dev/null
fi

occupant="$(lsof -ti "TCP:$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
if [[ -n "$occupant" ]]; then
  echo "Error: port $PORT is already in use by pid $occupant." >&2
  exit 1
fi

# Siri is the only voice. Kept as a variable so --tts still reads from one
# place if another backend is ever added.
TTS="${TTS:-siri}"

args=(
  serve
  --host 127.0.0.1
  --port "$PORT"
  --stt native-stt
  --llm_backend responses-api
  --tts "$TTS"
)

SIRI_VOICE="${SIRI_VOICE:-en-US-F}"
SIRI_TTS_BIN="${SIRI_TTS_BIN:-$HOME/.local/bin/siri-tts}"

args+=(
  --siri_tts_voice "$SIRI_VOICE"
  --siri_tts_binary "$SIRI_TTS_BIN"
)

args+=(
  --model_name "$MODEL"
  --responses_api_stream
  --no_responses_api_disable_thinking
  --responses_api_reasoning_effort "$REASONING_EFFORT"
  --init_chat_prompt "$PROMPT"
  --stream_batch_sentences "$BATCH_SENTENCES"
  --chat_size "$CHAT_SIZE"
  --thresh "$VAD_THRESH"
  --min_silence_ms "$VAD_MIN_SILENCE_MS"
  --min_speech_ms "$VAD_MIN_SPEECH_MS"
  --speech_pad_ms "$VAD_SPEECH_PAD_MS"
  --short_segment_merge_ms "$VAD_SHORT_SEGMENT_MERGE_MS"
)

args+=(--native_stt_locale "$STT_LOCALE")

exec "$CHATBOT_BIN" "${args[@]}"
