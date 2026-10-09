# U5: one contract file for the cross-language seams (design)

Status: adopted (option A), amended after an agy reviewer challenge (see the end). The file is [`contracts/pi-voice.json`](../../contracts/pi-voice.json).

## What crosses a language boundary today

| Seam | Producer | Consumer | Where today |
| --- | --- | --- | --- |
| stdio in (extension to Voice): `user{text}`, `mute{muted}`, `interrupt`, `result{id,speak,full}`, `job_update{id,status,note?}`, `quit` | TS `child.ts` `HeadlessIn` | Swift `HeadlessBridge.handle(line:)`, `object["…"] as? String ?? ""` | TS union type; Swift string literals; no shared test |
| stdio out (Voice to extension): `ready`, `error{message}`, `request_error{message}`, `speech_started`, `heard{text,item_id}`, `spoken_delta{text,item_id}`, `spoken{text,item_id}`, `work{id,brief}`, `stop_work` | Swift `HeadlessBridge.attach` `emit([...])` | TS `child.ts` `parseLine` | dictionary literals; hand parser; prose list in `CLAUDE.md` |
| job statuses `queued working done stopped superseded dropped failed` | TS `JobUpdateStatus` (and a second inline copy in `voice.ts` `Effect`) | Swift `PiJobTracker.State` | two enums, no shared test |
| tools `spawn_thinking{brief}`, `stop_thinking{}` | Swift `LiveVoiceBackend` tool defs | Python `voice_prompt.py` (`"spawn_thinking" in names` gates the Pi section; prose names both tools) | `tests/test_tool_contract.py` covers server/client tool split only |
| `VOICE_HISTORY` env: JSON array of `{role, text}` | TS `headlessChildEnv` | Swift `VoiceHistoryEnv.parse` (moved out of `VoiceApp.swift` so `test.sh` compiles it) | none |
| channel tags `[STATUS]`, `[FINAL]`, legacy `[PI]` | Swift `PiJobTracker.statusChannel`/`finalChannel` | Python `voice_prompt.py` explains them to the model | separate tests per side |

Left out on purpose: the job-id marker, `[Pi handoff]`/`[Pi result …]` history prefixes and the startup pack (written and read by TS only; already unit-tested there); caps (`RESULT_MAX` 1500 and Swift `defaultPageLimit` 1500 match by coincidence: `resultsPayload` is not a model tool and nothing ties them); Realtime WS events (OpenAI's protocol, owned by the backend; U8 covers Swift's decoding of them).

## Candidates, judged by how a one-sided rename fails

A. **Data file plus a conformance test per language.** `contracts/pi-voice.json` lists the seams above. Each language keeps hand-written code and gains one test that loads the JSON and exercises its *real* code against it: Swift feeds every inbound sample through `HeadlessBridge.handle(line:)` with `MockVoiceBackend` and fires every outbound callback through `emitSink`; TS runs every outbound sample through `parseLine` and captures every inbound message `VoiceChild` writes; pytest checks the Python prompt names every tool and tag. A rename on one side fails that side's suite (D4). A rename in the JSON fails every side until each is updated. Cost: one JSON file, three tests, no build step.

B. **Codegen.** JSON generates `Contract.swift`, `contract.ts`, `contract.py` constants; code uses the constants, so a rename is a compile error. Costs: a generator, generated files checked in plus a CI drift check, `test.sh`/`build.sh` compile lists change, and agents must learn "edit the JSON, regenerate". A constant prevents typos but not a field one side forgot to read; the behavioural tests from A are still needed for that.

C. **Source-parsing pytest only** (the `test_tool_contract.py` style: regex over Swift and TS sources). Cheapest, but it checks spelling, not behaviour: a parser that ignores a field still passes, and every refactor (U6, U8, U9) breaks the regexes.

**Choice: A.** It is the only one where the failing test is in the language that changed, with a message naming the seam, and it survives U6/U8/U9 rewrites because it tests behaviour, not text. B can be layered later if constants are wanted; nothing in A blocks it. Reversal cost of A is low (delete one file and three tests); that is why it is the one-way door taken first.

## Shape

See `contracts/pi-voice.json`. Field types are `string`, `boolean`, or an enum name from `enums` (`jobStatus`, `historyRole`); a trailing `?` marks optional. A message lists every field its sender writes; extra keys are a violation (no emitter writes extra keys today).

