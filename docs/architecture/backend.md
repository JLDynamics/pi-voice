# Backend architecture (`src/chatbot`)

Moved verbatim from CLAUDE.md; CLAUDE.md keeps the map and the rules.

## The pipeline is threads plus queues, not async

`s2s_pipeline.py` builds one `PipelineUnit`: six handler objects wired in a line by `queue.Queue`, each running `BaseHandler.run()` on its own thread under `ThreadManager`.

```
mic (Swift) → recv_audio → VADHandler → spoken_prompt → NativeSTTHandler → stt_output
  → TranscriptionNotifier → text_prompt → ResponsesApiModelHandler → lm_response
  → LMOutputProcessor → lm_processed → SiriTTSHandler → send_audio → speakers (Swift)
```

To add or change a stage, subclass `BaseHandler` (`baseHandler.py`): implement `setup()` and `process(item) -> Iterator[out]`. The base loop handles the `SESSION_END` soft reset, the `PIPELINE_END` sentinel, stale-input dropping, and timing. Handlers are constructed with `defer_setup=True` so models load on handler threads while `RealtimeServer` binds HTTP first — `/health` must answer before the pipeline is ready.

Backends are registered, not hardcoded: `backend_registry.py` maps `native-stt` / `responses-api` / `siri` to a config dataclass plus a factory. Each has exactly one implementation today, but new STT/TTS goes through `BackendSpec`, and `--stt` / `--tts` select it (`--llm_backend` is parsed but ignored; `s2s_pipeline.py` hardcodes `responses-api`, and a new backend also needs its name in `choices` in `arguments_classes/module_arguments.py`). Arg dataclasses live in `arguments_classes/` and are parsed by `HfArgumentParser`. `run-openrouter.sh` is the real launcher and holds the tuning defaults as env vars (`MODEL`, `VAD_*`, `STT_LOCALE`, `REASONING_EFFORT`, `SIRI_VOICE`, `SIRI_TTS_BIN`, …); it sources `~/.config/chatbot/env` (an `OPENROUTER_API_KEY` already in the shell wins) and requires that key, prefers `.venv/bin/chatbot`, refuses a busy port, builds `SpeechHelper` (`macos/SpeechHelper/scripts/build.sh`) if missing, and is supervised by `run-browser.sh`.

Messages between stages are mostly typed Pydantic models in `pipeline/messages.py` (`VADAudio`, `Transcription`, `LLMResponseChunk`, …), but VAD input is `bytes` or `(bytes, RuntimeConfig)` (`handler_types.py` `VADIn`), and TTS output is raw audio bytes/array or `AudioOutput`. `pipeline/events.py` holds internal `text_output` events that `RealtimeService` translates to Realtime wire events; custom wire events (`response.speak`, `input_audio_buffer.turn_admitted`) live in `service.py`.

## Cancellation and turn-taking (the subtle part)

Most bugs in this repo live here. Four cooperating mechanisms:

- **`CancelScope`** (`pipeline/cancel_scope.py`) — a monotonic generation counter. A barge-in bumps the generation; handler threads compare their captured generation via `is_stale()` and drop work. No locks: single writer (the asyncio router thread), many readers, GIL-atomic ints.
- **`SpeculativeTurnTracker`** (`pipeline/speculative_turns.py`) — turns carry `(turn_id, revision)`. A turn can be reopened when the user pauses and keeps talking; stale revisions are discarded rather than answered twice.
- **`TurnAdmission`** (`pipeline/turn_admission.py`) — the only interruption authority. Barge-in fires first at VAD onset (`AudioHandler.on_speech_started`) when a response is in flight and `SpeechStartedEvent.interrupt_response` is True (requiring sustained speech ≥ `min_speech_ms`, or ≥200 ms for a short final segment in `vad_handler.py`); that hands `TurnAdmission(interrupt_output=True)` to the interrupter. Onsets below that bar cancel nothing at onset, but every non-empty transcript admission (`service.py` `_admit_turn`) also retires in-flight output. Client `response.cancel` is a separate path in `websocket_router.py`. End of turn is Silero silence plus Smart Turn (`VAD/smart_turn.py`); the old word-level transcript gate is gone.
- **`PipelineTurnInterrupter`** (`api/openai_realtime/turn_interruption.py`) — flushes queues on barge-in via `flush_turn_queues`, preserving `SESSION_END` and audio sentinels; the client `response.cancel` handler in `websocket_router.py` calls the same `flush_turn_queues` (`queue_flush.py`), so both paths share one flush order.

