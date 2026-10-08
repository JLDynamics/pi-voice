# Conversation

A user speaks or types to converse with Agent in natural language. Agent answers aloud with synthesized speech and updates the conversation transcript. The system detects conversational turns, processes barge-in interruptions, and records model token consumption.

## Sub-features
- `spoken-reply`: Agent synthesizes speech responding to user voice or text input.
- `typed-turn`: User types text into the composer and receives a spoken reply.
- `vad-detection`: Voice activity detection flags speech onsets and admits spoken turns.
- `token-usage`: Model token usage increments on the backend for completed turns.

## How to get to it (user POV)
- Speak into the microphone during an active `/voice` call in Pi.
- Type in Pi's composer while voice mode is active.
- Connect a Realtime API client to the backend WebSocket at `/v1/realtime`.

## Driving it with pv.py
Preconditions: Isolated backend launched; doctor reports PASS; working audio IO for Voice.
- **Voice client conversation**: Run `$SKILL/scripts/pv.py drive conversation --via voice`. The harness starts Voice muted, types `{"type":"user","text":"Hi there. Reply with just the single word hello."}` on stdin, and receives a `spoken` event. Checks: `Voice ready (muted: mic and speaker closed)`, `Voice audio engine started (needs working audio IO)`, `typed turn gets a spoken reply`, `second turn answers in context`, `backend /v1/usage changed (model really called on this run's port)`, `Voice still running at quit`, `quit exits cleanly`. Evidence: `conversation.ndjson`, `usage.json`, `checks.json`.
- **Backend WebSocket verification**: Run `$SKILL/scripts/pv.py drive conversation --via backend`. The harness runs `verify-voice.py` against `/health`, a live text turn, and spoken PCM that must raise `speech_started`. Checks: `verify-voice: voice /health`, `verify-voice: voice runs current code`, `verify-voice: voice health reports tool flag`, `verify-voice: live model reply`, `verify-voice: live mic speech_started`, `verify-voice.py summary ok and exit 0`. Evidence: `verify-voice/summary.json`, `verify-voice.stdout.txt`, `checks.json`.

## Gotchas
- Driving `--via backend` skips Voice.app's own session.update, so `spawn_thinking` and `stop_thinking` tools are omitted and Agent tool behavior is absent.
- Running `--via voice` with the laptop lid closed crashes Voice with "player did not see an IO cycle".
- Background model requests spend real OpenRouter tokens on each run.
