# Native macOS pieces

`Voice/` is a headless audio bridge with no window. Pi's `/voice` runs it as a child process and communicates via NDJSON over stdio (`HeadlessBridge.swift`); it is the only mic and speaker client in the system.

`SpeechHelper/` is the transcriber the Python backend shells out to, one
process per spoken turn. It exists because `SpeechAnalyzer` and
`SpeechTranscriber` are Swift-only, so PyObjC cannot reach the fast on-device
engine, and the bridged `SFSpeechRecognizer` is the slower one.

```bash
./macos/SpeechHelper/scripts/build.sh          # writes build/speech-helper
./macos/SpeechHelper/build/speech-helper --locales
./macos/SpeechHelper/build/speech-helper --locale en-US --prepare
```

`run-openrouter.sh` builds it when the binary is missing, so a fresh clone
needs no extra step. It reads raw 16-bit mono PCM on stdin and prints one JSON
object; a turn's audio never touches disk. Needs macOS 26 or newer.

## Build and launch

Build the app bundle:

```bash
./macos/Voice/scripts/build.sh
```

This compiles `macos/Voice/build/Voice.app`. Pi's `/voice` command launches that binary with `--headless` (or the path set in `$VOICE_BIN`). Do not open it from Finder or via `open` — with no stdin pipe connected, it sees EOF on stdin and quits immediately.

Running `./macos/Voice/scripts/install.sh [--skip-build]` copies the build to `/Applications`, but nothing in the default setup launches that copy.

## Service startup

On startup, `Voice.app` probes `http://127.0.0.1:8766/health`. If the service is unreachable or not running this checkout's current code, Voice runs `./run-browser.sh --reuse-running` (which replaces stale, foreign, unknown, or hung services) and waits up to 180 seconds for models to finish loading. Startup output is logged to `/tmp/voice-service-startup.log`. On quit, Voice stops only the launcher process that it started. A custom `voice.wsUrl` is never auto-started.

## Backends and configuration

- Default: `LiveVoiceBackend` (real mic/speaker I/O via CoreAudio, connected to the realtime WebSocket backend).
- Mock backend: scripted transcript over the bridge without network (`defaults write dev.jldynamics.Voice voice.useMock -bool true`).

UserDefaults keys (`dev.jldynamics.Voice`):
- `voice.useMock` (`Bool`): Use `MockVoiceBackend` instead of connecting to WebSocket.
- `voice.wsUrl` (`String`): WebSocket server URL (defaults to `ws://127.0.0.1:8766/v1/realtime`).
- `voice.micGain` (`Double`): Software input gain multiplier applied to mic capture.
- `voice.audioMode` (`String`): `automatic` (enables voice processing AEC on speakers) or `headphones` (bypasses voice processing).
- `tools.web_search` (`Bool`): Gates the client-published `bash` research tool definition (default true; key name is legacy).

## Layout

```
macos/
  Voice/
    Sources/App/       VoiceApp (accessory mode NSApplication, lifecycle)
    Sources/Audio/     AudioEngine (CoreAudio I/O, AEC), PCMBridge (format conversion)
    Sources/Session/   HeadlessBridge, LiveVoiceBackend, MockVoiceBackend, VoiceSession,
                       LocalService, LocalServiceStarter, VoiceRuntime, VoiceTools
    Resources/         Info.plist, Voice.entitlements
    Tests/             RuntimeTests.swift (run by scripts/test.sh)
    scripts/           build.sh, test.sh, install.sh, verify.sh, micmode.swift
  SpeechHelper/
    Sources/           main.swift (PCM on stdin -> JSON transcript on stdout)
    scripts/           build.sh
```
