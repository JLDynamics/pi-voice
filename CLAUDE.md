# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Mac-first live voice assistant. Three codebases in one repo:

| Tree | Language | Role |
| --- | --- | --- |
| `src/chatbot/` | Python | The realtime voice backend (`chatbot serve`), a WebSocket server that speaks the OpenAI Realtime API protocol on `:8766` |
| `macos/` | Swift | `Voice.app` — headless, no window: the only mic capture and audio playback client in the system — and `SpeechHelper` (on-device STT subprocess) |
| `pi-voice/` | TypeScript | A Pi coding-agent extension (`/voice`) that drives a headless `Voice.app` (`VOICE_THINKER=luna`); Luna talks and hands real work to Pi via `ask_pi` |

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

CI (`macos-14`): runs only Python checks (ruff check, ruff format --check, mypy, pytest, `uv build`, and install smoke test `tests/install_smoke.py`). Swift (`test.sh`) and pi-voice (`node --test`) tests are local only. Pushing a `v*` tag publishes the `chatbot` package to public PyPI (`publish.yml`).

## Architecture

### The pipeline is threads plus queues, not async

`s2s_pipeline.py` builds one `PipelineUnit`: six handler objects wired in a line by `queue.Queue`, each running `BaseHandler.run()` on its own thread under `ThreadManager`.

```
mic (Swift) → recv_audio → VADHandler → spoken_prompt → NativeSTTHandler → stt_output
  → TranscriptionNotifier → text_prompt → ResponsesApiModelHandler → lm_response
  → LMOutputProcessor → lm_processed → SiriTTSHandler → send_audio → speakers (Swift)
```

To add or change a stage, subclass `BaseHandler` (`baseHandler.py`): implement `setup()` and `process(item) -> Iterator[out]`. The base loop handles the `SESSION_END` soft reset, the `PIPELINE_END` sentinel, stale-input dropping, and timing. Handlers are constructed with `defer_setup=True` so models load on handler threads while `RealtimeServer` binds HTTP first — `/health` must answer before the pipeline is ready.

Backends are registered, not hardcoded: `backend_registry.py` maps `native-stt` / `responses-api` / `siri` to a config dataclass plus a factory. Each has exactly one implementation today, but new STT/TTS goes through `BackendSpec`, and `--stt` / `--tts` select it (`--llm_backend` is parsed but ignored; `s2s_pipeline.py` hardcodes `responses-api`, and a new backend also needs its name in `choices` in `arguments_classes/module_arguments.py`). Arg dataclasses live in `arguments_classes/` and are parsed by `HfArgumentParser`. `run-openrouter.sh` is the real launcher and holds the tuning defaults as env vars (`MODEL`, `VAD_*`, `STT_LOCALE`, `REASONING_EFFORT`, `SIRI_VOICE`, `SIRI_TTS_BIN`, …); it requires `OPENROUTER_API_KEY`, builds `SpeechHelper` if missing, and is supervised by `run-browser.sh`.

Messages between stages are mostly typed Pydantic models in `pipeline/messages.py` (`VADAudio`, `Transcription`, `LLMResponseChunk`, …), but VAD input is `bytes` or `(bytes, RuntimeConfig)` (`handler_types.py` `VADIn`), and TTS output is raw audio bytes/array or `AudioOutput`. `pipeline/events.py` holds internal `text_output` events that `RealtimeService` translates to Realtime wire events; custom wire events (`response.speak`, `input_audio_buffer.turn_admitted`) live in `service.py`.

### Cancellation and turn-taking (the subtle part)

Most bugs in this repo live here. Four cooperating mechanisms:

