# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Mac-first live voice assistant. Three codebases in one repo:

| Tree | Language | Role |
| --- | --- | --- |
| `src/chatbot/` | Python | The realtime voice backend (`chatbot serve`), a WebSocket server that speaks the OpenAI Realtime API protocol on `:8766` |
| `macos/` | Swift | `Voice.app` — headless, no window: the only mic capture and audio playback client in the system — and `SpeechHelper` (on-device STT subprocess) |
| `pi-voice/` | TypeScript | A Pi coding-agent extension (`/voice`) that drives a headless `Voice.app` (`VOICE_THINKER=luna`); Agent talks and hands real work to Pi via `spawn_thinking` |

Python never opens an audio device: the Swift client captures the mic, streams PCM as `input_audio_buffer.append` over the WebSocket, and plays back the returned `response.output_audio.delta` audio. Python receives the PCM, runs VAD/STT, and synthesizes TTS audio. Keep that in mind before proposing server-side audio changes.

`Voice.app` has no user interface. The SwiftUI panel (orb, transcript, settings, saved chats) and the menu bar extra were deleted once Pi became the only way in — roughly 1,800 lines that no longer ran. `VoiceApp.swift` is now a bare `NSApplication` in `.accessory` mode that attaches `HeadlessBridge` and starts listening. Do not reintroduce a window; if something needs to be shown to the user, it belongs in Pi.

## Commands

```bash
uv sync                       # install into this project's own .venv
./set-keys.sh                 # write ~/.config/chatbot/env (0600)
./run-browser.sh              # start the voice backend on :8766 (refuses a busy port)
./run-browser.sh --reuse-running  # keep current service, TERM any stale/foreign/unknown/hung Chatbot service; Voice.app launches this way

uv run ruff check src tests
uv run ruff format --check src tests   # CI checks formatting; README omits this
uv run mypy src
uv run python -c "import nltk; nltk.download('punkt_tab')"  # required for local pytest
uv run pytest -q
uv run pytest tests/test_text_prompt.py::test_text_prompt_keeps_persona_in_session_prompt -q   # single test

node --test 'pi-voice/src/*.test.ts'   # pi-voice extension (no npm script, Node ≥22.18 strips types)
bash macos/Voice/scripts/test.sh       # Swift runtime tests (swiftc, no Xcode project)
bash macos/Voice/scripts/build.sh      # build macos/Voice/build/Voice.app

uv run python scripts/verify-voice.py              # live text turn over the real WebSocket
uv run python scripts/verify-voice.py --research   # live bash/curl research / stray-Han TTS
```

CI (`macos-14`): runs Python checks installed with `uv sync --frozen --no-default-groups --group dev` (ruff check, ruff format --check, mypy, pytest, `uv build`, `twine check --strict`, and install smoke test `tests/install_smoke.py` plus `pip check`), Pi extension tests on Node 24, and Swift runtime tests. Pushing a `v*` tag publishes the `chatbot` package to public PyPI (`publish.yml`).

## Architecture

### The pipeline is threads plus queues, not async

`s2s_pipeline.py` builds one `PipelineUnit`: six handler objects wired in a line by `queue.Queue`, each running `BaseHandler.run()` on its own thread under `ThreadManager`.

```
mic (Swift) → recv_audio → VADHandler → spoken_prompt → NativeSTTHandler → stt_output
  → TranscriptionNotifier → text_prompt → ResponsesApiModelHandler → lm_response
  → LMOutputProcessor → lm_processed → SiriTTSHandler → send_audio → speakers (Swift)
```

To add or change a stage, subclass `BaseHandler` (`baseHandler.py`): implement `setup()` and `process(item) -> Iterator[out]`. The base loop handles the `SESSION_END` soft reset, the `PIPELINE_END` sentinel, stale-input dropping, and timing. Handlers are constructed with `defer_setup=True` so models load on handler threads while `RealtimeServer` binds HTTP first — `/health` must answer before the pipeline is ready.

