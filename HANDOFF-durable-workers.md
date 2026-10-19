# Handoff: pi-voice durable workers

Repo: `~/Documents/pi-voice`. Branch `main`, **34 files uncommitted** (29 modified, 5 new).
Baseline commit: `7edf4fc`.

## What was changed

### 1. Parallel workers (`pi-durable`), on by default

New: `pi-voice/src/worker/host.mjs` — a worker host process. Opens one `pi-durable`
`Harness`, gives each job its own conversation (`tx.createConversation`), and runs
N jobs concurrently in one process.

New: `pi-voice/src/worker/config.mjs` — model/credential/instruction/policy
resolution, kept separate so it is testable without a harness.

New: `pi-voice/src/worker.ts` — the extension's client: `WorkerHost`, NDJSON
decoder, event mapping, `durableWorkers()`.

Protocol added to `contracts/pi-voice.json` as a new `worker` section (contract
`version` is now 2; `tests/test_contract.py` asserts 2). NDJSON:
- in: `spawn`, `stop`, `status`, `list`, `quit`
- out: `ready`, `progress`, `job`, `result`, `status`, `list`, `error`

Default switched: `durableWorkers(env)` returns `true` when `VOICE_WORKERS` is
unset. `VOICE_WORKERS=single|steer|pi|off` restores the old single-session path.
`macos/Voice/Sources/Session/VoiceTools.swift` `HeadlessTools.parallelWorkers`
mirrors it (same four opt-out values).

Reducer changes:
- `pi-voice/src/work.ts` `openJob`: when `state.worker` is set, emits
  `workerSpawn` instead of `sendWork`, and does **not** mark the previous job
  `superseded`.
- `pi-voice/src/voice-job.ts`: handles `workerJob`, `workerResult`,
  `workerStatus`, `workerList`.
- `pi-voice/src/voice-lifecycle.ts`: handles `workerReady` (accepted while
  `connecting`, because the host readies during spawn) and `workerError`.
- `PiJobTracker.handoffAck(_:steering:parallel:)` returns
  `status: "started_alongside"` in parallel mode.

Failure behaviour: if the host cannot start or exits, `startWorkersIfEnabled`
catches it and emits `workerError`; the reducer warns, drops the `worker` marker,
and later jobs use the single session. The voice call does not end.

### 2. `check_thinking` (read-only status verb)

Third headless tool alongside `spawn_thinking`/`stop_thinking`. Sends a `status`
line to the extension; the extension answers with a `snapshot` payload.
- Single-session path: `snapshotFor()` in `pi-voice/src/work.ts`.
- Durable path: host's own job record (`workerAsk` → `workerStatus`).
- `Voice.app` falls back to `PiJobTracker.snapshotPayload` if the extension does
  not reply within 2.5 s.
Documented in `src/chatbot/LLM/voice_prompt.py` (`VOICE_PI_HANDOFF`).

### 3. Per-job thinking effort

`spawn_thinking(brief, effort?)`, `effort` ∈ `off|minimal|low|medium|high`,
default `off`. Carried on the `work` line, stored on the job, applied by
`applyJobEffort` in `pi-voice/src/voice.ts` when the job's turn starts, restored
on job end. `restoreThinking` refuses to overwrite a level the user changed
mid-job (compares current level against the one the job set). Unknown effort
strings are refused at three points (Swift `spawnEffort`, TS `asEffort`,
`PiJobTracker.ask`).

### 4. Structured progress

`progress` wire message replaces the prose-only note: `{id, tool, phase,
summary, active}`. Rendered by `PiJobTracker.activityLabel` and
`activityNote` (TS) as "Pi is running a command (git status) + 1 more".
Two feeders share `piProgressGeneration` so a note is never injected twice; both
gated on `canInjectContext`.

### 5. Worker tools (9)

`bash, read, write, edit, grep, ls, find, skill, screenshot`.
- `skill` lists/loads `SKILL.md` from `~/.pi/agent/skills`, `~/.agents/skills`,
  and a project `.pi/skills` (`findSkills` in `config.mjs`).
- `screenshot` captures the frontmost layer-0 window via `osascript` +
  `screencapture`, returns a PNG image content block, falls back to whole screen.

### 6. Write policy (no prompts)

`VOICE_WORKER_SANDBOX` = `workspace-write` (default) | `read-only` |
`unrestricted`; `VOICE_WORKER_WRITE_ROOTS=/a:/b` adds roots.
- `write`/`edit` refuse via `allowWrite()` → `writeRefusal()` (realpath-checked).
- `bash` runs under `/usr/bin/sandbox-exec` with an SBPL profile from
  `sandboxProfile()` (denies `file-write*` outside roots + temp dirs).

### 7. Model, credentials, instructions

- Model: `VOICE_WORKER_PROVIDER`/`VOICE_WORKER_MODEL` → else Pi's
  `~/.pi/agent/settings.json` default + its entry in `~/.pi/agent/models.json` →
  else OpenRouter fallback. `PI_HOME` overrides the config directory.
  `pinProvider` stamps `api` and `baseUrl` onto each model (models.json keeps
  them at provider level).
- Credentials: `loadCredentials()` reads `$CHATBOT_ENV` or
  `~/.config/chatbot/env`; existing env wins.
- Instructions: `AGENTS.md` + `CLAUDE.md` from cwd as a `project_instructions`
  prompt section (`instructionsText`).

### 8. Visibility and persistence

- Persistence: host opens `openNodeJsonlStorage()` at
  `~/.config/pi-voice/workers/run-<iso>-<pid>/` (`VOICE_WORKER_STATE`
  overrides); falls back to `MemoryStorage` if unwritable. `ready` carries
  `state: <path>`.
