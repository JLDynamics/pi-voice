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
`voice.ts` → `VoiceChild.spawn` / `headlessChildEnv` → `VoiceApp.swift` →
`VoiceSession.seedHistory` → `LiveVoiceBackend.replayHistory`.
Verify restart recall beyond 20 messages, a genuinely empty new conversation,
stable resume-list numbering, and a corrupt-cabinet fallback. Test the public
command/lifecycle rather than manually reproducing only its reducer effects.

For delegation changes, verify success, partial failure, explicit stop, and
replacement during an active job. Check spoken text, `pi_status` outcome, and
`pi_results` contents together: receiving a result must not turn a stopped or
failed job into a completed job. Label incomplete findings as partial.

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

These are pending product fixes, not claims of completed verification:

- `VoiceChild.spawn` accepts two arguments, but `startChild` supplies three;
  the archive path never reaches `headlessChildEnv`.
- Swift still caps history at 20 messages, despite the 80-message TS replay.
- Empty cabinets are seeded from the branch, including after `/voice new`.
- Resume numbers are recomputed on selection instead of retaining the shown list.
- Superseded results use the normal finished-result path; partial labeling and
  terminal-state preservation need cross-language coverage. Stop/replacement
  paths still require investigation rather than assuming settlement covers them.

CI now runs existing TypeScript and Swift tests as well as Python checks.
TypeScript typechecking remains a gap: Node's type stripping does not check
arity or types. The child-argument mismatch demonstrates why runtime tests
alone are insufficient.
