# AGENTS.md

Detailed repo guide: `CLAUDE.md`. Read it before changing pipeline, turn-taking, tools, or `pi-voice/`. `README.md` covers install/run and env vars.

## Layout (three codebases, one repo)

- `src/chatbot/` (Python): realtime voice backend (`chatbot serve`), WebSocket server speaking OpenAI Realtime protocol on `:8766`. Never opens an audio device.
- `macos/` (Swift, no Xcode project): headless `Voice.app` (only mic capture/playback client) + `SpeechHelper` (on-device STT subprocess). App has no window by design — do not reintroduce one; user-facing output belongs in Pi.
- `pi-voice/` (TypeScript): Pi 0.86 extension (`/voice`) driving headless `Voice.app`. No npm scripts; Node ≥22.18 strips types directly.

## Commands

```bash
uv sync                                        # install into .venv
./set-keys.sh                                  # write ~/.config/chatbot/env (0600)
./run-browser.sh                               # start backend on :8766; refuses busy port
./run-browser.sh --reuse-running               # Voice.app launch mode: restarts stale/foreign/unknown/hung Chatbot services only

uv run ruff check src tests
uv run ruff format --check src tests           # CI enforces; README omits
uv run mypy src
uv run python -c "import nltk; nltk.download('punkt_tab')"  # once, else local pytest fails
uv run pytest -q
uv run pytest tests/test_text_prompt.py::test_text_prompt_keeps_persona_in_session_prompt -q  # single test

node --test 'pi-voice/src/*.test.ts'           # pi-voice extension
bash macos/Voice/scripts/test.sh               # Swift runtime tests (swiftc)
bash macos/Voice/scripts/build.sh              # build macos/Voice/build/Voice.app

uv run python scripts/verify-voice.py            # live text turn over real WebSocket (needs ./run-browser.sh up)
uv run python scripts/verify-voice.py --research # live server bash/curl research; services must be up
```

CI (`macos-14`) runs Python checks only (ruff, format, mypy, pytest, `uv build`, install smoke). Swift and pi-voice tests are local-only. `v*` tag publishes `chatbot` to PyPI.

## Gotchas

- Port `8766` is hardcoded in `LocalServiceStarter.swift`. If `/health` reports `foreign`, that may be an active session from another checkout — do not run `--reuse-running`, this checkout's Voice.app, or `/voice`. Never `pkill` by name; stop only the launcher you started.
- Launch scripts prepend their own `src` to `PYTHONPATH`; cwd does not matter, and the backend always runs this tree's code.
- Pipeline is threads + `queue.Queue`, not async: six `BaseHandler` subclasses in `s2s_pipeline.py`, each on its own thread. New stage = subclass `BaseHandler` (`setup()` + `process()`); handlers use `defer_setup=True` so `/health` answers before models load.
- Turn-taking bugs live in four cooperating pieces (`cancel_scope.py`, speculative turn revisions, `turn_admission.py` as sole interruption authority, `turn_interruption.py` queue flush). Read `CLAUDE.md` architecture section before touching them.
- Backend selection goes through `backend_registry.py` + arg dataclasses in `arguments_classes/`; tuning defaults live as env vars in `run-openrouter.sh`. `Audio` flows as raw bytes/arrays between VAD and TTS, typed Pydantic models elsewhere (`pipeline/messages.py`).
- Server `bash` tool runs under macOS `sandbox-exec` (temp-dir writes only, no localhost); the `curl_bash.py` screen is not a security boundary (`python3 -c`, `curl -o`, `$(...)`, `curl -L` redirects to LAN pass it). Under Pi, Luna has no bash — research/memory/web go via `ask_pi`. Luna's other headless tools are `stop_pi`, `pi_status` (local job mirror in `PiJobTracker`), and `pi_results` (paged re-read of cached results); no further client-side tools remain. Do not reintroduce the removed `:7860` sidecar or others (screenshot etc.).
- `pi-voice/`: `voice.ts`/`work.ts` `step()`/`handleCommand()` are pure reducers returning `{state, effects}` — keep logic there for testability. `child.ts` owns the child process + lease file (`$TMPDIR/pi-voice.lease.json`) blocking a second Pi window. NDJSON wire contract (Voice→Pi: `ready`/`heard`/`work`/`stop_work`; Pi→Voice: `user`/`result`/`job_update`/`quit`) is in `CLAUDE.md`.
- Verify via the real path (`/health`, `/v1/usage`, actual client WebSocket events). Mock only at the OpenRouter boundary, not internal setters.
- Secrets in `~/.config/chatbot/env` (mode 600); only `OPENROUTER_API_KEY` from the environment overrides it. Keep credentials outside the repository. Review commits for secrets before publishing.
- Constraints: Siri TTS binary dlopens private Apple frameworks — macOS updates can break it with no fallback. STT has no auto-detect (`STT_LOCALE` fixes language). No Chinese Siri voice installed.
- PRs: feature branch off `main`, one change per PR, review before merge.