Backends are registered, not hardcoded: `backend_registry.py` maps `native-stt` / `responses-api` / `siri` to a config dataclass plus a factory. Each has exactly one implementation today, but new STT/TTS goes through `BackendSpec`, and `--stt` / `--tts` select it (`--llm_backend` is parsed but ignored; `s2s_pipeline.py` hardcodes `responses-api`, and a new backend also needs its name in `choices` in `arguments_classes/module_arguments.py`). Arg dataclasses live in `arguments_classes/` and are parsed by `HfArgumentParser`. `run-openrouter.sh` is the real launcher and holds the tuning defaults as env vars (`MODEL`, `VAD_*`, `STT_LOCALE`, `REASONING_EFFORT`, `SIRI_VOICE`, `SIRI_TTS_BIN`, …); it sources `~/.config/chatbot/env` (an `OPENROUTER_API_KEY` already in the shell wins) and requires that key, prefers `.venv/bin/chatbot`, refuses a busy port, builds `SpeechHelper` (`macos/SpeechHelper/scripts/build.sh`) if missing, and is supervised by `run-browser.sh`.

Messages between stages are mostly typed Pydantic models in `pipeline/messages.py` (`VADAudio`, `Transcription`, `LLMResponseChunk`, …), but VAD input is `bytes` or `(bytes, RuntimeConfig)` (`handler_types.py` `VADIn`), and TTS output is raw audio bytes/array or `AudioOutput`. `pipeline/events.py` holds internal `text_output` events that `RealtimeService` translates to Realtime wire events; custom wire events (`response.speak`, `input_audio_buffer.turn_admitted`) live in `service.py`.

### Cancellation and turn-taking (the subtle part)

Most bugs in this repo live here. Four cooperating mechanisms:

- **`CancelScope`** (`pipeline/cancel_scope.py`) — a monotonic generation counter. A barge-in bumps the generation; handler threads compare their captured generation via `is_stale()` and drop work. No locks: single writer (the asyncio router thread), many readers, GIL-atomic ints.
- **`SpeculativeTurnTracker`** (`pipeline/speculative_turns.py`) — turns carry `(turn_id, revision)`. A turn can be reopened when the user pauses and keeps talking; stale revisions are discarded rather than answered twice.
- **`TurnAdmission`** (`pipeline/turn_admission.py`) — the only interruption authority. Barge-in fires first at VAD onset (`AudioHandler.on_speech_started`) when a response is in flight and `SpeechStartedEvent.interrupt_response` is True (requiring sustained speech ≥ `min_speech_ms`, or ≥200 ms for a short final segment in `vad_handler.py`); that hands `TurnAdmission(interrupt_output=True)` to the interrupter. Onsets below that bar cancel nothing at onset, but every non-empty transcript admission (`service.py` `_admit_turn`) also retires in-flight output. Client `response.cancel` is a separate path in `websocket_router.py`. End of turn is Silero silence plus Smart Turn (`VAD/smart_turn.py`); the old word-level transcript gate is gone.
- **`PipelineTurnInterrupter`** (`api/openai_realtime/turn_interruption.py`) — flushes queues on barge-in via `flush_turn_queues`, preserving `SESSION_END` and audio sentinels; the client `response.cancel` handler in `websocket_router.py` calls the same `flush_turn_queues` (`queue_flush.py`), so both paths share one flush order.

`should_listen` is set for the whole session: this is full duplex. Echo is handled by client-side AEC plus Silero VAD, not by muting the mic.

### Realtime service layer

`api/openai_realtime/` emulates the OpenAI Realtime API over a local WebSocket so the Swift client can use a standard protocol.

