import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  HistoryStore,
  isDelegationChatter,
  withoutBrief,
  workHistoryTurn,
} from "./history.ts";
import { boundHistory } from "./session.ts";

describe("HistoryStore", () => {
  it("appends and replays newest-first within budget", () => {
    const store = HistoryStore.open(":memory:");
    try {
      store.append({ role: "user", text: "hello" }, "voice");
      store.append({ role: "assistant", text: "hi there" }, "voice");
      assert.equal(store.count(), 2);
      assert.deepEqual(store.loadReplay(), [
        { role: "user", text: "hello" },
        { role: "assistant", text: "hi there" },
      ]);
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
      assert.equal(replay[0].text.length, 4096);
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
      assert.deepEqual(store.loadReplay(), [
        { role: "user", text: "check the logs" },
        { role: "assistant", text: "done, found one error" },
      ]);
    } finally {
      store.close();
    }
  });

  it("replays work turns without the brief", () => {
    const store = HistoryStore.open(":memory:");
    try {
      const turn = workHistoryTurn("read the config file", "the port is 8766");
      assert.match(turn.text, /^\[Earlier, Pi finished "read the config file"\]/);
      store.append({ role: "user", text: "what port?" }, "voice");
      store.append(turn, "work");
      const replay = store.loadReplay();
      assert.equal(replay.length, 2);
      assert.equal(replay[1].text, "[Earlier, Pi finished a job] the port is 8766");
    } finally {
      store.close();
    }
  });

  it("never starts a replay with an assistant turn", () => {
    const store = HistoryStore.open(":memory:");
    try {
      store.append({ role: "assistant", text: "orphan reply" }, "voice");
      assert.deepEqual(store.loadReplay(), []);
    } finally {
      store.close();
    }
  });
});

describe("isDelegationChatter", () => {
  it("flags handoff wording on untagged voice rows", () => {
    assert.equal(
      isDelegationChatter({ role: "assistant", kind: "voice", text: "I'll have Pi check that" }),
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
});