`should_listen` is set for the whole session: this is full duplex. Echo is handled by client-side AEC plus Silero VAD, not by muting the mic.

## Realtime service layer

`api/openai_realtime/` emulates the OpenAI Realtime API over a local WebSocket so the Swift client can use a standard protocol.

- `websocket_router.py` — the FastAPI/uvicorn app, session claim/release, audio batching, and the `SESSION_END` drain contract: at 10s of waiting for `SESSION_END` to propagate back, only a warning is logged; the unit stays unclaimable until `SESSION_END` drains. A reconnect before 180s force-releases it (that attempt is rejected in `_claim_unit`, while unblocking the drain so a retry succeeds). At 180s it is quarantined (the session is unregistered and the unit remains unclaimable until the chain drains, possibly forever). A quarantine is logged as an error (`quarantining unit until the handler chain drains`) in the server log (`/tmp/chatbot-server.log` under `run-browser.sh`, overridable with `SERVER_LOG`). One client at a time: a second WebSocket connection is rejected with `session_limit_reached` (1008); connecting while models are still loading returns `server_starting` (1013). Endpoints are `/health`, `/v1/realtime`, and `/v1/usage`.
- `service.py` — `RealtimeService` owns per-session state: `Chat` history, `RuntimeConfig`, transcript admission, and the event handlers under `handlers/`. Custom protocol extensions include `response.speak` and `input_audio_buffer.turn_admitted`.
- `runtime_config.py` — `thinker` defaults from env `VOICE_THINKER` (`luna` or `pi`), overridable per session via `session.update` `thinker`. Under `pi`, no model turn is enqueued and `response.create` is rejected; the client sends `response.speak` with text to voice instead. No current client uses `pi`: Voice.app and pi-voice force `luna`; Pi results return as `[PI] …` user items that Luna speaks.

## Server-side tools

`LLM/server_tools.py` runs the model's own research *inside* one response, on the server, rather than round-tripping through the client (that was measurably slower). `bash` is the only server tool. `curl_bash.py` screens the command first (curl plus text filters, literal URLs must be public, destructive words refused), then runs it under a macOS `sandbox-exec` profile: the kernel denies file writes outside the temp directories and connections to localhost, and without `sandbox-exec` nothing runs. The screen alone is not a boundary (`python3 -c`, `curl -o` and `$(...)` pass it), and private LAN addresses reached through a `curl -L` redirect are not blocked. No client-side tools remain: `screenshot` was removed. Screen questions and computer use go through `spawn_thinking`; Pi captures the frontmost window with `screencapture` and drives the UI with its own tools. The voice session never receives the image.

The FastAPI sidecar on `:7860` that used to serve `web_search`, `read_page`, `search_chat_history`, `remember` and `forget` — and the Chrome page bridge extension with it — was removed. Pi owns web reading and memory. Do not reintroduce a second local service for them.

## Staleness contract (`build_info.py`)

Reuse and restart happen only via `run-browser.sh --reuse-running` (which Voice.app runs); plain `run-browser.sh` refuses a busy port. The service fingerprints the sources it loaded at startup and reports on `/health` whether disk has since changed. Under `--reuse-running`, a service that is `stale`, `foreign` (another checkout), `hung`, or `unknown` (only when the process is `chatbot serve`) is restarted; non-Chatbot processes are never touched. `scripts/service_state.py` classifies this from one health probe. If you change what counts as "the code", update `BACKEND_SOURCES`.
