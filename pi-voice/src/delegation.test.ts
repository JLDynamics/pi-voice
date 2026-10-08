import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  appendTranscript,
  delegationForHandoff,
  MAX_ACTIVE_TRANSCRIPT_BYTES,
  MAX_REALTIME_DELEGATION_FIELD_BYTES,
  renderDelegation,
  setTranscript,
  takeHandoffTranscript,
  transcriptDelta,
  truncateActiveTranscript,
  type TranscriptEntry,
} from "./delegation.ts";

const bytes = (text: string) => Buffer.byteLength(text);
const entryBytes = (entry: TranscriptEntry) => bytes(entry.role) + bytes(entry.text) + 3;
const field = (rendered: string, tag: string) => rendered.match(new RegExp(`<${tag}>([\\s\\S]*)</${tag}>`))?.[1] ?? "";

describe("renderDelegation (Codex core/src/context/realtime_delegation.rs)", () => {
  it("renders exactly what Codex renders", () => {
    // codex realtime_conversation_tests.rs wraps_handoff_with_transcript_delta
    assert.equal(
      renderDelegation("delegate this", "user: hello\nassistant: hi there"),
      "<realtime_delegation>\n  <input>delegate this</input>\n  <transcript_delta>user: hello\nassistant: hi there</transcript_delta>\n</realtime_delegation>",
    );
    assert.equal(renderDelegation("hello"), "<realtime_delegation>\n  <input>hello</input>\n</realtime_delegation>");
    assert.equal(
      renderDelegation("use a < b && c > d", "saw <that>"),
      "<realtime_delegation>\n  <input>use a &lt; b &amp;&amp; c &gt; d</input>\n  <transcript_delta>saw &lt;that&gt;</transcript_delta>\n</realtime_delegation>",
    );
  });

  it("caps each field at 4 KiB: input keeps its start, transcript keeps its end", () => {
    const rendered = renderDelegation(`start${"x".repeat(8 * 1024)}input-end`, `transcript-start${"y".repeat(8 * 1024)}latest`);
    assert.ok(bytes(rendered) < 9 * 1024);
    const input = field(rendered, "input");
    const transcript = field(rendered, "transcript_delta");
    assert.equal(bytes(input), MAX_REALTIME_DELEGATION_FIELD_BYTES);
    assert.equal(bytes(transcript), MAX_REALTIME_DELEGATION_FIELD_BYTES);
    assert.ok(input.startsWith("start") && input.endsWith("…") && !input.includes("input-end"));
    assert.ok(transcript.startsWith("…") && transcript.endsWith("latest") && !transcript.includes("transcript-start"));
  });

  it("escapes before capping and never splits a character", () => {
    const rendered = renderDelegation("é".repeat(5000), "<".repeat(2000));
    const input = field(rendered, "input");
    assert.ok(!input.includes("\uFFFD"));
    assert.ok(bytes(input) <= MAX_REALTIME_DELEGATION_FIELD_BYTES);
    const transcript = field(rendered, "transcript_delta");
    // 2000 "<" escape to 8000 bytes, so the cap applies to the escaped text
    // (and, as in Codex, may cut through an entity at the start).
    assert.ok(transcript.startsWith("…") && transcript.endsWith("&lt;"));
    assert.ok(bytes(transcript) <= MAX_REALTIME_DELEGATION_FIELD_BYTES);
  });
});

describe("active transcript (Codex codex-api realtime_websocket/methods.rs)", () => {
  it("drops the oldest entries beyond 8 KiB", () => {
    let entries: TranscriptEntry[] = [];
    for (let i = 0; i < 40; i++) entries = setTranscript(entries, i % 2 ? "assistant" : "user", `turn ${i} ${"z".repeat(500)}`, `item${i}`);
    const total = entries.reduce((sum, entry) => sum + entryBytes(entry), 0);
    assert.ok(total <= MAX_ACTIVE_TRANSCRIPT_BYTES);
    assert.ok(entries.length < 40);
    assert.match(entries.at(-1)!.text, /^turn 39 /);
    assert.ok(!entries.some((entry) => entry.text.startsWith("turn 0 ")));
    // One more 500-byte turn would not fit: what was dropped is exactly the oldest.
    assert.ok(total + entryBytes(entries[0]) > MAX_ACTIVE_TRANSCRIPT_BYTES - 520);
  });

  it("keeps the end of a single oversized entry behind an ellipsis", () => {
    const [only] = truncateActiveTranscript([{ role: "user", text: `old-start ${"w".repeat(10_000)} newest` }]);
    assert.ok(only.text.startsWith("…") && only.text.endsWith(" newest"));
    assert.equal(entryBytes(only), MAX_ACTIVE_TRANSCRIPT_BYTES);
  });

  it("applies revisions by item id and appends streamed speech", () => {
    let entries = setTranscript([], "user", "find fli", "h1");
    entries = setTranscript(entries, "user", "find flights", "h1");
    entries = appendTranscript(entries, "assistant", "Which ", "s1");
    entries = appendTranscript(entries, "assistant", "dates?", "s1");
    entries = setTranscript(entries, "assistant", "Which dates?", "s1");
    assert.equal(transcriptDelta(entries), "user: find flights\nassistant: Which dates?");
    assert.equal(transcriptDelta([]), undefined);
  });

  it("adds the brief as a user entry on handoff unless the user said exactly that", () => {
    const said: TranscriptEntry[] = [{ role: "user", text: "check the news" }];
    assert.deepEqual(takeHandoffTranscript(said, " check the news "), said);
    assert.deepEqual(takeHandoffTranscript([{ role: "assistant", text: "ok" }], "check the news"), [
      { role: "assistant", text: "ok" },
      { role: "user", text: "check the news" },
    ]);
    assert.equal(
      delegationForHandoff([{ role: "user", text: "what's new in AI" }, { role: "assistant", text: "Let me look." }], "search the latest AI news"),
      "<realtime_delegation>\n  <input>search the latest AI news</input>\n  <transcript_delta>user: what's new in AI\nassistant: Let me look.\nuser: search the latest AI news</transcript_delta>\n</realtime_delegation>",
    );
  });
});
