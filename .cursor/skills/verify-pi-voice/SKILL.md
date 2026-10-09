---
name: verify-pi-voice
description: Drive pi-voice the way a user does and capture proof. Launches an isolated voice backend on its own port, then drives typed turns through the backend WebSocket, headless Voice.app over NDJSON stdio, or real Pi with this checkout's /voice extension, using temp config, TMPDIR and Pi session dirs. Use before claiming a conversation, spawn_thinking handoff, steer, stop_thinking, or shared-memory change works, or when a pi-voice behavior needs reproducing.
---

# Verify pi-voice

pi-voice has no window. The user surface is speech (or typing in Pi's composer) during a `/voice` call. Three layers can be driven headlessly, from cheapest to most complete:

| `--via` | What runs | Proves | Needs |
| --- | --- | --- | --- |
| `backend` | the Python backend over its real WebSocket | model turns, VAD, startup-pack injection | nothing extra |
| `voice` | headless `Voice.app` from this checkout (muted for conversation and memory; unmuted for handoff, steer and stop, which check the spoken ack); the harness plays the Pi extension on stdio | Agent's tools (`spawn_thinking`, `stop_thinking`), `[FINAL]`/`[STATUS]` handling, job journal | working audio IO |
| `pi` | real `pi` in a PTY with this checkout's extension and `/voice` | the whole chain: handoff to Pi, result spoken, shared log, reconnect pack | working audio IO, Pi model tokens, unmuted mic and speaker |

Every drive is a real path. Nothing calls internal setters; the only stand-in is the harness replying as Pi on the `voice` layer, which is the NDJSON contract Voice.app already isolates.

```bash
SKILL=.cursor/skills/verify-pi-voice     # from the repo root (or a worktree root)
$SKILL/scripts/pv.py --help
```

## Launch

```bash
uv sync                                  # once per checkout or worktree
$SKILL/scripts/pv.py launch              # add --build after Swift changes, --port N to pick a port
```

`launch` creates `/tmp/pv-verify/<run-id>/` (override the root with `PV_VERIFY_ROOT`), writes a fixture project, builds `Voice.app` if it is missing, and starts `./run-browser.sh` with `PORT=<18766+>` and `SERVER_LOG=<run>/logs/server.log` in its own process group. Ready means `/health` answers `"ready": true`; launch prints that payload and exits. It refuses port 8766 and any busy port.

Isolation it sets up:

- Port: its own (18766 and up). The user's Voice.app and backend stay on 8766 and are never probed beyond an informational `lsof`.
- Voice.app: `scratch/bin/voice` execs this checkout's build with `-voice.wsUrl ws://127.0.0.1:<port>/v1/realtime`. That argument lives only in the process. `defaults write` would retarget the user's installed Voice.app, so never use it.
- Shared memory: each Pi drive uses `PI_VOICE_CONFIG=<run>/scratch/pi/<drive>/cfg` and each seeded pack `<run>/scratch/seed/<drive>` (never `~/.config/pi-voice`), so one drive's conversation never becomes the next drive's startup pack. `scratch/cfg` is for manual runs via `pv.py env`.
- Lease, stderr logs, job journal: `TMPDIR=<run>/scratch/tmp/`.
- Pi: `--session-dir <run>/scratch/pi/<drive>/sessions --offline --no-mcp -na -ne -ns -np -e <checkout>/pi-voice/src/index.ts`, started in the fixture folder. Pi still reads its own auth and default model from `~/.pi/agent`. The harness writes nothing there, and `--offline --no-mcp` skip Pi's startup network and MCP work, but Pi may still touch its own caches.
- Secrets: the launcher sources `~/.config/chatbot/env` itself. Never print that file, process environments (`ps -E`), or auth files.

## Doctor

```bash
$SKILL/scripts/pv.py doctor
```

Read-only. Exit 0 only when every check passes: launcher alive and is `run-browser.sh`, the port's listener is in this run's process group, `/health` ready, `source` is this checkout and not `stale`, the Voice build is newer than every Swift source, the wrapper targets this port, `pi` is on PATH, scratch dirs exist, no stray drive children. It prints `WARN lid is closed` when the MacBook is in clamshell mode (see Gotchas). Run it first, and again whenever a drive fails strangely. Results are saved as `evidence/doctor-<stamp>.json`.

## Drive

```bash
$SKILL/scripts/pv.py drive conversation --via backend   # cheapest live proof
$SKILL/scripts/pv.py drive memory --via backend
$SKILL/scripts/pv.py drive conversation                 # --via voice is the default
$SKILL/scripts/pv.py drive handoff
$SKILL/scripts/pv.py drive steer
$SKILL/scripts/pv.py drive stop
$SKILL/scripts/pv.py drive memory
$SKILL/scripts/pv.py drive handoff --via pi             # full chain, real Pi
$SKILL/scripts/pv.py drive memory --via pi              # /voice, stop, /voice again
$SKILL/scripts/pv.py drive noaudio                      # no audio IO: readable error, exit 3, no crash
$SKILL/scripts/pv.py drive pack                         # 3 calls: first live turn after a pack is answered
```

Each drive prints `CHECK PASS|FAIL <name>` lines and ends with `VERIFIED` or `NOT VERIFIED` (exit 0 or 1). The recipes, stable handles, and per-feature proof live in [`features/`](features/README.md). Read the matching file before driving, and drive every entry point the file lists before calling a feature verified.

The stable handles are the NDJSON wire types on Voice's stdio (`ready`, `spoken`, `work`, `stop_work`, `error` out; `user`, `mute`, `job_update`, `result`, `quit` in; see `pi-voice/src/child.ts`), Realtime events on the WebSocket, Pi session entries (user messages ending in `[Pi voice job id: <id>]`; Pi writes its session file only after its first assistant message, so Agent-only turns never show there), and rows in `history-<id>.sqlite` (what Agent said aloud is `role=assistant, kind=voice`, written at once).

For an ad hoc drive, `eval "$($SKILL/scripts/pv.py env)"` exports the run's `PI_VOICE_CONFIG`, `TMPDIR`, `VOICE_BIN`, `PV_PORT`, `PV_PROJ` and `PV_SESSIONS`. Start your own processes from that shell and stop them yourself.

## Evidence

Each drive writes `<run>/evidence/<feature>-<via>-<stamp>/`:

- `checks.json`: verdict plus every check with its detail.
- `drive.log`: the timed action log, every NDJSON line sent and received.
- `<label>.ndjson`: the raw Voice transcript (`voice` layer).
- `jobs-journal.json`, `usage.json`, `startup-pack.json`, `recall.json`, `verify-voice/summary.json`: side effects read back after the action.
- `pi-pty.log`, `pi-session.jsonl`, `history-rows.json`, `voice-history.*.json` (`pi` layer): Pi's terminal, the session file, the shared log rows, and the startup pack each Voice child received.

Proof standards: drive the user path (typed or spoken turn, `/voice`), capture the action and the resulting state, and read the side effect back from its store (journal file, sqlite rows, `/v1/usage`) instead of trusting a reply. A spoken reply that merely exists is not proof; the checks match its content. Report the `--via` layer with every claim: `backend` evidence does not prove Voice.app or Pi behavior. All content is synthetic (fixture folder, invented project name). Never use the user's real conversation history as evidence.

## Cleanup

```bash
$SKILL/scripts/pv.py logs --job <work-id>    # bundle every log; list lines naming that job id
$SKILL/scripts/pv.py cleanup                   # bundles logs first, then removes scratch
```

Stops only what the run started: drive children recorded in `state.json` (by process group, after re-checking the command), any Voice pid in the run's lease file whose command is this checkout's build, and the launcher's process group (`run-browser.sh`, `run-openrouter.sh`, `chatbot serve`). It never kills by process name. It waits for the port to free, deletes `scratch/`, and keeps `logs/` and `evidence/`, writing `evidence/cleanup-<stamp>.json` with what it stopped and what it kept. Run it after every run, including failed ones.

## Helpers

- `scripts/pv.py`: `launch`, `doctor`, `drive <feature> [--via backend|voice|pi]`, `env`, `cleanup`; every subcommand takes `--run <run-id>` (default: the latest run). Standard library only.
- `scripts/seed-history.mjs`: writes a synthetic earlier call into a scratch cabinet with the extension's own `Conversation` code, reopens it, and prints the startup pack. `PI_VOICE_CONFIG=<dir> PV_PROJ=<folder> PV_SRC=pi-voice/src node $SKILL/scripts/seed-history.mjs`.
- `scripts/ws_recall.py`: replays a pack on the backend WebSocket the way Voice.app does (no reply requested), then asks one question. `.venv/bin/python $SKILL/scripts/ws_recall.py <ws_url> <pack.json> <question> <out.json>`.

## Completion criteria

Before saying a change is done, read `references/completion-criteria.md`. It says which boundary each check proves (history chain, delegation cases, safe live checks), and you must name any boundary you did not exercise.

## Gotchas

- Lid closed means no audio IO. Headless Voice then sends `error` "No audio input or output is running. ..." and exits 3 (before U2 it aborted with `'player did not see an IO cycle'`), so `--via voice`/`--via pi` report NOT VERIFIED with that reason. Use `--via backend`, or open the lid. Muting does not avoid it. `drive noaudio` proves this path with the lid open via the per-process `-voice.simulateNoAudioIO YES` argument.
- `--via pi` must stay unmuted: work is dropped while muted, by design, with no spoken ack. `--via voice` handoff, steer and stop are unmuted too: while muted Voice skips its fallback ack when the model calls a tool without speaking, so an ack check would only pass by luck. Unmuted drives open the real mic and play speech aloud; run them in a quiet room. They fail `no room speech or echo heard while the mic was open` if the mic picked anything up.
- `--via backend` skips Voice.app's own `session.update`, so it cannot show tool calls.
- Model replies vary. Checks match loose content (`hello`, `4|four`, file names). One failing content check is a reason to read `drive.log`, not to loosen the regex.
- Port 8766 belongs to the user. A launcher or `/voice` from any checkout on 8766 restarts `stale` or `foreign` services; this skill never uses it.
- A Swift change needs `launch --build`; doctor fails on a stale build. A backend change needs a fresh `launch` (doctor fails on `stale`).
- Tracing one handoff: the work id appears in the journal (`pi-voice.jobs.jsonl`), in Voice's stderr log as `[PiJob] {...}` lines, and in the same log as extension lines `[pi-voice] job <id> ...`; ignored stdio lines are logged there once per reason. `pv.py logs --job <id>` collects them.