- `websocket_router.py` — the FastAPI/uvicorn app, session claim/release, audio batching, and the `SESSION_END` drain contract: at 10s of waiting for `SESSION_END` to propagate back, only a warning is logged; the unit stays unclaimable until `SESSION_END` drains. A reconnect before 180s force-releases it (that attempt is rejected in `_claim_unit`, while unblocking the drain so a retry succeeds). At 180s it is quarantined (the session is unregistered and the unit remains unclaimable until the chain drains, possibly forever). A quarantine is logged as an error (`quarantining unit until the handler chain drains`) in the server log (`/tmp/chatbot-server.log` under `run-browser.sh`, overridable with `SERVER_LOG`). One client at a time: a second WebSocket connection is rejected with `session_limit_reached` (1008); connecting while models are still loading returns `server_starting` (1013). Endpoints are `/health`, `/v1/realtime`, and `/v1/usage`.
- `service.py` — `RealtimeService` owns per-session state: `Chat` history, `RuntimeConfig`, transcript admission, and the event handlers under `handlers/`. Custom protocol extensions include `response.speak` and `input_audio_buffer.turn_admitted`.
- `runtime_config.py` — `thinker` defaults from env `VOICE_THINKER` (`luna` or `pi`), overridable per session via `session.update` `thinker`. Under `pi`, no model turn is enqueued and `response.create` is rejected; the client sends `response.speak` with text to voice instead. No current client uses `pi`: Voice.app and pi-voice force `luna`; Pi results return as `[PI] …` user items that Luna speaks.

### Server-side tools

`LLM/server_tools.py` runs the model's own research *inside* one response, on the server, rather than round-tripping through the client (that was measurably slower). `bash` is the only server tool. `curl_bash.py` screens the command first (curl plus text filters, literal URLs must be public, destructive words refused), then runs it under a macOS `sandbox-exec` profile: the kernel denies file writes outside the temp directories and connections to localhost, and without `sandbox-exec` nothing runs. The screen alone is not a boundary (`python3 -c`, `curl -o` and `$(...)` pass it), and private LAN addresses reached through a `curl -L` redirect are not blocked. No client-side tools remain: `screenshot` was removed. Screen questions and computer use go through `spawn_thinking`; Pi captures the frontmost window with `screencapture` and drives the UI with its own tools. The voice session never receives the image.

The FastAPI sidecar on `:7860` that used to serve `web_search`, `read_page`, `search_chat_history`, `remember` and `forget` — and the Chrome page bridge extension with it — was removed. Pi owns web reading and memory. Do not reintroduce a second local service for them.

### Staleness contract (`build_info.py`)

Reuse and restart happen only via `run-browser.sh --reuse-running` (which Voice.app runs); plain `run-browser.sh` refuses a busy port. The service fingerprints the sources it loaded at startup and reports on `/health` whether disk has since changed. Under `--reuse-running`, a service that is `stale`, `foreign` (another checkout), `hung`, or `unknown` (only when the process is `chatbot serve`) is restarted; non-Chatbot processes are never touched. `scripts/service_state.py` classifies this from one health probe. If you change what counts as "the code", update `BACKEND_SOURCES`.

### Pi voice (`pi-voice/`)

A Pi 0.86 extension, not a standalone app. `/voice` spawns one headless `Voice.app` child (`--headless`, `VOICE_THINKER=luna`) that talks NDJSON over stdio (`HeadlessBridge.swift`):
- Wire contract out: `ready`, `error` (`message`), `speech_started`, `heard` (`text`, `item_id`), `spoken_delta` and `spoken` (`text`, `item_id`), `work` (`id`, `brief`), `stop_work`.
- Wire contract in: `user` (`text`; interrupts any in-flight response first), `mute` (`muted`), `interrupt`, `result` (`id`, `speak`, `full`; Swift falls back to `speak` without `full`), `job_update` (`id`, `status`, `note?`; status is `queued`/`working`/`done`/`stopped`/`superseded`/`dropped`/`failed`, and an empty id or status is ignored), `quit`. Unknown types are ignored; stdin EOF quits. The TypeScript side of this contract is typed in `child.ts`.

Agent's headless tools are defined in `macos/Voice/Sources/Session/LiveVoiceBackend.swift`; `PiJobTracker.swift` caches results (last 10; tests can page a cached answer at 4000 characters). `index.ts` is the extension entry point (`pi.extensions` in `package.json`) and hooks `message_start` / `agent_settled`.

