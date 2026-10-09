# pi-voice

A Mac-first live voice assistant that runs its ears and voice locally, while a Responses API model handles the conversation. You talk to Pi in the terminal; Voice.app is the microphone and the speakers.

`microphone → on-device macOS speech-to-text → Responses API → Siri text-to-speech → speakers`

Voice.app has no window. It runs as a background audio bridge and talks to Pi over stdio; the SwiftUI panel was removed once Pi became the only way in.

It keeps realtime WebSocket turn-taking and interruption/cancellation. Under Pi, Agent has no bash; research, memory, and web reading go to Pi via `spawn_thinking`.

Audio modes are set via the `voice.audioMode` UserDefaults key (default `automatic`, configured via `defaults write dev.jldynamics.Voice voice.audioMode <mode>`). In `automatic` mode, Apple's voice processing handles echo cancellation, with Silero VAD detecting interruptions. The first assistant reply suppresses mic capture while playing and for a short tail because an echoed greeting can otherwise become the user's next turn; later replies allow interruptions. If voice processing cannot start, the app falls back to compatibility mode, which suppresses microphone capture during playback. `headphones` mode keeps capture open without echo cancellation.

## Requirements

- Apple Silicon Mac, macOS 26 or newer (the on-device speech engine)
- Python 3.10 or newer
- [uv](https://docs.astral.sh/uv/)
- An OpenRouter key
- The [siri-tts](https://github.com/maximilianromer/siri-tts-cli) binary installed at `~/.local/bin/siri-tts` (or specified via `SIRI_TTS_BIN`)
- For `/voice`: Pi 1.1 or later 1.x with `pi-voice/` loaded as an extension (`package.json` `pi.extensions`), running Node ≥22.18

## Install and run

```bash
uv sync
./set-keys.sh
./run-browser.sh
```

`uv sync` installs the app and its dependencies.

```bash
./macos/Voice/scripts/build.sh
```

That produces `macos/Voice/build/Voice.app`, which the pi-voice extension launches for you — there is nothing to open by hand (opened by hand with no stdin pipe it sees EOF and quits). `run-browser.sh` starts the realtime backend (`:8766`). When the extension launches Voice, it starts that backend if needed, and replaces one that is not running this checkout's current code (stale, foreign, unknown, or hung).

The default model path is `anthropic/claude-haiku-5.5` through OpenRouter. Speech-to-text runs **on this Mac** through Apple's on-device engine, so there is no key, no metering, and nothing that expires: a turn transcribes punctuated in ~170ms, including the helper process launch, and a live 33s turn with four pauses took 370ms. It covers 45 locales; `STT_LOCALE` picks one, because the engine has no auto-detect. `macos/SpeechHelper/build/speech-helper --locales` lists what this Mac supports and which models are already installed, and a locale's model downloads itself the first time the backend starts with it. It transcribes fillers literally and does not know proper nouns it has no context for, which a hosted service tidied.

Text-to-speech uses **Apple's Siri voices** (`en-US-F` by default) through
[siri-tts](https://github.com/maximilianromer/siri-tts-cli), which reaches the neural voices Apple
publishes to no API: `AVSpeechSynthesisVoice` never lists them and `say -v` ignores their names.
Measured against the Kokoro backend it replaced, on the same sentence, it is 84ms to first audio
versus 208ms. That binary dlopens private frameworks, so a macOS update can break it; there is no
fallback backend. Siri is the only voice, so until that is fixed the app has no voice at all.

This build does not speak Chinese. There is no Chinese Siri voice installed (only en-US/en-GB). Chinese STT locales exist (`STT_LOCALE=zh-CN`), but Han characters in a reply reach the English Siri voice as written.

## Configuration

The launch scripts read secrets from `~/.config/chatbot/env` (or `CHATBOT_ENV`), written with owner-only permissions by `set-keys.sh`. Only `OPENROUTER_API_KEY` from the environment overrides saved values; other variables in that file override the environment.

| Setting | Default | Purpose |
| --- | --- | --- |
| `STT_LOCALE` | `en-US` | Transcription locale, e.g. `en-GB`, `en-AU`, `ja-JP`, `fr-FR`. The engine has no auto-detect, so this fixes the spoken language |
| `TTS` | `siri` | TTS backend: `siri` (Apple's Siri voices, the only voice) |
| `SIRI_VOICE` | `en-US-F` | Siri voice name; `siri-tts voices --available` lists what this Mac has installed |
| `SIRI_TTS_BIN` | `~/.local/bin/siri-tts` | Path to the siri-tts binary |
| `MODEL` | `anthropic/claude-haiku-5.5` | OpenRouter Responses API model ID |
| `PORT` | `8766` | Realtime backend port for a manual `./run-browser.sh`; Voice.app always connects to 8766 unless `voice.wsUrl` is set |
| `VAD_MIN_SILENCE_MS` | `2000` | Silence (ms) before a spoken turn is considered finished. Higher keeps pauses inside one turn instead of splitting it; lower answers faster after you truly stop |
| `VAD_THRESH` | `0.6` | VAD confidence threshold; higher = fewer false voice triggers |
| `VAD_MIN_SPEECH_MS` | `384` | Sustained speech (ms) before a user turn / barge-in is confirmed. Raise to soften barge-in (so brief noises or the assistant's own echo don't cut a reply) |
| `VAD_SPEECH_PAD_MS` | `500` | Audio padding (ms) prepended to detected speech |
| `VAD_SHORT_SEGMENT_MERGE_MS` | `0` | Stitching window (ms) for adjacent sub-threshold speech fragments |
| `CHAT_SIZE` | `100` | Maximum message history retained in the conversation |
| `BATCH_SENTENCES` | `3` | Sentence batch size for streaming TTS synthesis |
| `REASONING_EFFORT` | `low` | OpenRouter reasoning effort (`none`, `minimal`, `low`, `medium`, `high`) |
| `VOICE_BIN` | `macos/Voice/build/Voice.app/Contents/MacOS/Voice` | Path to the Voice binary launched by pi-voice |
| `PROMPT` | `You are an AI conversation partner: perceptive, relaxed, warm, and quietly playful. You enjoy exploring ideas and have something thoughtful to contribute. Speak with the ease of someone comfortable in the conversation.` | Default session persona inserted into the server-built prompt; Voice.app sends its own, so this applies only to clients that don't |

### Research

During `/voice`, Pi stays in its ordinary terminal transcript. Your words appear there, and Luna's reply grows in one chat entry as she speaks. Pi's tool calls and answer start folded in place; click either entry to open its full text and click again to close it. The folded Pi answer shows whether Pi is still writing or has finished. `/voice stop` ends the call. Text submitted in Pi's composer during voice goes to Luna.

The voice transcript keeps one visible entry per user utterance and one per Luna utterance. Your entry is placed as soon as a transcript arrives, before Luna's streaming reply; later recognition revisions update that same line. Hidden final records save completed text for session replay without adding extra lines. Voice.app sends stable IDs so an interrupted reply cannot finish a later line by mistake. The current on-device speech engine recognizes a completed audio segment, so your words appear as each segment is recognized; it does not supply word-by-word guesses while a segment is still being spoken.

Pi's normal terminal settings apply. Pi's bundled renderer (reviewed for Pi 0.87.1 and 1.1.0) needs the scoped repair in `scripts/patch-pi-native-fold.mjs <pi-coding-agent package dir>` for per-entry folding during voice. For the managed install that directory is `~/.pi/agent/install/releases/<version>/node_modules/@earendil-works/pi-coding-agent`. The script verifies the exact bundle, saves a backup, supports `--check` (dry run) and `--restore`, does nothing on an already-patched bundle, and refuses any unreviewed version without changing it. An existing Pi process must be restarted after applying it.

Under Pi, Voice.app publishes two tools: `spawn_thinking` and `stop_thinking`. Agent has no bash tool in that configuration. Casual chat stays with Agent. Files, the shell, research, PDFs, code changes, what is on screen, and computer use (click, type, forms, navigation) go to the open Pi session through `spawn_thinking`. Pi captures the frontmost window with `screencapture` and uses its own computer-use tools. The terminal that launches Pi may need Screen Recording and Accessibility permission. A second call while Pi is busy redirects that task; the latest brief wins. A changed progress note is stored as silent `[STATUS]` context and is not spoken. The answer arrives as `[FINAL]` and Agent speaks that. Agent does not poll. `stop_thinking` cancels the current job and leaves the call up. `/voice stop` ends the call.

Voice and Pi share one conversation log per thread (`~/.config/pi-voice/history-<id>.sqlite`). The folder you start in only chooses which thread opens; `/voice resume` lists threads by id. Starting `/voice` rebuilds a dated background pack (`Latest` and `Previous`) and warns that it may be stale. Each `spawn_thinking` brief includes that pack, and the full Pi answer is stored in the log, not only the short excerpt Agent speaks. Stopping `/voice` saves any leftover transcript into the same log.

To try this branch: check it out, build Voice (`bash macos/Voice/scripts/build.sh`), start Pi from this worktree with `pi-voice/` loaded, and run `/voice`. Say something you can answer from the conversation (Agent should answer), then ask it to read a file, look something up, or say what is on your screen (it should call `spawn_thinking` and stay mostly quiet until a status or final update). Asking it to click or type is the same handoff. Say "stop" to cancel the job without ending voice, or `/voice stop` to hang up. Start `/voice` again and the earlier exchange should come back as dated background, not as a new question. A muted mic drops a new handoff (`dropped`) and does not start Pi.

The server-side `bash` research tool still exists for clients that publish it (e.g. `scripts/verify-voice.py --research`) and runs under a macOS `sandbox-exec` profile: file writes only in temp dirs and no connections to this Mac (localhost). Literal private-network URLs are refused, but a `curl -L` redirect to one is not. No client-side tools remain.

## Optional tools

- **Memory** belongs to Pi. The voice stack keeps only the running conversation, and does not persist it.

## Development

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv run pytest -q
node --test 'pi-voice/src/*.test.ts'
bash macos/Voice/scripts/test.sh
uv run python scripts/verify-voice.py --research   # live bash/curl research, services must be up
```

CI runs on macOS (Python lint, format, types, tests; Swift runtime tests; Pi extension tests), builds the Python package, and performs an installation smoke test. Publishing is handled by `.github/workflows/publish.yml` for `v*` tags.

### Contributing via pull requests

Work on a feature branch off `main`, open a pull request, and wait for review before merging. Keep each PR focused on one change so reviewers can follow the diff easily. After approval, merge into `main` and delete the branch.

The native test runner (`bash macos/Voice/scripts/test.sh`) covers playback queue generations, cancelled work, tool definitions, service staleness, mic capture muting, transcript revisions, and the headless NDJSON bridge.

## License

Apache-2.0. This project is a fork of Hugging Face's [`speech-to-speech`](https://github.com/huggingface/speech-to-speech) project. The upstream copyright notice is retained in [LICENSE](LICENSE) and [NOTICE](NOTICE), with the fork's copyright (JLDynamics) added alongside.
