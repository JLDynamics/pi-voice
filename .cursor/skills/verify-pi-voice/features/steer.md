# Steer

If the user changes direction while a background task is running, Agent redirects Pi to the new goal instead of queuing a redundant job. Agent verbally confirms the redirect, Pi supersedes the earlier task, and Agent announces the final result of the redirected task.

## Sub-features
- `steering-dispatch`: A second `spawn_thinking` while active returns status steering and emits a new work ID.
- `superseded-mark`: The extension or harness marks the first job ID as superseded on the wire.
- `journal-steered`: The jobs journal records the redirection as a steered event.
- `repeat-suppression`: Dispatches repeating the same brief within 30 seconds answer already working and send nothing.

## How to get to it (user POV)
- Speak a clarifying or changed instruction while Pi is actively working on a `/voice` task.
- Type an updated request into Pi's composer before the active voice job finishes.
- A Realtime client that declares `spawn_thinking` itself can call it twice; the steer logic lives in Voice and the extension, not the backend.

## Driving it with pv.py
Preconditions: Isolated backend running; doctor reports PASS; working audio IO.
- **Voice harness steer**: Run `$SKILL/scripts/pv.py drive steer --via voice`. The harness prompts Voice to search for TODO comments and receives a first `work` event. While busy, the harness sends a redirect asking for FIXME comments instead. Voice calls `spawn_thinking` with status steering and emits a second `work` ID. The harness answers `job_update` with status `superseded` for the first job, sends status `done` and a result for the second job, and verifies spoken output. Checks: `Voice ready (unmuted: real mic and speaker open)`, `Voice audio engine started (needs working audio IO)`, `first handoff emits work`, `redirect while busy emits a new work id (steer, not stop)`, `redirect is acknowledged aloud`, `the redirected job's result is spoken`, `journal records the steer`, `no room speech or echo heard while the mic was open`, `quit exits cleanly`. Evidence: `steer.ndjson`, `jobs-journal.json`, `checks.json`.

## Gotchas
- Repeating the same brief within 30 seconds answers `already_working` and sends nothing, so redirect text must differ in content words.
- A steer redirects Pi after the current tool step completes and does not abort a running tool mid-flight.
- The steer path must emit a new work ID rather than a `stop_work` event.