Agent's tools under `--headless` are `spawn_thinking` and `stop_thinking`. Casual chat stays with Agent. Real work (files, shell, research, PDFs, code, the screen, computer use) calls `spawn_thinking`, emitting a `work` event (`work` is dropped while muted, reported back as a `dropped` update so the mirror never shows a phantom job). A second dispatch while one runs steers Pi and marks the first id `superseded` on the wire rather than losing it silently; the latest brief wins. Voice.app's tool result says so: a first dispatch returns `status: started`, and a dispatch while a job is active returns `status: steering` with "This was sent to steer the current Pi task. Pi is redirected to this brief and the earlier task is replaced." so Agent says it redirected Pi instead of calling the new task queued. The brief is sent to Pi as a normal message when Pi is idle, and as a steer when busy. The message carries a trailing `[Pi voice job id: …]` marker; `message_start` binds that exact id, and `agent_settled` retrieves only assistant text following that message and before another user message. During a voice job, the extension injects a work section into Pi's system prompt and sets Pi's thinking level to off, restoring it after. When Pi finishes, the extension sends a short leading excerpt (`RESULT_MAX`, 1500 graphemes) and the complete answer over the `result` wire message. Voice.app gives Agent the excerpt as a `[FINAL]` item, caches the complete answer, and the shared log stores that answer (up to 16,000 characters) with the brief. The visible Pi message is only the brief and the job-id marker (marker last). The dated startup pack of the shared log reaches Pi's model through the extension's `context` hook, which prepends it to that job's message on each model request only: it is not drawn in Pi's chat, not shown in the "Steering:" line, and not saved in Pi's session file. Only the latest voice job carries a pack. Pi's steer does not abort a running tool; it is delivered after the current tool batch. A `spawn_thinking` whose brief differs from the running job's only in case, punctuation, order, or filler words within 30s (`PiJobTracker.activeRepeat`) answers `already_working` and sends nothing, so Pi's work in progress survives a repeat. A changed progress note after 8s is pushed as silent `[STATUS]` context with no follow-up, so Agent does not speak it. There is no repeating check-in. the handoff and `stop_thinking` stay fire-and-forget. There is no `pi_status` or `pi_results` tool. The `$TMPDIR/pi-voice.jobs.jsonl` journal records metadata, not the full answer. The extension mirrors job phases back as `job_update`: queued and working, tool start, partial output, completion or failure, and the beginning of Pi's written answer. If the user cancels, Agent calls `stop_thinking`, which emits `stop_work` and aborts only the voice job's own turn (idle, unbound, or user-typed turns are marked stopped without killing anything else); a `stop_thinking` with nothing running answers `idle` and forwards nothing. `/voice stop` ends the voice call; it does not merely cancel the current job.

Typing in Pi's composer input while voice is on routes directly to Luna as user text. Running `/voice stop` (or a second `/voice`) sends `quit`; Voice stops the launcher process it started, and the backend exits. Logs go to `/tmp/voice-service-startup.log`, `/tmp/chatbot-server.log`, and the extension's stderr log in `$TMPDIR` (`pi-voice.<pid>.stderr.log`).

Voice and Pi share one durable log, `~/.config/pi-voice/history-<id>.sqlite`, keyed by conversation id. `session.json` only remembers which id a working directory opens; `/voice resume` lists every log by id, so a different folder does not hide an earlier thread. Voice turns, the handoff brief, and the full Pi result are appended there. Progress chatter is stored and left out of what the models see.

When `/voice` starts, the extension builds one user-role startup pack from the newest rows that fit `VOICE_REPLAY_KB` (default 16KB, at most 80 turns): a warning that the background may be incomplete or stale, then `Latest` (the last user turn and what followed) and `Previous` (older rows in the window). Each line is dated in RFC3339. Voice.app injects that pack with `conversation.item.create` and does not request a reply, so it is not spoken. The same pack goes to Pi with each `spawn_thinking`, hidden from the chat (see above). `[Pi handoff]` rows are Agent's briefs and are labelled `Agent`; only `[Pi result …]` rows are labelled `Pi`. The sqlite file is the shared thread. `/voice stop`, and a child failure, write any uncommitted user transcript and partial reply into the log, framed as leftover history rather than a new request. While the log is still empty, the first start imports the last 20 final voice turns from Pi's branch once. `/voice new` starts a fresh id. Override the pack budget with `VOICE_REPLAY_KB`, the config dir with `PI_VOICE_CONFIG`.

