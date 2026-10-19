import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { decodeWorkerLine, durableWorkers, hostEnv, toVoiceEvent } from "./worker.ts";
import { asUser, asWork, step, type VoiceState, type StepWorld } from "./voice.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const contract = JSON.parse(readFileSync(join(HERE, "../../contracts/pi-voice.json"), "utf8")) as {
  worker: { in: Record<string, Record<string, string>>; out: Record<string, Record<string, string>> };
};

function on(over: Partial<Extract<VoiceState, { tag: "on" }>> = {}): VoiceState {
  return {
    tag: "on",
    pid: 1 as never,
    mic: { tag: "open" },
    job: { tag: "none" },
    lastSpoken: null,
    streamingSpoken: "",
    spokenItemId: null,
    lastHeard: null,
    heardPending: null,
    heardItemId: null,
    ...over,
  };
}

function world(over: Partial<StepWorld> = {}): StepWorld {
  return { idle: true, branch: [], now: 1000, ...over };
}

describe("worker wire", () => {
  it("every contract worker.out type decodes to a known event", () => {
    const samples: Record<string, unknown> = {
      ready: { model: "m" },
      progress: { id: "j1", tool: "bash", phase: "start", summary: "ls", active: "bash" },
      job: { id: "j1", status: "working", note: "n" },
      result: { id: "j1", speak: "s", full: "f" },
      status: { id: "j1", payload: "{}" },
      list: { jobs: "[]" },
      error: { message: "boom" },
    };
    for (const type of Object.keys(contract.worker.out)) {
      const decoded = decodeWorkerLine(JSON.stringify({ ev: type, ...(samples[type] as Record<string, unknown>) }));
      assert.ok(!("ignored" in decoded), `${type} must decode: ${JSON.stringify(decoded)}`);
      assert.ok(toVoiceEvent(decoded).tag, `${type} must map to an event`);
    }
  });

  it("refuses an unknown phase or status instead of guessing", () => {
    assert.ok("ignored" in decodeWorkerLine(JSON.stringify({ ev: "progress", id: "j", tool: "bash", phase: "nope" })));
    assert.ok("ignored" in decodeWorkerLine(JSON.stringify({ ev: "job", id: "j", status: "banana" })));
    assert.ok("ignored" in decodeWorkerLine("not json"));
    assert.ok("ignored" in decodeWorkerLine(JSON.stringify({ ev: "future" })));
  });

  it("is on by default, and only the single-worker modes turn it off", () => {
    // Parallel workers are the default runner: each job is its own conversation.
    assert.equal(durableWorkers({}), true, "no setting means parallel");
    assert.equal(durableWorkers({ VOICE_WORKERS: "" }), true, "an empty setting means parallel");
    for (const mode of ["durable", "parallel", "DUAL", "anything-else"]) {
      assert.equal(durableWorkers({ VOICE_WORKERS: mode }), true, `${mode} keeps it on`);
    }
    for (const mode of ["single", "steer", "pi", "off", "OFF", " single "]) {
      assert.equal(durableWorkers({ VOICE_WORKERS: mode }), false, `${mode} falls back to one session`);
    }
  });
});

