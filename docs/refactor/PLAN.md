# Agent-friendly pi-voice: refactor plan

Status: phase 1 done (verification skill, one bug fix, this plan). Phase 2 waits for review.
Process: figure-it-out Phase A (frame) and Phase B (design). Decision trail: [`decisions.tsv`](decisions.tsv).

## Goal

Make pi-voice easy and safe for coding agents (Codex, Claude, Pi, agy) to change: fewer wrong edits, failures that explain themselves, and a scripted way to prove a change on the real product. Who benefits: the user, who reviews agent PRs and lives with the voice assistant daily, and the next agent, who arrives cold.

## Definition of done

Each predicate is a command whose output can be compared before and after. Baselines were captured on `f155638` (phase 1).

| # | Predicate | Baseline (f155638) | Target |
| --- | --- | --- | --- |
| D1 | `.cursor/skills/verify-pi-voice/scripts/pv.py` (path named in `CLAUDE.md` and `AGENTS.md`) `launch`, `doctor`, `drive conversation --via backend`, `drive memory --via backend`, `cleanup` all exit 0 from a fresh worktree, and evidence survives cleanup | VERIFIED (run 20261008-151119) | still VERIFIED after every unit |
| D2 | The `--via voice` matrix (`conversation`, `handoff`, `steer`, `stop`, `memory`) and `--via pi` (`handoff`, `memory`) are VERIFIED on a Mac with audio IO | not yet run: lid was closed, Voice crashes | all VERIFIED, verdicts recorded in the decision log |
| D3 | With the lid closed (no audio IO) headless Voice emits an `error` line and exits non-zero (a distinct code, not SIGABRT) | aborts with SIGABRT (rc 134, `player did not see an IO cycle`) | `drive conversation` reports NOT VERIFIED with the readable `error`, no crash report |
| D4 | Renaming one field or status on one side of any cross-language contract makes that language's test suite fail | most renames pass every suite (see survey) | a scripted mutation run (`scripts/check-contracts.sh --mutate`) turns each suite red for its own side |
| D5 | `tsc --noEmit` over `pi-voice/src` runs in CI against pinned Pi types | not in CI; locally 1 error (a test's typing) against Pi 1.1.0 | 0 errors, CI step required |
| D6 | No hand-edited source file over 600 lines among the two files agents touch most | `LiveVoiceBackend.swift` 1075, `voice.ts` 940 | each under 600, every suite and D1/D2 unchanged (a consequence of U8/U9, not their driver) |
| D7 | `CLAUDE.md` is a map plus rules: under 10 KB, and a test fails when it names a file that does not exist | 21.5 KB, several stale facts (Pi version, test file list, fixed bugs listed as open) | under 10 KB, drift test in pytest |
| D8 | Swift runtime tests still fail when compiled with `-O` | every check is `assert()`, compiled out under `-O` | checks use an always-on helper; `-O` build still fails on a forced false; a pytest fails on any bare `assert(` in `macos/Voice/Tests`, so CI keeps it |

Done means D1 to D8 hold on the branch tip, verified by the commands above, not by reading the diff.

## Scope and effort

About 10 units plus an optional product unit (U11), 25 to 35 focused hours (estimate). Python backend internals (VAD, turn-taking, cancellation) stay out of scope: they are large but well documented and tested, and the agent trips found there are fewer than at the language boundaries. Blockers found while grounding:

- Voice.app needs working audio IO. With the MacBook lid closed it aborts, so D2 needs the lid open (or an external audio device). This blocks every `--via voice` and `--via pi` drive today.
- CI has no Pi package installed; D5 needs pinned dev dependencies for types only.
- `--via pi` spends Pi model tokens and opens the real mic and speaker (mute drops handoffs by design).

## Rigor

High. The extension, Voice.app and backend are the user's daily voice assistant, and the risky parts are cross-language contracts that no single suite sees. Gates, not effort:

- Each unit lands as its own commit on its own branch, with the four suites green (`node --test`, pytest plus ruff, format and mypy, Swift `test.sh`, `build.sh`) and `pv.py` D1 drives VERIFIED before and after.
- Units that touch Voice.app or the extension also need the D2 drives for the features they touch, with the lid open.
- Behavior-preserving units (splits) must leave every existing test unchanged. A test edit in a split unit is a red flag that needs a decision-log row.
- Delegated work is judged by re-running the gates on the artifact, never by the worker's summary.
- One-way doors (the contract format in U5) go through a design comparison before code: two or three candidate shapes, chosen by how a rename fails.

## Where agents trip (survey)

Sources: my reading of the tree at `f155638`, the agy researcher survey (job `research-mv00yhtt-827e43b9`), and the phase 1 runs. Ranked by how likely each causes a wrong change.

1. **Cross-language wire contracts are implicit.** The NDJSON stdio contract is typed in `pi-voice/src/child.ts` and parsed from `[String: Any]` in `HeadlessBridge.swift`; Realtime events are dictionaries in `LiveVoiceBackend.handleMessage` (174 lines). Job statuses exist as a TS union (`child.ts` `JobUpdateStatus`) and a Swift enum (`PiJobTracker.State`) with no shared test. A renamed field decodes to a default and no suite fails. `tests/test_tool_contract.py` checks tool names, descriptions, ack statuses (`started`, `steering`, `already_working`) and steer dispatch order by text over Swift source, but no test ties the NDJSON field names or the job status set across TS and Swift.
2. **Magic strings are produced in one language and interpreted in another.** `[STATUS]` and `[FINAL]` are built in Swift (`PiJobTracker`) and explained to the model in Python (`voice_prompt.py`); `[PI]` is still described in the prompt though no current client sends it. `[Pi voice job id: …]` is written and regex-parsed in `work.ts` and must stay last. `[Pi handoff]`, `[Pi result "…"]`, and the leftover frame are written in `history.ts` and decide speaker labels in the startup pack.
3. **Character caps interact across files.** `RESULT_MAX` 1500 (TS) and Swift `defaultPageLimit` 1500; `WORK_RESULT_CHARS` 16,000; delegation 4 KiB per field and 8 KiB delta; `VOICE_REPLAY_KB` 16 with 80 turns and a 64 KB env bound (`session.ts`); the Pi voice prompt must stay under 5,500 characters (`tests/test_voice_prompt.py`). Each is pinned in its own suite; nothing states which must stay ordered relative to the others.
4. **No TypeScript typecheck.** Node strips types, so arity and shape errors run. The `VoiceChild.spawn` arity bug recorded in `scripts/AGENT-VERIFICATION.md` was this class. Locally, `tsc` against Pi 1.1.0 is clean except one test typing.
5. **Giant files and functions.** `LiveVoiceBackend.swift` 1075 lines (`handleMessage` 174), `voice.ts` 940 (`step` 204, `apply` 107), `websocket_router.py` `create_app` 275, `vad_handler.py` `_process_realtime` 155. `LiveVoiceBackend.swift` is not compiled by `test.sh`, so none of its logic is unit tested.
6. **Stale agent docs.** `CLAUDE.md` (21.5 KB, loaded every session) says Pi 0.86/0.87.1 (installed is 1.1.0) and lists two of the five TS test files. `AGENT-VERIFICATION.md` lists five "pending" findings; four are already fixed (spawn arity, Swift 20-message cap removed in a331063, `/voice new` seeding, resume list retention).
7. **Diagnostics are scattered and some failures are silent.** Logs live in `/tmp/chatbot-server.log`, `/tmp/voice-service-startup.log`, `$TMPDIR/pi-voice.<pid>.stderr.log`, and (until b481cfb) a journal that ignored `TMPDIR`. Health and journal writes swallow errors (`LocalServiceStarter.fetchJSON`, `PiJobTracker.appendJournal`). Voice aborts with no `error` line when audio IO is missing.
8. **Test-harness traps.** Swift tests are `assert()` calls, which `-O` removes; `build.sh` uses `-O`, `test.sh` does not. There was no scripted end-to-end path until this phase; earlier E2E scripts lived in `/tmp`.
9. **Port 8766 is shared with the user's live assistant.** `run-browser.sh --reuse-running` and any checkout's Voice.app restart `stale` or `foreign` services there. Agents need an isolated way to run (now `pv.py`, own port, per-process `-voice.wsUrl`).

## Units

Numbered by topic; the run order is below the table (riskiest unknown first, verification scaffolds before code changes). "agy" means agy implements a scoped prompt and the lead judges the diff and re-runs the gates; "lead" means the work needs hardware, judgment, or cross-language design.

| # | Unit | Why first / risk | Who | Verified by |
| --- | --- | --- | --- | --- |
| U0 | Verification skill `verify-pi-voice`, journal under `TMPDIR`, agent-doc pointers (phase 1) | scaffold for everything else | lead, agy drafts | D1 VERIFIED; suites green (done) |
| U1 | Run the full D2 matrix with the lid open; record verdicts; fix the harness where it, not the product, is wrong | biggest unknown: the voice and pi drives have never run green in this form | lead | D2 verdicts in `decisions.tsv` |
| U2 | Voice: no audio IO becomes an `error` event, not an abort | unknown AVFoundation behavior across devices (built-in, AirPods, USB); product decision below | lead spike, then agy | D3 with lid closed; D2 still VERIFIED with lid open on built-in and one Bluetooth device |
| U3 | Swift test checks always run (helper instead of `assert`), plus a pytest banning bare `assert(` in Swift tests | cheap scaffold; protects every later Swift unit, so it lands before any Swift code change | agy | D8: a forced false fails under `-O`; the ban test fails on a planted `assert(` |
| U4 | TS typecheck: `pi-voice/tsconfig.json`, pinned dev types, CI step, fix the one test typing | scaffold; catches the arity class of bug | agy | D5; a deliberate arity break fails `tsc` |
| U5 | One contract file for the cross-language seams (NDJSON in/out types and fields, job statuses, tool names and argument shape, channel prefixes, job-id marker, history prefixes, shared caps), with a reader test in each language | one-way door: design comparison first (JSON table vs codegen) | lead designs, agy writes per-language tests | D4 mutation run |
| U6 | Typed decode at the stdio boundary: Swift `Codable` enum in `HeadlessBridge`, TS parse checked against the contract, unknown or invalid lines logged once with the reason | depends on U5 | agy (per language, separate branches) | suites; a malformed line shows in the stderr log; D2 handoff unchanged |
| U7 | Diagnostics: `pv.py logs` bundles every log for a run; work id printed in extension stderr, Voice stderr, and the journal; debug-path `try?` and bare `except` log once | makes the next failure explain itself | agy | a forced handoff failure is traceable by one id across all three logs |
| U8 | `LiveVoiceBackend.swift` in two steps: first extract the pure parts (event decode, tool-call dispatch decisions) into files that `test.sh` compiles, with new tests; only then split the coordinator by role (Realtime events, tool calls, Pi job channel) | behavior-preserving, high blast radius: it has no unit tests today; needs U1 green first | agy, lead reviews | new pure tests green before the move; D6; Swift suite and D2 unchanged, no test edits in the move step |
| U9 | Split `voice.ts` `step` by event family (call lifecycle, transcript, job), keeping it a pure reducer | behavior-preserving | agy, lead reviews | D6; `voice.test.ts` unchanged and green; D2 unchanged |
| U10 | Slim `CLAUDE.md` to a map plus rules; move deep sections to `docs/architecture/`; fold `AGENT-VERIFICATION.md` into the skill; drift test | last, so it describes the final shape | lead writes, agy drafts the moves | D7 |
| U11 | Optional, product behavior, outside the agent-friendliness goal: superseded-but-finished results (history stores `[Pi result …]` as complete while Voice frames the same answer as partial) | needs the user's call; can be tracked as a separate issue | lead | decision row, then a cross-language test |

Run order: U0, U1, then U3 and U4 (cheap scaffolds, so U2's Swift change and every later unit is checked by them), then U2, U5, U6, U7, U8 and U9, and U10 strictly last because its drift test must describe the final file layout. U3 and U4 touch disjoint files and can run as parallel agy jobs in separate worktrees. U8 and U9 are different languages and can also run in parallel once U5 and U6 land. U5 must precede U6. Nothing fans out wider than two workers, because every unit needs the lead to judge it on hardware.

## Decisions for the user

1. **No audio device (U2).** Recommended: Voice fails fast with an `error` line ("no audio input/output; is the lid closed?") so Pi shows it. Alternative: a text-only mode that keeps typed turns working without audio. The first is smaller and matches "Voice.app is only ears and mouth".
2. **Superseded but finished (U11).** Pi finished the earlier brief before the redirect landed. Today the shared log calls it a complete result and Voice tells Agent it is partial. Recommended: treat it as complete in both (Pi did finish it), and keep "partial" for answers cut off by a stop or a steer mid-turn.
3. **Eval (optional).** A before/after run of five seeded agent tasks (for example "add a job status", "raise RESULT_MAX", "rename the brief field") measuring how many land green without help. It costs agent time; it is the only direct measure of "fewer agent mistakes".
4. **Committing the decision log.** Phase 1 commits `docs/refactor/decisions.tsv` so the trail travels with the branch. Say if you prefer it local.

## Reviewer challenge

An agy reviewer (job `review-mv01fyhy-717b3beb`) challenged this plan. Folded in: D3 exits non-zero rather than 0; U10 no longer runs in parallel; U3 and U4 run before U2; U8 adds tests before moving code and waits for U1; the survey's `test_tool_contract.py` claim was corrected; D6 names two files; D8 gets a CI-enforced ban on bare `assert(`; U11 is marked optional product work. Rejected: moving `pv.py` to `scripts/` (the skill is not ignored, since `.gitignore` re-includes `.cursor/skills/`, and both agent docs name its path; D1 now pins that path instead).

## Phase 1 results

- Verification skill: `.cursor/skills/verify-pi-voice/` (SKILL.md, `scripts/pv.py`, `scripts/seed-history.mjs`, `scripts/ws_recall.py`, `features/`). Proof run `20261008-151119`: launch, doctor, `conversation --via backend` VERIFIED, `memory --via backend` VERIFIED, `handoff --via voice` NOT VERIFIED (audio crash, lid closed), cleanup kept the evidence.
- Bug fixed: b481cfb, the Pi job journal now honors `TMPDIR` like the extension's lease and stderr log (regression test in `RuntimeTests.swift`).
- Bug found, not fixed: Voice aborts when audio IO is missing (U2). Checked, not a bug: the Swift 20-message history cap is gone (a331063); the backend keeps `CHAT_SIZE` 100 user turns with compaction, so the startup pack survives a long call.
