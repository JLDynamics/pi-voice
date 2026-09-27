# Verify Voice

API and live-turn checks for the voice backend. Voice.app is headless, so there
is no interface to click. Start the backend first (`./run-browser.sh`); these
scripts never start, stop or restart a service.

```bash
# From this checkout (uv supplies the websockets package; system python3 lacks it):
uv run python scripts/verify-voice.py
./macos/Voice/scripts/verify.sh            # same, via uv

# Live server-side research and a stray-Han TTS turn:
uv run python scripts/verify-voice.py --research
```

What it covers:

- Voice `/health`, including source fingerprint, `stale`, and `server_tools`
- A short live model reply over the realtime WebSocket (passes on any non-empty reply)
- Spoken PCM (macOS `say`) streamed through the live VAD must raise `speech_started`
- With `--research`: dated news RSS checks (run in this process, not over the
  server), a turn that must `bash`/`curl` a page on the server, a verify-first
  turn that must research without being told the command, and a live TTS turn
  containing `华为` (no Chinese voice; the turn must still complete with audio
  and no error)
- `--skip-talk` runs only the health checks (and, with `--research`, the RSS checks)

Results land in `/tmp/voice-verify-<timestamp>/summary.json` (`--out` picks the folder).