describe("durable job dispatch", () => {
  it("a second brief is its own job, not a steer, when workers are on", () => {
    const workerState = on({ worker: { model: "haiku" } });
    const first = step(workerState, { tag: "work", id: asWork("j1"), brief: asUser("list files"), effort: "off" }, world());
    const spawn1 = first.effects.find((e) => e.tag === "workerSpawn");
    assert.ok(spawn1 && spawn1.tag === "workerSpawn", "the durable path spawns");
    assert.equal(first.effects.some((e) => e.tag === "sendWork"), false, "it must not steer the Pi session");

    // A second brief while the first is still running: a new job, and the old
    // one is NOT superseded — that is the whole point of the durable path.
    const second = step(first.state, { tag: "work", id: asWork("j2"), brief: asUser("count the tests"), effort: "off" }, world({ idle: false }));
    const spawn2 = second.effects.find((e) => e.tag === "workerSpawn");
    assert.ok(spawn2 && spawn2.tag === "workerSpawn");
    assert.equal(spawn2.id, asWork("j2"));
    assert.equal(
      second.effects.some((e) => e.tag === "sendJobUpdate" && e.status === "superseded"),
      false,
      "a parallel job must not kill the first",
    );
  });

  it("keeps the single-session steer when the worker marker is absent", () => {
    // `state.worker` is set by the host's ready line; without it the reducer
    // steers, which is the VOICE_WORKERS=single path.
    const first = step(on(), { tag: "work", id: asWork("j1"), brief: asUser("list files") }, world());
    const second = step(first.state, { tag: "work", id: asWork("j2"), brief: asUser("count tests") }, world({ idle: false }));
    const work = second.effects.find((e) => e.tag === "sendWork");
    assert.ok(work && work.tag === "sendWork");
    assert.equal(work.deliver, "steer");
    assert.equal(second.effects.some((e) => e.tag === "workerSpawn"), false);
  });

  it("a worker result settles only its own job and is spoken", () => {
    const opened = step(on({ worker: { model: "m" } }), { tag: "work", id: asWork("j1"), brief: asUser("x") }, world());
    const settled = step(opened.state, { tag: "workerResult", id: asWork("j1"), speak: "three files", full: "three files: a, b, c" }, world());
    const post = settled.effects.find((e) => e.tag === "postResult");
    assert.ok(post && post.tag === "postResult");
    assert.equal(post.full, "three files: a, b, c");
    const update = settled.effects.find((e) => e.tag === "sendJobUpdate");
    assert.ok(update && update.tag === "sendJobUpdate" && update.status === "done");
    assert.equal(settled.state.tag === "on" && settled.state.job.tag, "none");
  });

  it("a status read on the durable path asks the host, not the local mirror", () => {
    const state = on({ worker: { model: "m" }, job: { tag: "running", id: asWork("j1"), brief: asUser("x"), startedAt: 0, bound: true, afterEntryId: null, lastNote: null, effort: "off" } });
    const asked = step(state, { tag: "statusRequest", id: "", request: "how is it going" }, world());
    assert.equal(asked.effects.some((e) => e.tag === "workerAsk"), true);
    assert.equal(asked.effects.some((e) => e.tag === "sendSnapshot"), false, "the host owns the state");
  });

  it("a failed worker job is reported, never silently idle", () => {
    const failed = step(on({ worker: { model: "m" } }), { tag: "workerJob", id: asWork("j1"), status: "failed", note: "worker unanswered" }, world());
    const update = failed.effects.find((e) => e.tag === "sendJobUpdate");
    assert.ok(update && update.tag === "sendJobUpdate" && update.status === "failed");
  });
});

describe("worker host readiness", () => {
  it("accepts ready while still connecting, and carries it into the live state", () => {
    // The host is started at spawn time, so its ready line arrives before the
    // child's; dropping it there sent the first job down the single-session path.
    const connecting: VoiceState = { tag: "connecting", mic: { tag: "open" }, startedAt: 0 };
    const ready = step(connecting, { tag: "workerReady", model: "haiku" }, world());
    assert.equal(ready.state.tag, "connecting");
    assert.deepEqual(ready.state.tag === "connecting" ? ready.state.worker : null, { model: "haiku" });

    const live = step(ready.state, { tag: "ready" }, world({ pid: 7 as never }));
    assert.equal(live.state.tag, "on");
    assert.deepEqual(live.state.tag === "on" ? live.state.worker : null, { model: "haiku" });

    // And the first job after that is a parallel spawn, not a steer.
    const job = step(live.state, { tag: "work", id: asWork("j1"), brief: asUser("x") }, world());
    assert.equal(job.effects.some((e) => e.tag === "workerSpawn"), true);
  });

  it("ignores ready when voice is off", () => {
    const off: VoiceState = { tag: "off" };
    assert.deepEqual(step(off, { tag: "workerReady", model: "m" }, world()).effects, []);
  });
});

describe("worker activity wording", () => {
  it("names the activity, never the tool executable", () => {
    const ev = toVoiceEvent({ ev: "progress", id: "j1", tool: "bash", phase: "start", summary: "git status", active: "bash" });
    assert.ok(ev.tag === "jobProgress");
    assert.equal(ev.note, "Pi is running a command (git status)");
    assert.doesNotMatch(ev.note, /Pi is bash/);
    const done = toVoiceEvent({ ev: "progress", id: "j1", tool: "read", phase: "end", summary: "a.ts", active: "" });
    assert.ok(done.tag === "jobProgress");
    assert.equal(done.note, "Pi is finished reading files (a.ts)");
  });
});