Production seams added so each side's real code is testable without a live app: Swift `HeadlessTools` in `VoiceTools.swift` (tool names, definitions, `spawnBrief`; was private in the untested `LiveVoiceBackend.swift`), `VoiceHistoryEnv.parse`, `HeadlessBridge.onStopService`, `MockVoiceBackend.mutedCalls`; TS `toVoice` builders (the only place outbound field names are written) and `JOB_UPDATE_STATUSES` (the `voice.ts` `Effect` copy now reuses `JobUpdateStatus`).

## Conformance tests (agy writes, lead judges)

Samples are generated from the vocabulary: `string` gets a distinct non-empty value per field, `boolean` gets `true` (and `false` where both matter), an enum gets each of its values.

- TS `pi-voice/src/contract.test.ts`: every `fromVoice` sample goes through `parseLine` and yields an event carrying each field's value (a dropped field fails, not only a dropped type); every `toVoice` builder output has a contract `type` and exactly that message's keys, with optional keys present only when given; every `toVoice` message has a builder; `JOB_UPDATE_STATUSES` equals `enums.jobStatus`; `headlessChildEnv` writes `voiceHistory.env` as an array of objects with exactly the `turn` keys.
- Swift `RuntimeTests` `testContract()`: loads the JSON via `#filePath`; every `toVoice` sample sent through `HeadlessBridge.handle(line:)` (mock driven to `.listening`, `onStopService`/`onTerminate` injected) reaches `MockVoiceBackend` with its values (`ingestedUserText`, `postedResults`, `piJobUpdates`, `mutedCalls`, `interruptCount`, terminate called); every emission seen while firing each backend callback has a contract `type` and exactly its keys, and every `fromVoice` type is emitted at least once; `PiJobTracker.State` raw values equal `enums.jobStatus`; `VoiceHistoryEnv.variable` equals `voiceHistory.env` and `parse` keeps a sample built from `turn`; `HeadlessTools.definitions` names equal `tools` keys with exactly the contract argument keys, and `spawnBrief` reads the contract argument; `statusChannel`/`finalChannel` start with the contract tags.
- Python `tests/test_contract.py`: the JSON is well formed (known types, enum names resolve); the Pi section of the voice prompt names every tool and every channel tag; `test_tool_contract.py` stays as is.
- `scripts/check-contracts.sh --mutate` (D4): refuses a dirty tree for the files it mutates; for each mutation, rewrites one string literal on one side (for example `"item_id"` in `HeadlessBridge.swift`, `object.item_id` in `child.ts`, `case stopped` raw value, `"spawn_thinking"` in `HeadlessTools`, `[FINAL]` in `PiJobTracker.swift`, `[STATUS]` in `voice_prompt.py`, `VOICE_HISTORY` in `child.ts`), runs that side's suite, restores the file (trap on exit), and requires failure; prints a table and exits non-zero if any mutation survived. Local only, not in CI.

## Risks

- Behavioural Swift tests need `MockVoiceBackend` hooks for `ingestUserText`, `postResult`, `updatePiJob`, `setMuted`, `interrupt`; if some are missing, the test adds them to the mock, not to production code.
- `quit` calls `LocalServiceStarter.shared.stop()` and `onTerminate()`; the test injects `onTerminate` and must not stop a real service.
- The JSON is read at test time only; production code does not load it, so a missing file cannot break the app.

## Reviewer challenge (agy job `review-mv0j4iz1-7aef8ef8`)

Accepted: tool definitions lived in `LiveVoiceBackend.swift`, which `test.sh` does not compile, so a Swift rename passed every suite (moved to `HeadlessTools`); `VoiceChild` could not be observed without spawning (added `toVoice` builders); ack statuses never reach Python (dropped from the contract); `VOICE_HISTORY` crosses TS to Swift and was missing (added); `setMuted`/`interrupt` are ignored while the session is idle, so the Swift test drives the mock to `.listening` first, and the mock now records mute; `quit` stopped the real launcher (now injectable). Partly accepted: in-place mutation stays, but only string literals are mutated (they compile, so the failure is the contract test, not the compiler), with a trap that restores the file. Rejected: JSON Schema instead of the four-token vocabulary (Swift has no validator; samples are generated from the vocabulary in a few lines per language).
