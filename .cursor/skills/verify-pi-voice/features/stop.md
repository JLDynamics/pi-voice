# Stop

The user can abort an active background task at any time by telling Agent to cancel it. Agent immediately calls stop to cancel Pi's work, confirms the stop aloud, and handles cancellation gracefully when no task is running.

## Sub-features
- `stop-thinking`: Agent invokes `stop_thinking` to cancel an in-flight background task.
- `stop-work-event`: Voice emits `stop_work` on stdio to signal cancellation to the extension.
- `job-stopped-update`: The harness or extension marks the active job status as stopped.
- `idle-stop`: A stop command issued when nothing is running answers aloud without forwarding `stop_work`.

## How to get to it (user POV)
- Say "cancel that", "never mind", or "stop" into the microphone during an active job on `/voice`.
- Type a stop instruction into Pi's composer while a voice task is in progress.
- A Realtime client that declares `stop_thinking` itself can call it; the stop logic lives in Voice and the extension, not the backend.

## Driving it with pv.py
Preconditions: Isolated backend running; doctor reports PASS; working audio IO.
- **Voice harness stop**: Run `$SKILL/scripts/pv.py drive stop --via voice`. The harness starts Voice and requests work. While busy, the harness sends a cancel prompt. Voice calls `stop_thinking` and emits `{"type":"stop_work"}`. The harness answers `{"type":"job_update","id":...,"status":"stopped"}`, and Agent acknowledges the stop aloud. The harness then sends a stop request with nothing running; Agent answers aloud without emitting `stop_work`. Checks: `Voice ready (unmuted: real mic and speaker open)`, `Voice audio engine started (needs working audio IO)`, `handoff emits work`, `cancel calls stop_thinking (stop_work emitted)`, `stop is acknowledged aloud`, `stop with nothing running still answers`, `stop with nothing running forwards nothing`, `no room speech or echo heard while the mic was open`, `quit exits cleanly`. Evidence: `stop.ndjson`, `drive.log`, `checks.json`.

## Gotchas
- Running `/voice stop` ends the whole voice session and child process rather than canceling just the active job.
- When no job is running, Agent must reassure the user aloud and must not emit a phantom `stop_work` event.
- Canceling a voice job aborts only the bound task and leaves idle turns or typing sessions intact.
