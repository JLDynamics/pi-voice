# Handoff

When the user asks for substantial coding or workspace actions, Agent delegates the task to Pi. Agent speaks an immediate verbal acknowledgment, Pi runs the task in the background, and Agent speaks the final result summary back to the user once Pi finishes.

## Sub-features
- `spawn-thinking`: Agent invokes `spawn_thinking` and emits a work event with an ID and brief.
- `spoken-ack`: Agent speaks a single verbal acknowledgment confirming handoff.
- `status-mirror`: Job status updates progress through working to done.
- `spoken-result`: Agent speaks the `[FINAL]` excerpt returned from Pi.
- `shared-log`: The shared SQLite database logs briefs and completed Pi results.

## How to get to it (user POV)
- Ask Agent by voice to inspect files, edit code, or run terminal tools during a `/voice` call.
- Type a complex task into Pi's composer while voice mode is active.
- A Realtime client on the backend WebSocket that declares `spawn_thinking` in its own `session.update`; the handoff logic lives in Voice and the extension.

## Driving it with pv.py
Preconditions: Isolated backend running; doctor reports PASS; working audio IO; a quiet room (both paths run unmuted).
- **Voice harness handoff**: Run `$SKILL/scripts/pv.py drive handoff --via voice`. The harness, unmuted, asks Voice to have Pi list the files in the current project folder. Voice calls `spawn_thinking`, which emits `{"type":"work","id":...,"brief":...}` and speaks an acknowledgment once. The harness replies with `{"type":"job_update","id":...,"status":"working"}`, then status `done`, and sends `{"type":"result","id":...,"speak":...,"full":...}`. Voice speaks the `[FINAL]` excerpt. Checks: `Voice ready (unmuted: real mic and speaker open)`, `Voice audio engine started (needs working audio IO)`, `spawn_thinking emits a work event with id and brief`, `handoff is acknowledged aloud`, `one handoff, not repeated by the ack`, `[FINAL] result is spoken back`, `job journal written under the run's TMPDIR`, `no room speech or echo heard while the mic was open`, `quit exits cleanly`. Evidence: `handoff.ndjson`, `jobs-journal.json`, `checks.json`.
- **Pi terminal handoff**: Run `$SKILL/scripts/pv.py drive handoff --via pi`. The harness types a file-listing prompt in Pi. Pi receives a user message ending with `[Pi voice job id: <id>]` (marker last). Agent speaks Pi's result. Checks: `call: /voice started headless Voice from this checkout`, `typed text reaches Agent and is spoken`, `Pi receives the job message, marker last`, `Pi's result is spoken back by Agent`, `shared log stores the [Pi handoff] brief`, `shared log stores the full [Pi result]`, `job journal lands in the isolated TMPDIR`, `call: /voice stop ends the Voice child`. Evidence: `pi-pty.log`, `pi-session.jsonl`, `history-rows.json`, `jobs-journal.json`, `checks.json`.

## Gotchas
- Work is dropped while muted by design with no spoken acknowledgment, so the Pi test path must stay unmuted.
- The trailing marker `[Pi voice job id: <id>]` must remain strictly last in the message to bind results cleanly.
- The job journal `pi-voice.jobs.jsonl` is written under the run's isolated `TMPDIR` (`scratch/tmp`), not the user home directory.
