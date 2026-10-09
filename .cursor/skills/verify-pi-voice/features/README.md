# pi-voice verification map

This directory maps the verification surface for the pi-voice integration across its backend, headless Voice client, and Pi extension boundaries. The verification harness `pv.py` launches an isolated test environment, validates system health, drives discrete feature behaviors over supported transports, and records structured evidence on disk without mutating user configuration or processes.

## Baseline preconditions

- Launch an isolated backend using `pv.py launch`. This allocates an isolated port in the range 18766 to 18865 and never touches user port 8766.
- Run `pv.py doctor` and require all checks to report PASS before driving any feature.
- Never drive an instance or process that this run did not start.
- Driving `--via voice` and `--via pi` needs working audio IO. Doctor WARNs when the MacBook lid is closed; Voice then stops with a readable "No audio input or output is running" error and exit 3. `pv.py drive noaudio` checks that path with the lid open. Driving `--via backend` still works with the lid closed.
- Driving `--via pi` spends Pi model tokens and opens the real mic and speaker unmuted, because mute drops handoffs by design. `--via voice` handoff, steer and stop also run unmuted, because Voice skips its fallback spoken ack while muted.
- All drives spend OpenRouter tokens on the backend.

## Driving conventions

Run drives using the syntax `$SKILL/scripts/pv.py drive <feature> [--via backend|voice|pi]`, where `SKILL=.cursor/skills/verify-pi-voice`. The available feature and transport pairs are defined in the `DRIVES` table. The `backend` transport speaks the OpenAI Realtime WebSocket protocol directly. The `voice` transport drives headless Voice over standard IO NDJSON. The `pi` transport drives an interactive Pi session inside a pseudo-terminal. Always run `$SKILL/scripts/pv.py cleanup` when done to terminate run processes and delete scratch files while preserving logs and evidence.

## Proof and skip reporting

Evidence lands in `/tmp/pv-verify/<run-id>/evidence/<feature>-<via>-<stamp>/`. Each run produces `checks.json` with a final verdict of VERIFIED or NOT VERIFIED, a sequential `drive.log`, and per-drive transcript files. A drive fails immediately if any required check fails or if an unhandled exception occurs. If a prerequisite check fails, the harness records the failure in `checks.json` and skips subsequent steps rather than reporting false completion.

## Feature entry contract

Every feature document starts with a summary of user-visible behavior, followed by four mandatory sections: Sub-features, How to get to it (user POV), Driving it with pv.py, and Gotchas. Preconditions must be verified before driving. Each driving step pairs user actions with the exact `pv.py` command or wire message, the expected check names printed to stdout, and the persistent evidence files written to disk.

## Features

- [Conversation](conversation.md): Live voice dialogue, user text input turns, and speech VAD detection.
- [Handoff](handoff.md): Delegating tasks from Voice to Pi via `spawn_thinking` and receiving spoken results.
- [Steer](steer.md): Redirecting an active Pi background job to a new task brief.
- [Stop](stop.md): Canceling an active task with `stop_thinking` and handling idle stop requests.
- [Memory](memory.md): Preserving context across calls via the startup pack and history recall.
