import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { Background, parseBg, type BgHost } from "./bg.ts";
import type { VoiceEvent, WorkId } from "./voice.ts";

function rig(startFails = false) {
  const notes: string[] = [];
  const delivered: Array<[string, string]> = [];
  const calls: string[] = [];
  let emit: (event: VoiceEvent) => void = () => {};
  let starts = 0;
  const host: BgHost = {
    spawnJob: (id, brief) => calls.push(`spawn ${id} ${brief}`),
    stopJob: (id) => calls.push(`stop ${id}`),
    quit: () => calls.push("quit"),
  };
  const bg = new Background(
    { notify: (t) => notes.push(t), deliver: (id, t) => delivered.push([id, t]) },
    (onEvent) => {
      if (startFails) throw new Error("no host");
      starts++;
      emit = onEvent;
      return host;
    },
  );
  return { bg, notes, delivered, calls, send: (e: VoiceEvent) => emit(e), starts: () => starts };
}

describe("parseBg", () => {
  it("reads the subcommands, and treats anything else as a task", () => {
    assert.deepEqual(parseBg(""), { kind: "list" });
    assert.deepEqual(parseBg("list"), { kind: "list" });
    assert.deepEqual(parseBg("stop bg-2"), { kind: "stop", id: "bg-2" });
    assert.deepEqual(parseBg("stop"), { kind: "help" });
    assert.deepEqual(parseBg("fix the failing test"), { kind: "start", brief: "fix the failing test" });
    assert.deepEqual(parseBg("list the files"), { kind: "start", brief: "list the files" });
  });
});

describe("Background", () => {
  it("starts one host and gives each task its own id", () => {
    const r = rig();
    r.bg.run({ kind: "start", brief: "one" });
    r.bg.run({ kind: "start", brief: "two" });
    assert.equal(r.starts(), 1);
    assert.deepEqual(r.calls, ["spawn bg-1 one", "spawn bg-2 two"]);
  });

  it("delivers a finished answer into the chat", () => {
    const r = rig();
    r.bg.run({ kind: "start", brief: "one" });
    r.send({ tag: "workerResult", id: "bg-1" as WorkId, speak: "short", full: "the full answer" });
    assert.deepEqual(r.delivered, [["bg-1", "the full answer"]]);
    assert.equal(r.bg.jobs.get("bg-1")?.status, "done");
  });

  it("stops only a running task", () => {
    const r = rig();
    r.bg.run({ kind: "stop", id: "bg-9" });
    assert.match(r.notes.at(-1) ?? "", /No running/);
    r.bg.run({ kind: "start", brief: "one" });
    r.bg.run({ kind: "stop", id: "bg-1" });
    assert.ok(r.calls.includes("stop bg-1"));
  });

  it("marks running tasks failed when the host dies, and restarts on the next task", () => {
    const r = rig();
    r.bg.run({ kind: "start", brief: "one" });
    r.send({ tag: "workerError", message: "worker host exited (1)" });
    assert.equal(r.bg.jobs.get("bg-1")?.status, "failed");
    r.bg.run({ kind: "start", brief: "two" });
    assert.equal(r.starts(), 2);
  });

  it("reports a host that cannot start instead of throwing", () => {
    const r = rig(true);
    r.bg.run({ kind: "start", brief: "one" });
    assert.match(r.notes.at(-1) ?? "", /Could not start/);
    assert.equal(r.bg.jobs.size, 0);
  });

  it("lists tasks", () => {
    const r = rig();
    assert.match(r.bg.describe(), /No background tasks/);
    r.bg.run({ kind: "start", brief: "one" });
    assert.match(r.bg.describe(), /bg-1\s+running\s+one/);
  });
});
