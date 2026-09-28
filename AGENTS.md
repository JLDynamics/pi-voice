# AGENTS.md

Read `CLAUDE.md` before changing anything. It is the single source of truth for layout, commands, architecture, and constraints in this repo; `README.md` covers install, run, and env vars.

The rules most likely to bite:

- If `:8766/health` reports `foreign`, stop: that may be another active session. Do not run `./run-browser.sh --reuse-running`, this checkout's Voice.app, or `/voice`.
- Never `pkill` by process name; stop only the launcher you started.
- `Voice.app` has no window by design. Do not add one; anything user-facing belongs in Pi.
- Verify through the real path (`/health`, `/v1/usage`, real WebSocket events). Mock only at the OpenRouter boundary.
- Keep secrets in `~/.config/chatbot/env` (mode 600), never in the repo.
- Work on a feature branch off `main`, one change per PR.
