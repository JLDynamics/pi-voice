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

uv run ruff check src tests .cursor/skills
uv run ruff format --check src tests .cursor/skills   # CI checks formatting; README omits this
uv run mypy src
uv run python -c "import nltk; nltk.download('punkt_tab')"  # required for local pytest
uv run pytest -q
uv run pytest tests/test_text_prompt.py::test_text_prompt_keeps_persona_in_session_prompt -q   # single test

node --test 'pi-voice/src/*.test.ts'   # pi-voice extension tests (Node ≥22.18 strips types)
(cd pi-voice && npm ci --ignore-scripts && npm run typecheck)   # tsc against pinned Pi 1.1.0 types; CI runs it
bash macos/Voice/scripts/test.sh       # Swift runtime tests (swiftc, no Xcode project); use check(), never assert()
bash macos/Voice/scripts/build.sh      # build macos/Voice/build/Voice.app

uv run python scripts/verify-voice.py              # live text turn over the real WebSocket
uv run python scripts/verify-voice.py --research   # live bash/curl research / stray-Han TTS
```

CI (`macos-14`): runs Python checks installed with `uv sync --frozen --no-default-groups --group dev` (ruff check, ruff format --check, mypy, pytest, `uv build`, `twine check --strict`, and install smoke test `tests/install_smoke.py` plus `pip check`), Pi extension tests on Node 24, and Swift runtime tests. Pushing a `v*` tag publishes the `chatbot` package to public PyPI (`publish.yml`).

## Architecture (map)

Read the linked doc before changing that area; these are the load-bearing rules.

- **Backend pipeline** — [`docs/architecture/backend.md`](docs/architecture/backend.md). `s2s_pipeline.py` wires six `BaseHandler` threads with `queue.Queue` (mic → VAD → STT → notifier → LLM → output processor → Siri TTS). Threads plus queues, not async. New stages subclass `BaseHandler`; new STT/TTS goes through `backend_registry.py`. `run-openrouter.sh` is the real launcher and holds the tuning defaults.
- **Cancellation and turn-taking** (same doc; most bugs live here): `CancelScope` generations, `SpeculativeTurnTracker` revisions, `TurnAdmission` as the only interruption authority, and `flush_turn_queues` shared by barge-in and `response.cancel`. Full duplex: echo is handled by AEC plus VAD, not by muting.
- **Realtime service** (`api/openai_realtime/`) emulates the OpenAI Realtime API on `/v1/realtime`, with `/health` and `/v1/usage`. One client at a time. `SESSION_END` drain and quarantine rules are in the doc.
- **Server tools:** `bash` is the only one, screened and run under `sandbox-exec`. No client-side tools and no `:7860` sidecar. Pi owns web reading and memory, so do not add a second local service.
- **Staleness:** `build_info.py` fingerprints `BACKEND_SOURCES`. Only `run-browser.sh --reuse-running` restarts a stale same-checkout service; it never touches other processes.
- **Pi voice** — [`docs/architecture/pi-voice.md`](docs/architecture/pi-voice.md). A Pi 1.x extension (tested on 1.1.0, the pinned dev types). `/voice` spawns a headless `Voice.app` speaking NDJSON over stdio.
  - `contracts/pi-voice.json` is the source of truth for every cross-language seam: stdio messages, job statuses, `VOICE_HISTORY`, the `spawn_thinking`/`stop_thinking` tools, and the `[STATUS]`/`[FINAL]`/`[PI]` channels. Change it together with each side's conformance test (`scripts/check-contracts.sh --mutate`).
  - TS: `index.ts` (entry), `voice.ts` (the `Voice` class plus the `step()` dispatcher into `voice-lifecycle.ts`, `voice-transcript.ts` and `voice-job.ts`, with shared types in `voice-core.ts`), `work.ts`, `child.ts` (child process, typed wire, lease file), `history.ts` (sqlite log, startup pack) and `delegation.ts`.
  - Swift: `HeadlessBridge.swift` (stdio), `VoiceTools.swift` (`HeadlessTools`), `LiveVoiceBackend.swift` plus `+Realtime`, `+Tools` and `+PiJobs`, and `PiJobTracker.swift`.
  - Reducers (`step()`/`handleCommand()`) are pure and return `{state, effects}`. Keep new logic there so it stays testable.
  - Never call `AVAudioPlayerNode.play()` before the engine has rendered once. An audio start failure sends `error` and exits 3.
  - The shared log is `~/.config/pi-voice/history-<id>.sqlite`. Override the config dir with `PI_VOICE_CONFIG`.
  - Logs: `/tmp/voice-service-startup.log`, `/tmp/chatbot-server.log`, and `$TMPDIR/pi-voice.<pid>.stderr.log` (job lines are tagged `job <id>`). `pv.py logs --job <id>` collects them.

## Working in this checkout

- **Verification:** use the `verify-pi-voice` skill, `.cursor/skills/verify-pi-voice/SKILL.md`. `pv.py launch`, `doctor`, `drive <feature> --via backend|voice|pi`, and `logs`, and `cleanup` run an isolated backend on its own port with temp config, TMPDIR and Pi session dirs, and keep the evidence. `.cursor/skills/verify-pi-voice/references/completion-criteria.md` has boundary-specific completion criteria and safe live-check rules.

- **`AGENTS.md` is a short pointer to this file** for other coding agents. Keep facts here, not there.
- **Tests:** Python tests are flat `tests/test_*.py` plus `tests/openai_realtime/` (own `conftest.py`); Swift tests are one file, `macos/Voice/Tests/RuntimeTests.swift`; pi-voice tests are `pi-voice/src/*.test.ts`.
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
