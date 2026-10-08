# Agent verification and review

## Evidence before completion

Report which boundary each check crosses. Reducer and SQLite tests prove local
logic; `verify-voice.py` proves backend WebSocket behavior. Neither proves a
Pi-delegated conversation through the extension and Voice.app. A passing suite
is not evidence for a path it never exercises.

For conversation changes, exercise the actual child launch, NDJSON bridge,
WebSocket events, and returned audio. Keep regression fixtures in the repo,
with isolated config directories; mock only OpenRouter. State any untested
boundary in the final response.

For history changes, follow the full chain:
`Conversation.replay` (dated Latest/Previous startup pack) and `handoffContext` on
`sendWork`, plus the leftover flush in `stopLive` → `VoiceChild.spawn` /
`headlessChildEnv` `VOICE_HISTORY` → `VoiceApp.swift` → `VoiceSession.seedHistory` →
`LiveVoiceBackend.replayHistory` (`conversation.item.create`, no response).
The shared log is `history-<id>.sqlite`, keyed by thread id. `session.json` is only
the folder → id map. Pi's session cannot take a silent append, so each handoff
carries the pack and the job-id marker stays last. Verify a restart rebuilds the
pack, `/voice stop` records uncommitted transcript as leftover history, `/voice new`
stays empty, resume by id, and a corrupt-cabinet fallback. Test the public
command/lifecycle rather than manually reproducing only its reducer effects.

For delegation changes, verify success, partial failure, explicit stop, and
replacement during an active job. `[STATUS]` is silent context. Check spoken
text against the `[FINAL]` channel and the job mirror together: receiving a result must not
turn a stopped or failed job into a completed job. Label incomplete findings
as partial. Agent has no `pi_status` or `pi_results` tool.

## Safe live checks

Probe health and inspect ownership before launch. A foreign service means stop;
an existing same-checkout service is not automatically ours to restart either.
Use plain `run-browser.sh`. Launch without a `head` pipeline, retain that exact
launcher's PID, and stop that PID. Confirm its child exited and the port is free.
Process-name searches are diagnostics, not ownership evidence.

When changing models, inspect only the MODEL assignment in the config file,
not the whole secret-bearing file. Confirm the effective running model and
`/v1/usage`; launcher defaults can be overridden by config. Keep credentials
out of tool output as well as commits. Rotate a credential if exposed.

## Review findings from the October 2026 session

Status as of the agent-friendly refactor (see `docs/refactor/PLAN.md`):

- Fixed: `VoiceChild.spawn` takes `(onEvent, history)` and `startChild` passes two
  arguments.
- Fixed: Swift no longer caps history at 20 messages (removed in a331063). The
  backend keeps `CHAT_SIZE` user turns (100 under `run-openrouter.sh`) and then
  compacts, so the startup pack survives a long call.
- Fixed: `/voice new` and resumed cabinets are never seeded from the branch, and
  resume numbers use the list that was shown (`conversation.test.ts`).
- Open: a superseded job whose answer Pi had already finished is stored as a
  complete `[Pi result …]` but spoken as partial. This needs a product decision
  before a cross-language test (plan unit U11).

CI now runs existing TypeScript and Swift tests as well as Python checks.
TypeScript typechecking remains a gap: Node's type stripping does not check
arity or types. The child-argument mismatch demonstrates why runtime tests
alone are insufficient.
