import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  handoffHistoryTurn,
  HistoryStore,
  isDelegationChatter,
  leftoverTurn,
  speaker,
  PACK_END,
  startupPack,
  withoutBrief,
  WORK_RESULT_CHARS,
  workHistoryTurn,
  type DatedTurn,
} from "./history.ts";
import { boundHistory } from "./session.ts";

describe("HistoryStore", () => {
  it("appends and replays newest-first within budget", () => {
    const store = HistoryStore.open(":memory:");
    try {
      store.append({ role: "user", text: "hello" }, "voice");
      store.append({ role: "assistant", text: "hi there" }, "voice");
      assert.equal(store.count(), 2);
      const replay = store.loadReplay();
      assert.equal(replay.length, 1);
      assert.equal(replay[0].role, "user");
      assert.match(replay[0].text, /Latest/);
      assert.match(replay[0].text, /incomplete or stale/);
      assert.match(replay[0].text, /User: hello/);
      assert.match(replay[0].text, /Agent: hi there/);
      assert.doesNotMatch(replay[0].text, /Previous:/);
    } finally {
      store.close();
    }
  });

  it("drops blank turns and caps length", () => {
    const store = HistoryStore.open(":memory:");
    try {
      assert.equal(store.append({ role: "user", text: "   " }, "voice"), null);
      store.append({ role: "user", text: "x".repeat(5000) }, "voice");
      const replay = store.loadReplay();
      assert.equal(replay.length, 1);
      assert.ok(replay[0].text.includes("x".repeat(4096)));
      assert.ok(!replay[0].text.includes("x".repeat(4097)));
    } finally {
      store.close();
    }
  });

  it("hides progress chatter from replay but keeps it stored", () => {
    const store = HistoryStore.open(":memory:");
    try {
      store.append({ role: "user", text: "check the logs" }, "voice");
      store.append({ role: "assistant", text: "I'll have Pi check the logs" }, "progress");
      store.append({ role: "assistant", text: "done, found one error" }, "voice");
      assert.equal(store.count(), 3);
      const replay = store.loadReplay();
      assert.equal(replay.length, 1);
      assert.match(replay[0].text, /check the logs/);
      assert.match(replay[0].text, /done, found one error/);
      assert.doesNotMatch(replay[0].text, /I'll have Pi/);
    } finally {
      store.close();
    }
  });

  it("keeps the brief and a long Pi result in the startup pack", () => {
    const store = HistoryStore.open(":memory:");
    try {
      const answer = "y".repeat(5000);
      const turn = workHistoryTurn("read the config file", answer);
      assert.match(turn.text, /^\[Pi result "read the config file"\]/);
      assert.ok(turn.text.includes(answer));
      store.append({ role: "user", text: "what port?" }, "voice");
      store.append(turn, "work");
      const replay = store.loadReplay();
      assert.equal(replay.length, 1);
      assert.match(replay[0].text, /Pi result "read the config file"/);
      assert.ok(replay[0].text.includes(answer));
      assert.match(replay[0].text, /T\d{2}:\d{2}:\d{2}/);
    } finally {
      store.close();
    }
  });

  it("wraps an orphan assistant turn as background, not an opening line", () => {
    const store = HistoryStore.open(":memory:");
    try {
      store.append({ role: "assistant", text: "orphan reply" }, "voice");
      const replay = store.loadReplay();
      assert.equal(replay.length, 1);
      assert.equal(replay[0].role, "user");
      assert.match(replay[0].text, /Latest/);
      assert.match(replay[0].text, /orphan reply/);
    } finally {
      store.close();
    }
  });
});

describe("startupPack", () => {
  const row = (role: DatedTurn["role"], kind: string, text: string, t: string): DatedTurn => ({ role, kind, text, t });

  it("labels Latest and Previous, dates rows, and drops progress chatter", () => {
    const pack = startupPack([
      row("assistant", "voice", "newest answer", "2026-10-08T18:00:00.000Z"),
      row("user", "voice", "newest question", "2026-10-08T17:59:00.000Z"),
      row("assistant", "work", '[Pi result "check logs"] found one error', "2026-10-08T17:00:00.000Z"),
      row("assistant", "progress", "I'll have Pi check the logs", "2026-10-08T16:59:30.000Z"),
      row("user", "voice", "check the logs", "2026-10-08T16:59:00.000Z"),
    ], 100_000);
    assert.equal(pack.length, 1);
    assert.equal(pack[0].role, "user");
    const text = pack[0].text;
    assert.match(text, /incomplete or stale/);
    assert.match(text, /Do not parrot it/);
    assert.match(text, /not treat it as a new request/);
    assert.match(
      text,
      /Latest:\n2026-10-08T17:59:00.000Z User: newest question\n2026-10-08T18:00:00.000Z Agent: newest answer/,
    );
    assert.match(text, /Previous:\n2026-10-08T16:59:00.000Z User: check the logs/);
    assert.match(text, /2026-10-08T17:00:00.000Z Pi: \[Pi result "check logs"\] found one error/);
    assert.doesNotMatch(text, /I'll have Pi/);
  });

  it("labels a [Pi handoff] row as Agent's brief, not as Pi", () => {
    const pack = startupPack([
      row("assistant", "work", '[Pi result "AI news"] three headlines', "2026-10-08T18:02:00.000Z"),
      row("assistant", "work", "[Pi handoff] Search the latest AI news", "2026-10-08T18:00:30.000Z"),
      row("user", "voice", "search the latest AI news", "2026-10-08T18:00:00.000Z"),
    ], 100_000);
    const text = pack[0].text;
    assert.match(text, /2026-10-08T18:00:30.000Z Agent: \[Pi handoff\] Search the latest AI news/);
    assert.doesNotMatch(text, /Pi: \[Pi handoff\]/);
    assert.match(text, /2026-10-08T18:02:00.000Z Pi: \[Pi result "AI news"\] three headlines/);
    assert.equal(speaker({ role: "assistant", kind: "work", text: handoffHistoryTurn("x").text }), "Agent");
    assert.equal(speaker({ role: "assistant", kind: "work", text: workHistoryTurn("x", "y").text }), "Pi");
  });

  it("closes the pack so the next user turn reads as live, not more background", () => {
    // Without the closing line the model often answered the first real turn
    // after a pack with "I'm here whenever you're ready" (0/14 explicit asks
    // obeyed in a live probe), or spoke its reasoning about the background.
    const pack = startupPack([
      row("assistant", "voice", "Hello.", "2026-10-08T18:00:00.000Z"),
      row("user", "voice", "Hi, reply with just hello.", "2026-10-08T17:59:00.000Z"),
    ], 100_000);
    assert.ok(pack[0].text.endsWith(`\n\n${PACK_END}`), pack[0].text);
    const withPrevious = startupPack([
      row("assistant", "voice", "newest answer", "2026-10-08T18:00:00.000Z"),
      row("user", "voice", "newest question", "2026-10-08T17:59:00.000Z"),
      row("user", "voice", "older question", "2026-10-08T17:00:00.000Z"),
    ], 100_000);
    assert.ok(withPrevious[0].text.endsWith(PACK_END), "end line comes after Previous too");
    assert.match(PACK_END, /user talking now/);
  });

  it("omits an empty Previous section and an empty log", () => {
    const only = startupPack([
      row("assistant", "voice", "hi", "2026-10-08T18:00:00.000Z"),
      row("user", "voice", "hello", "2026-10-08T17:59:00.000Z"),
    ], 100_000);
    assert.match(only[0].text, /Latest:/);
    assert.doesNotMatch(only[0].text, /Previous:/);
    assert.deepEqual(startupPack([], 1000), []);
  });

  it("keeps a long result, including line breaks, instead of the old 600-character slice", () => {
    const turn = workHistoryTurn("ask", "y".repeat(20_000));
    assert.ok(turn.text.length > 4096);
    assert.ok(turn.text.length <= WORK_RESULT_CHARS + 300);
    assert.match(workHistoryTurn("ask", "line one\nline two").text, /line one\nline two/);
  });
});

describe("leftoverTurn", () => {
  it("frames uncommitted speech as history, not a new request", () => {
    const turn = leftoverTurn("user", "  still talking ");
    assert.equal(turn.role, "user");
    assert.equal(turn.text, "[Call ended. Leftover transcript, not a new request.] still talking");
    assert.equal(leftoverTurn("assistant", "partial").role, "assistant");
  });
});

describe("isDelegationChatter", () => {
  it("flags handoff wording on untagged voice rows", () => {
    assert.equal(
      isDelegationChatter({ role: "assistant", kind: "voice", text: "I'll have Pi check that" }),
      true,
    );
    assert.equal(
      isDelegationChatter({ role: "assistant", kind: "voice", text: "Let me look that up" }),
      true,
    );
    assert.equal(
      isDelegationChatter({ role: "assistant", kind: "voice", text: "the answer is 42" }),
      false,
    );
    assert.equal(
      isDelegationChatter({ role: "user", kind: "voice", text: "I'll have Pi check that" }),
      false,
    );
  });
});

describe("withoutBrief", () => {
  it("leaves ordinary text alone", () => {
    assert.equal(withoutBrief("hello"), "hello");
  });
});

describe("boundHistory", () => {
  it("passes small histories through", () => {
    const turns = [{ role: "user" as const, text: "hi" }];
    assert.deepEqual(boundHistory(turns), turns);
  });

  it("keeps a startup pack longer than the old 4096-character slice", () => {
    const text = "z".repeat(5000);
    assert.equal(boundHistory([{ role: "user", text }])[0].text, text);
  });

  it("shortens one oversized pack instead of dropping it", () => {
    const text = "q".repeat(70_000);
    const out = boundHistory([{ role: "user", text }]);
    assert.equal(out.length, 1);
    assert.ok(out[0].text.length > 0);
    assert.ok(out[0].text.length < 64_000);
    assert.ok(Buffer.byteLength(JSON.stringify(out), "utf8") <= 64_000);
  });
});