describe("worker host failure", () => {
  it("is a warning and a fallback, never a dead call", () => {
    // Now that parallel workers are the default, a host that cannot start must
    // not end the voice call — the single Pi session takes over.
    const live = on({ worker: { model: "m" } });
    const failed = step(live, { tag: "workerError", message: "host exited (2)" }, world());
    assert.equal(failed.state.tag, "on", "the call survives");
    assert.equal(
      (failed.state as { worker?: unknown }).worker,
      undefined,
      "the marker is dropped, so later jobs use the single session",
    );
    const notify = failed.effects.find((e) => e.tag === "notify");
    assert.ok(notify && notify.tag === "notify");
    assert.equal(notify.kind, "warning");
    assert.match(notify.message, /single Pi session/);

    // And the next job is a plain sendWork (steer), not a workerSpawn.
    const job = step(failed.state, { tag: "work", id: asWork("j1"), brief: asUser("x") }, world());
    assert.equal(job.effects.some((e) => e.tag === "workerSpawn"), false);
    assert.equal(job.effects.some((e) => e.tag === "sendWork"), true);
  });

  it("ignores a host error when voice is off", () => {
    assert.deepEqual(step({ tag: "off" }, { tag: "workerError", message: "x" }, world()).effects, []);
  });
});

describe("worker job visibility", () => {
  // A worker job runs outside Pi's session, so without this the transcript is
  // silent while it works. The entry is a custom type, which Pi keeps out of
  // model context — visible to the user, invisible to Pi's model.
  it("draws the brief when a job starts and the outcome when it ends", () => {
    const started = step(
      on({ worker: { model: "m" } }),
      { tag: "work", id: asWork("j1"), brief: asUser("search the web for AI news") },
      world(),
    );
    const face = started.effects.find((e) => e.tag === "upsertFace");
    assert.ok(face && face.tag === "upsertFace", "the job is visible as it starts");
    assert.equal(face.kind, "work");
    assert.equal(face.id, "work:j1");
    assert.equal(face.text, "search the web for AI news");

    const done = step(started.state, { tag: "workerResult", id: asWork("j1"), speak: "s", full: "x".repeat(42) }, world());
    const end = done.effects.find((e) => e.tag === "upsertFace");
    assert.ok(end && end.tag === "upsertFace");
    assert.equal(end.id, "work:j1:done");
    assert.equal(end.text, "done — 42 chars");
  });

  it("shows a failure, so a doomed job is not silent", () => {
    const failed = step(
      on({ worker: { model: "m" } }),
      { tag: "workerJob", id: asWork("j1"), status: "failed", note: "worker unanswered" },
      world(),
    );
    const face = failed.effects.find((e) => e.tag === "upsertFace");
    assert.ok(face && face.tag === "upsertFace");
    assert.equal(face.text, "failed — worker unanswered");
  });

  it("stays silent on the single-session path, where Pi already shows the job", () => {
    const started = step(on(), { tag: "work", id: asWork("j1"), brief: asUser("x") }, world());
    assert.equal(
      started.effects.some((e) => e.tag === "upsertFace"),
      false,
      "no duplicate entry when the job is in Pi's own session",
    );
  });
});

describe("worker host state location", () => {
  const withConfig = (value: string | undefined, run: () => void) => {
    const saved = process.env.PI_VOICE_CONFIG;
    if (value === undefined) delete process.env.PI_VOICE_CONFIG;
    else process.env.PI_VOICE_CONFIG = value;
    try {
      run();
    } finally {
      if (saved === undefined) delete process.env.PI_VOICE_CONFIG;
      else process.env.PI_VOICE_CONFIG = saved;
    }
  };

  it("keeps job state under PI_VOICE_CONFIG, so an isolated run never touches the real one", () => {
    withConfig("/tmp/isolated-config", () => {
      assert.equal(hostEnv({}).VOICE_WORKER_STATE, "/tmp/isolated-config/workers");
    });
  });

  it("lets an explicit VOICE_WORKER_STATE win", () => {
    withConfig("/tmp/isolated-config", () => {
      assert.equal(hostEnv({ VOICE_WORKER_STATE: "/tmp/elsewhere" }).VOICE_WORKER_STATE, "/tmp/elsewhere");
    });
  });

  it("passes the rest of the environment through unchanged", () => {
    withConfig("/tmp/isolated-config", () => {
      assert.equal(hostEnv({ VOICE_WORKER_MODEL: "m" }).VOICE_WORKER_MODEL, "m");
    });
  });
});