- **`CancelScope`** (`pipeline/cancel_scope.py`) — a monotonic generation counter. A barge-in bumps the generation; handler threads compare their captured generation via `is_stale()` and drop work. No locks: single writer (the asyncio router thread), many readers, GIL-atomic ints.
- **`SpeculativeTurnTracker`** — turns carry `(turn_id, revision)`. A turn can be reopened when the user pauses and keeps talking; stale revisions are discarded rather than answered twice.
- **`TurnAdmission`** (`pipeline/turn_admission.py`) — the only interruption authority. Barge-in fires first at VAD onset (`AudioHandler.on_speech_started`) when a response is in flight and `SpeechStartedEvent.interrupt_response` is True (requiring sustained speech ≥ `min_speech_ms`, or ≥200 ms for a short final segment in `vad_handler.py`); that hands `TurnAdmission(interrupt_output=True)` to the interrupter. Onsets below that bar cancel nothing at onset, but every non-empty transcript admission (`service.py` `_admit_turn`) also retires in-flight output. Client `response.cancel` is a separate path in `websocket_router.py`. End of turn is Silero silence plus Smart Turn (`VAD/smart_turn.py`); the old word-level transcript gate is gone.
- **`PipelineTurnInterrupter`** (`api/openai_realtime/turn_interruption.py`) — flushes queues on barge-in via `flush_turn_queues`, preserving `SESSION_END` and audio sentinels; the client `response.cancel` handler in `websocket_router.py` calls the same `flush_turn_queues`, so both paths share one flush order.

`should_listen` is set for the whole session: this is full duplex. Echo is handled by client-side AEC plus Silero VAD, not by muting the mic.

### Realtime service layer

`api/openai_realtime/` emulates the OpenAI Realtime API over a local WebSocket so the Swift client can use a standard protocol.

- `websocket_router.py` — the FastAPI/uvicorn app, session claim/release, audio batching, and the `SESSION_END` drain contract: at 10s of waiting for `SESSION_END` to propagate back, only a warning is logged; the unit stays unclaimable until `SESSION_END` drains. A reconnect before 180s force-releases it (that attempt is rejected in `_claim_unit`, while unblocking the drain so a retry succeeds). At 180s it is quarantined (the session is unregistered and the unit remains unclaimable until the chain drains, possibly forever). A quarantine is logged as an error (`quarantining unit until the handler chain drains`) in the server log (`/tmp/chatbot-server.log` under `run-browser.sh`). One client at a time: a second WebSocket connection is rejected with `session_limit_reached` (1008); connecting while models are still loading returns `server_starting` (1013). Endpoints are `/health`, `/v1/realtime`, and `/v1/usage`.
- `service.py` — `RealtimeService` owns per-session state: `Chat` history, `RuntimeConfig`, transcript admission, and the event handlers under `handlers/`. Custom protocol extensions include `response.speak` and `input_audio_buffer.turn_admitted`.
- `runtime_config.py` — `thinker` defaults from env `VOICE_THINKER` (`luna` or `pi`), overridable per session via `session.update` `thinker`. Under `pi`, no model turn is enqueued and `response.create` is rejected; the client sends `response.speak` with text to voice instead. No current client uses `pi`: Voice.app and pi-voice force `luna`; Pi results return as `[PI] …` user items that Luna speaks.

### Server-side tools

`LLM/server_tools.py` runs the model's own research *inside* one response, on the server, rather than round-tripping through the client (that was measurably slower). `bash` is the only server tool. `curl_bash.py` screens the command first (curl plus text filters, literal URLs must be public, destructive words refused), then runs it under a macOS `sandbox-exec` profile: the kernel denies file writes outside the temp directories and connections to localhost, and without `sandbox-exec` nothing runs. The screen alone is not a boundary (`python3 -c`, `curl -o` and `$(...)` pass it), and private LAN addresses reached through a `curl -L` redirect are not blocked. No client-side tools remain: `screenshot` was removed, so Luna no longer sees the screen.

The FastAPI sidecar on `:7860` that used to serve `web_search`, `read_page`, `search_chat_history`, `remember` and `forget` — and the Chrome page bridge extension with it — was removed. Pi owns web reading and memory. Do not reintroduce a second local service for them.

### Staleness contract (`build_info.py`)