In interactive mode, `/voice` stays in Pi's ordinary terminal transcript. Luna's spoken deltas update one visible custom entry in place; a hidden final entry saves the completed text for replay. Pi shows its own native tool calls and answers. The installed Pi 0.87.1 renderer is repaired by `scripts/patch-pi-native-fold.mjs` so voice tool calls and answers start folded and open on click, and the folded answer shows writing/ready state. The script checks the bundle hash and keeps a restore backup.

Architecture & purity: `step()` / `handleCommand()` in `voice.ts` and `work.ts` are pure reducers returning `{state, effects}`; the `Voice` class applies effects to the Pi API; `child.ts` owns the child process and the lease file (`$TMPDIR/pi-voice.lease.json` or `tmpdir()`), which blocks a second Pi window from running voice concurrently and reaps orphaned `--headless` Voice processes. Keep new logic in the reducer so it stays testable.

## Working in this checkout

- **Verification or review:** read `scripts/AGENT-VERIFICATION.md` for boundary-specific completion criteria, safe service ownership, and pending integration gaps from the session retrospective.

- **`AGENTS.md` is a short pointer to this file** for other coding agents. Keep facts here, not there.
- **Tests:** Python tests are flat `tests/test_*.py` plus `tests/openai_realtime/` (own `conftest.py`); Swift tests are one file, `macos/Voice/Tests/RuntimeTests.swift`; pi-voice tests are `voice.test.ts` and `work.test.ts`.
- **Other scripts:** `macos/Voice/scripts/install.sh` copies the build into `/Applications`; `scripts/print_system_prompt.py` prints Luna's system prompt.

- **This is a standalone repo** with its own `.venv`. This public copy begins with a fresh, independent history. The launch scripts prepend their own directory's `src` (`$HERE/src`, `$ROOT/src`) to `PYTHONPATH` — cwd does not matter, guaranteeing the backend runs *this* tree's `chatbot` rather than another copy on the machine.
- **Port 8766 is hardcoded** in `LocalServiceStarter.swift`: Voice.app only starts and supervises `:8766` (`LocalServiceStarter.manages`). A second backend can run on another port (`PORT=… ./run-browser.sh`), but pointing Voice at it (`defaults write dev.jldynamics.Voice voice.wsUrl …`) changes an installed Voice.app too, and custom URLs are not auto-started. If a health probe says `foreign`, stop — that may be another active session, not yours; then also do not run `--reuse-running` or this checkout's Voice.app / `/voice`, because they restart foreign services.
- Verify through the real user path: `/health`, `/v1/usage`, and the WebSocket events the client actually sends. Do not call internal Python setters and call it proof. Mock only at the OpenRouter boundary.
- For live verification use `scripts/verify-voice.py` (see `scripts/README.md`). Start the service with `./run-browser.sh`, check `:8766/health`, and stop only the launcher you started — never `pkill` by process name, which would take down another project's services.
- Secrets live in `~/.config/chatbot/env` (mode 600). Keep credentials and personal session data outside the repository.
- Work on a feature branch off `main` and open a PR; keep each PR to one change.

## Known constraints

- Siri TTS goes through a binary that dlopens private Apple frameworks. A macOS update can break it and there is **no fallback backend** — no voice at all until it is fixed.
- STT has no auto-detect; `STT_LOCALE` fixes the spoken language. It transcribes fillers literally.
- A Chinese Siri voice may not be installed on every machine; Chinese STT locales do exist (`STT_LOCALE=zh-CN`), but Han in replies reaches the English Siri voice as written.