- Visibility: `openJob` emits `upsertFace` (`work:<id>`, brief) and
  `voice-job.ts` emits `upsertFace` (`work:<id>:done`, `done — N chars` /
  `failed — <reason>` / `stopped — …`). These are `pi-voice-face` custom entries,
  which Pi excludes from model context.

### 9. Supporting changes

- `pi-voice/src/voice-render.ts` (new): entry renderer extracted from `voice.ts`
  to keep it under the 600-line budget (`tests/test_docs_drift.py`).
- `pi-voice/package.json`: test script now includes `src/worker/*.test.ts`.
- `CLAUDE.md`, `README.md`, `docs/architecture/pi-voice.md`: updated.
- `scripts/check-contracts.sh`: mutation entries added for the new seams.
- `.cursor/skills/verify-pi-voice/scripts/pv.py`: pi-layer drives pin
  `VOICE_WORKERS=single`; `PV_VIA=durable` drives the default path;
  `PV_PI_MODEL` pins a model for one drive.
- New `scripts/verify-durable-workers.mjs`: `--jobs N` runs N concurrent jobs
  against the real model, asserts distinct answers, live overlap, tool progress.

## Verification status at handoff

Last full run, all green:
- `bash macos/Voice/scripts/test.sh` — pass
- `node --test 'pi-voice/src/*.test.ts' 'pi-voice/src/worker/*.test.ts'` — 186/186
- `cd pi-voice && npm run typecheck` — clean
- `.venv/bin/python -m pytest -q` — 626 passed
- `.venv/bin/python -m mypy src` — clean
- `.venv/bin/ruff check src tests .cursor/skills` / `format --check` — clean
- `node scripts/verify-durable-workers.mjs --jobs 2` and `--jobs 4` — 6/6 each
- pv.py drives `handoff --via pi`, `check --via pi`, `check --via voice`,
  `steer --via voice`, `stop --via voice` — VERIFIED

Pi's session model at handoff time: `magpie/opencode-zen/deepseek-v4.1-flash-free`
(an earlier model, `workbuddy-ai/deepseek-v4.1-flash`, hit HTTP 429).

## Work to be done

### 1. Commit

34 files are uncommitted on `main`. Split by concern if preferred: read-only
status verb (`check_thinking`), per-job effort, structured progress, parallel
workers, worker tools + sandbox, visibility + persistence.

### 2. Browser capability in the worker

The worker has no browser tool. On the durable path a request such as "open
x.com and list trending topics" depends on the model choosing to load the
`pi-browser` skill and driving it from `bash`. This is untested. Either:
- prove the skill path works end to end, or
- add a browser tool to `pi-voice/src/worker/host.mjs`.

### 3. Crash-resume (untested)

Worker state is now on disk via `openNodeJsonlStorage`, so resume is possible,
but no test kills a host mid-job and reopens the storage. `pi-durable`'s README
documents resume; the pi-voice path is unverified.

### 4. Pi session file on the durable path

Pi never runs its own model turn on the durable path, so it never writes a
session `.jsonl`; `upsertFace` entries exist only in the live chat. Verify this
is acceptable or persist a record to Pi's session as well.

### 5. `pi-durable` is Experimental

Upstream README states the API changes without notice (pin: `1.2.0`). Any
upgrade needs a re-run of `scripts/verify-durable-workers.mjs` and the drives.

## Known traps (found by failing during this work)

- `createConversation({ agent: { model } })` is ignored. Set the model with
  `configure({ model })` after creation, or every submission settles
  `unanswered: no_model`.
- `ownership` is required: `{kind:"ownerless"}` or `{kind:"task", taskId}`.
- The assistant message is `entry.model[0].content`, not `entry.message`.
- `models.json` keeps `api`/`baseUrl` at provider level; each model needs them
  stamped or the provider throws `no API implementation for undefined`.
- `createProvider` takes `auth: { apiKey: { … } }` (nested), and `check` must
  return `{type: "api_key"}`, not a boolean.
- A faux tool-call message needs `stopReason: "toolUse"` or the turn ends before
  the tool runs (relevant only to faux-provider tests).
- The worker host must read `~/.config/chatbot/env`: the Pi process has no
  provider environment of its own.
- `sandboxProfile` allows `/private/tmp` and `/private/var/folders`, so a write
  to `/tmp` succeeds by design; test escapes against `$HOME` or `/etc`.
- pi-layer drives must run with `VOICE_WORKERS=single` (the default is now
  durable, and those drives assert the job reaches Pi's session).
- `pv.py` drives share one TMPDIR per run, so the job journal
  (`$TMPDIR/pi-voice.jobs.jsonl`) accumulates across drives; filter by
  timestamp or a baseline count.

## Commands

```bash
cd ~/Documents/pi-voice
bash macos/Voice/scripts/test.sh
node --test 'pi-voice/src/*.test.ts' 'pi-voice/src/worker/*.test.ts'
(cd pi-voice && npm run typecheck)
.venv/bin/python -m pytest -q
node scripts/verify-durable-workers.mjs --jobs 4

SKILL=.cursor/skills/verify-pi-voice
.venv/bin/python $SKILL/scripts/pv.py launch --build
.venv/bin/python $SKILL/scripts/pv.py doctor
.venv/bin/python $SKILL/scripts/pv.py drive handoff --via pi
.venv/bin/python $SKILL/scripts/pv.py cleanup

# job records
ls ~/.config/pi-voice/workers/
rg '"toolName"' ~/.config/pi-voice/workers/run-*/main.jsonl
```