Reuse and restart happen only via `run-browser.sh --reuse-running` (which Voice.app runs); plain `run-browser.sh` refuses a busy port. The service fingerprints the sources it loaded at startup and reports on `/health` whether disk has since changed. Under `--reuse-running`, a service that is `stale`, `foreign` (another checkout), `hung`, or `unknown` (only when the process is `chatbot serve`) is restarted; non-Chatbot processes are never touched. `scripts/service_state.py` classifies this from one health probe. If you change what counts as "the code", update `BACKEND_SOURCES`.

### Pi voice (`pi-voice/`)

A Pi 0.86 extension, not a standalone app. `/voice` spawns one headless `Voice.app` child (`--headless`, `VOICE_THINKER=luna`) that talks NDJSON over stdio (`HeadlessBridge.swift`):
- Wire contract out: `ready`, `error`, `speech_started`, `heard` (`text`, `item_id`), `spoken`, `work` (`id`, `brief`), `stop_work`.
- Wire contract in: `user`, `mute`, `interrupt`, `result` (`id`, `speak`), `job_update` (`id`, `status`, `note?`), `quit`. Unknown types are ignored; stdin EOF quits.

Luna's tools under `--headless` are `ask_pi`, `stop_pi`, `pi_status`, and `pi_results`. When a turn needs real work Luna calls `ask_pi`, emitting a `work` event (`work` is dropped while muted, reported back as a `dropped` update so `pi_status` never shows a phantom job). A second dispatch while one runs steers Pi and marks the first id `superseded` on the wire rather than losing it silently. The brief is sent to Pi as a normal message when Pi is idle, and as a steer when busy. The message carries a trailing `[Pi voice job id: …]` marker; `message_start` binds that exact id, and `agent_settled` retrieves only assistant text following that message and before another user message. During a voice job, the extension injects a work section into Pi's system prompt and sets Pi's thinking level to off, restoring it after. When Pi finishes, the extension sends a short leading excerpt (`RESULT_MAX`, 1500 graphemes) and the complete answer over the `result` wire message. Voice.app gives Luna the excerpt as a `[PI]`-tagged item and caches the complete answer (`PiJobTracker`, last 10) for paged `pi_results` retrieval. The `$TMPDIR/pi-voice.jobs.jsonl` journal records metadata, not the full answer. The extension mirrors job phases back as `job_update`: queued and working, tool start, partial output, completion or failure, and the beginning of Pi's written answer. `pi_status` reads this local mirror; an idle status names the last terminal job and its outcome. `pi_status`/`pi_results` request a model follow-up so Luna speaks the answer, while `ask_pi`/`stop_pi` stay fire-and-forget. If the user cancels, Luna calls `stop_pi`, which emits `stop_work` and aborts only the voice job's own turn (idle, unbound, or user-typed turns are marked stopped without killing anything else); a `stop_pi` with nothing running answers `idle` and forwards nothing.

Typing in Pi's composer input while voice is on routes directly to Luna as user text. Running `/voice stop` (or a second `/voice`) sends `quit`; Voice stops the launcher process it started, and the backend exits. Logs go to `/tmp/voice-service-startup.log`, `/tmp/chatbot-server.log`, and the extension's stderr log in `$TMPDIR` (`pi-voice.<pid>.stderr.log`).

In interactive mode, `/voice` stays in Pi's ordinary terminal transcript. Luna's spoken deltas update one visible custom entry in place; a hidden final entry saves the completed text for replay. Pi shows its own native tool calls and answers. The installed Pi 0.87.1 renderer is repaired by `scripts/patch-pi-native-fold.mjs` so voice tool calls and answers start folded and open on click, and the folded answer shows writing/ready state. The script checks the bundle hash and keeps a restore backup.

Architecture & purity: `step()` / `handleCommand()` in `voice.ts` and `work.ts` are pure reducers returning `{state, effects}`; the `Voice` class applies effects to the Pi API; `child.ts` owns the child process and the lease file (`$TMPDIR/pi-voice.lease.json` or `tmpdir()`), which blocks a second Pi window from running voice concurrently and reaps orphaned `--headless` Voice processes. Keep new logic in the reducer so it stays testable.

## Working in this checkout

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
