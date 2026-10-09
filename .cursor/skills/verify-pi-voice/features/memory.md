# Memory

Agent retains conversational context across session restarts. When starting a voice call, prior turns and Pi results are injected into the session without being read aloud, allowing Agent to recall established project facts and previous handoff results when queried.

## Sub-features
- `startup-pack`: Seeds one user-role item with stale warning, Latest and Previous sections, and RFC3339-dated lines.
- `turn-attribution`: Labels `[Pi handoff]` rows as Agent and `[Pi result` rows as Pi.
- `silent-seed`: Voice injects history using `conversation.item.create` with no reply requested.
- `cross-session-recall`: Agent recalls remembered facts and answers questions without re-running tools.

## How to get to it (user POV)
- Refer to facts or results from earlier calls while speaking during a `/voice` session.
- Type questions about previous tasks into Pi's composer after reconnecting with `/voice`.
- Send historical conversation items over the backend WebSocket when creating a session.

## Driving it with pv.py
Preconditions: Isolated backend running; doctor reports PASS; each drive's `PI_VOICE_CONFIG` is its own dir under `scratch/` (`scratch/seed/<drive>` for the seeded pack, `scratch/pi/<drive>/cfg` via Pi).
- **Backend WebSocket replay**: Run `$SKILL/scripts/pv.py drive memory --via backend`. The harness seeds history using `seed-history.mjs` and runs `ws_recall.py`. The pack is injected with `conversation.item.create` without requesting a reply. Checks: `pack is one user turn with Latest and a Pi result`, `pack injected without a reply (no response before the question)`, `Agent recalls the earlier call from the pack` (expects "Pelican"), `recall used no tool (answered from the pack)`. Setup failures print `startup pack built by the extension code` or `ws_recall.py wrote recall.json` as FAIL instead. Evidence: `startup-pack.json`, `recall.json`, `checks.json`.
- **Voice harness replay**: Run `$SKILL/scripts/pv.py drive memory --via voice`. The harness seeds the startup pack into `VOICE_HISTORY`, starts Voice, and asks a recall question. Checks: `pack is one user turn with Latest and a Pi result`, `Agent recalls the earlier call from the pack`, `recall did not hand off to Pi` (a seeding failure prints `startup pack built by the extension code` as FAIL). Evidence: `startup-pack.json`, `memory.ndjson`, `checks.json`.
- **Pi session recall**: Run `$SKILL/scripts/pv.py drive memory --via pi`. Call 1 states that the project is named Pelican Harbor and stops. Call 2 starts, saves `voice-history.*.json` with the fact and `Latest:`, and Agent recalls the name. Checks: `call 1: /voice started headless Voice from this checkout`, `call 1: Agent answers`, `call 1: /voice stop ends the Voice child`, `call 2: /voice started headless Voice from this checkout`, `call 2: Voice child got one dated user-role pack with call 1`, `call 2: Agent recalls call 1 after reconnect`, `call 2: /voice stop ends the Voice child`. Evidence: `voice-history.*.json`, `history-rows.json`, `pi-pty.log`, `pi-session.jsonl`, `checks.json`.

## Gotchas
- Never use `~/.config/pi-voice` or real user conversations as evidence; the harness points `PI_VOICE_CONFIG` at a per-drive dir under the run's `scratch/`, so drives never share memory.
- The startup pack must produce zero `response.created` events before the user question is asked.
- In history rows, `[Pi handoff]` must be labelled Agent and `[Pi result` must be labelled Pi.
