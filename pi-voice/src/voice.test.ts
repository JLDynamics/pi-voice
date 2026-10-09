import assert from "node:assert/strict";
import { describe, it } from "node:test";
import type { SessionEntry } from "@earendil-works/pi-coding-agent";
import {
  AGENT_LABEL,
  asUser,
  asWork,
  detectMuteChord,
  faceLines,
  spokenFaceLines,
  sessionFaceLines,
  handleCommand,
  headlessChildEnv,
  muteHint,
  parseLine,
  parseSlash,
  RESULT_MAX,
  step,
  stderrLogPath,
  strip,
  Voice,
  voiceHistory,
  WORK_SECTION,
  type ChildPid,
  type Effect,
  type Job,
  type StepWorld,
  type VoiceState,
} from "./voice.ts";
import { fullResult, lastAssistantText, openJob, settleJob } from "./work.ts";
import { answerFinished, answerForJob, jobIdFromText, jobPrompt, lastUserJobId, toolProgress, webSourceCount } from "./work.ts";

function pid(n: number): ChildPid {
  return n as ChildPid;
}

function tags(effects: readonly Effect[]): string[] {
  return effects.map((effect) => effect.tag);
}

function world(over: Partial<StepWorld> = {}): StepWorld {
  return { idle: true, branch: [], now: 0, ...over };
}

function off(): VoiceState {
  return { tag: "off" };
}

function connecting(): VoiceState {
  return { tag: "connecting", mic: { tag: "open" }, startedAt: 0 };
}

function runningJob(brief: string, over: Partial<Extract<Job, { tag: "running" }>> = {}): Job {
  return {
    tag: "running",
    id: asWork("w1"),
    brief: asUser(brief),
    startedAt: 0,
    bound: false,
    afterEntryId: "leaf0",
    lastNote: null,
    ...over,
  };
}

function on(over: Partial<Extract<VoiceState, { tag: "on" }>> = {}): VoiceState {
  return {
    tag: "on",
    pid: pid(4242),
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

/** An assistant entry carrying Pi's stop reason ("stop" ends the turn, "toolUse" is mid-turn). */
function answerEntry(id: string, content: string, stopReason: string): SessionEntry {
  return {
    type: "message",
    id,
    parentId: null,
    timestamp: "t",
    message: { role: "assistant", content, stopReason, timestamp: 0 },
  } as unknown as SessionEntry; // partial fixture: no api/provider/usage, only what work.ts reads
}

function message(
  id: string,
  role: "user" | "assistant",
  content: unknown,
  parentId: string | null = null,
): SessionEntry {
  return {
    type: "message",
    id,
    parentId,
    timestamp: "t",
    message: { role, content, timestamp: 0 },
  } as SessionEntry;
}

describe("parseSlash", () => {
  it('"" is toggle', () => {
    assert.deepEqual(parseSlash(""), { tag: "toggle" });
    assert.deepEqual(parseSlash("   "), { tag: "toggle" });
  });

  it("stop", () => {
    assert.deepEqual(parseSlash("stop"), { tag: "stop" });
  });

  it("mute", () => {
    assert.deepEqual(parseSlash("mute"), { tag: "toggleMic" });
  });

  it("unmute", () => {
    assert.deepEqual(parseSlash("unmute"), { tag: "setMic", closed: false });
  });

  it("unknown", () => {
    assert.deepEqual(parseSlash("whisper"), { tag: "unknown", raw: "whisper" });
  });
});

describe("completions", () => {
  const voice = Voice.attach({} as never);

  it("empty prefix is no list so Enter starts instead of mute", () => {
    assert.deepEqual(voice.completions(""), []);
    assert.deepEqual(voice.completions("   "), []);
  });

  it("filters mute after a typed prefix", () => {
    assert.deepEqual(
      voice.completions("m").map((item) => item.value),
      ["mute"],
    );
  });

  it("calls the assistant Agent in the menu, not the Luna persona name", () => {
    const described = voice.completions("mu").concat(voice.completions("un")).map((item) => item.description);
    assert.deepEqual(described, ["Mute Agent's mic", "Unmute Agent's mic"]);
  });
});

it("a failed request tells the user to ask Agent to retry", () => {
  const next = step(on(), { tag: "requestError", message: "Pi could not start." }, world());
  const notify = next.effects.find((effect) => effect.tag === "notify");
  assert.ok(notify && notify.tag === "notify" && notify.message.endsWith("You can ask Agent to retry."));
});

describe("detectMuteChord", () => {
  it("is never ctrl+m", () => {
    const chord = detectMuteChord();
    assert.ok(chord === "alt+m" || chord === "ctrl+shift+m", chord);
    assert.notEqual(chord, "ctrl+m");
  });

  it("widget copy names only the bound chord", () => {
    const chord = detectMuteChord();
    const hint = muteHint();
    assert.equal(hint, `/voice mute   ${chord}   /voice stop`);
    assert.ok(!hint.split(/\s+/).includes("ctrl+m"));
  });

  it("Kitty env binds ctrl+shift+m", () => {
    const prevTerm = process.env.TERM;
    const prevProgram = process.env.TERM_PROGRAM;
    const prevKitty = process.env.KITTY_WINDOW_ID;
    process.env.TERM = "xterm-kitty";
    process.env.TERM_PROGRAM = "kitty";
    process.env.KITTY_WINDOW_ID = "1";
    try {
      assert.equal(detectMuteChord(), "ctrl+shift+m");
      assert.equal(muteHint(), "/voice mute   ctrl+shift+m   /voice stop");
    } finally {
      restoreEnv("TERM", prevTerm);
      restoreEnv("TERM_PROGRAM", prevProgram);
      restoreEnv("KITTY_WINDOW_ID", prevKitty);
    }
  });

  it("legacy terminals bind alt+m", () => {
    const prevTerm = process.env.TERM;
    const prevProgram = process.env.TERM_PROGRAM;
    const prevKitty = process.env.KITTY_WINDOW_ID;
    process.env.TERM = "xterm-256color";
    process.env.TERM_PROGRAM = "Apple_Terminal";
    delete process.env.KITTY_WINDOW_ID;
    try {
      assert.equal(detectMuteChord(), "alt+m");
      assert.equal(muteHint(), "/voice mute   alt+m   /voice stop");
    } finally {
      restoreEnv("TERM", prevTerm);
      restoreEnv("TERM_PROGRAM", prevProgram);
      restoreEnv("KITTY_WINDOW_ID", prevKitty);
    }
  });
});

function restoreEnv(name: string, value: string | undefined): void {
  if (value === undefined) delete process.env[name];
  else process.env[name] = value;
}

describe("faceLines", () => {
  it("shows saved voice entries in the normal terminal during a call", () => {
    assert.deepEqual(sessionFaceLines(AGENT_LABEL, "Hello there", 40, true), ["Agent  Hello there"]);
    assert.deepEqual(sessionFaceLines(AGENT_LABEL, "Hello there", 40, false), ["Agent  Hello there"]);
  });
  it("wraps the crash-log Luna reply so no line exceeds the terminal", () => {
    const spoken =
      "Ha, fair enough — consider this an open audition. No pressure on my end. So where do you want to start? I can hold up my end on most things: ideas, trivia, arguments you want to test out, or just whatever's rattling around in your head on a Sunday. Or if you want to stress-test me, throw something tricky at me and see what happens.";
    assert.equal(`Agent  ${spoken}`.length, 340);
    const lines = faceLines(AGENT_LABEL, spoken, 183);
    assert.ok(lines.length >= 2);
    for (const line of lines) assert.ok(line.length <= 183, line);
    assert.ok(lines.join(" ").includes("open audition"));
    assert.ok(lines.join(" ").includes("stress-test"));
  });

  it("hard-breaks a single token wider than the terminal", () => {
    const lines = faceLines("you", "x".repeat(50), 10);
    assert.deepEqual(
      lines.every((line) => line.length <= 10),
      true,
    );
    assert.ok(lines.length >= 5);
  });
});

it("marks the native Pi renderer active only while voice is running", () => {
  const voice = Voice.attach({} as never);
  const commit = (voice as unknown as { commit: (next: { state: VoiceState; effects: Effect[] }) => void }).commit.bind(voice);
  commit({ state: on(), effects: [] });
  assert.equal((globalThis as typeof globalThis & { __piVoiceActive?: boolean }).__piVoiceActive, true);
  commit({ state: off(), effects: [] });
  assert.equal((globalThis as typeof globalThis & { __piVoiceActive?: boolean }).__piVoiceActive, false);
});

it("streams Luna into one normal transcript entry and restores its final text", () => {
  type Entry = { kind: string; id?: string; text: string };
  const entries: Entry[] = [];
  let renderEntry: ((entry: { data: Entry }) => { render(width: number): string[] } | undefined) | undefined;
  const pi = {
    appendEntry: (_type: string, data: Entry) => entries.push(data),
    registerEntryRenderer: (_type: string, renderer: typeof renderEntry) => { renderEntry = renderer; },
  };
  const voice = Voice.attach(pi as never);
  (voice as unknown as { state: VoiceState }).state = on();
  let redraws = 0;
  const ctx = { isIdle: () => true, sessionManager: { getBranch: () => [], getLeafId: () => null },
    ui: { setWidget(_key: string, content?: (tui: { requestRender(): void }) => unknown) {
      if (typeof content === "function") content({ requestRender() { redraws++; } });
    } } } as never;
  (voice as unknown as { apply: (effect: Effect, ctx: unknown) => void }).apply(
    { tag: "paint", strip: { line1: "voice", line2: "/voice stop" } }, ctx);
  voice.feed({ tag: "spokenDelta", text: "First", itemId: "reply-1" }, ctx);
  assert.equal(entries.length, 1);
  assert.equal(entries[0]?.kind, "spoken-live");
  const component = renderEntry?.({ data: entries[0]! });
  assert.match(component?.render(40).join(" ") ?? "", /Agent  First/);
  voice.feed({ tag: "spokenDelta", text: " sentence", itemId: "reply-1" }, ctx);
  assert.equal(entries.length, 1);
  assert.match(component?.render(40).join(" ") ?? "", /First sentence/);
  voice.feed({ tag: "spoken", text: "First sentence.", itemId: "reply-1" }, ctx);
  assert.deepEqual(entries.map((entry) => entry.kind), ["spoken-live", "spoken-final"]);
  assert.equal(renderEntry?.({ data: entries[1]! }), undefined);
  assert.match(component?.render(40).join(" ") ?? "", /First sentence\./);
  assert.equal(redraws, 3);

  let replayRenderer: typeof renderEntry;
  Voice.attach({ registerEntryRenderer: (_type: string, renderer: typeof renderEntry) => { replayRenderer = renderer; } } as never);
  const replay = replayRenderer?.({ data: entries[0]! });
  replayRenderer?.({ data: entries[1]! });
  assert.match(replay?.render(40).join(" ") ?? "", /First sentence\./);
  assert.deepEqual(spokenFaceLines("First line\nSecond line", 40), ["Agent  First line", "       Second line"]);
});

it("places the user's live transcript before Luna and updates it in place", () => {
  type Entry = { kind: string; id?: string; text: string };
  const entries: Entry[] = [];
  let renderEntry: ((entry: { data: Entry }) => { render(width: number): string[] } | undefined) | undefined;
  const pi = {
    appendEntry: (_type: string, data: Entry) => entries.push(data),
    registerEntryRenderer: (_type: string, renderer: typeof renderEntry) => { renderEntry = renderer; },
  };
  const voice = Voice.attach(pi as never);
  (voice as unknown as { state: VoiceState }).state = on();
  const ctx = { isIdle: () => true, sessionManager: { getBranch: () => [], getLeafId: () => null },
    ui: { setWidget() {} } } as never;
  voice.feed({ tag: "heard", text: asUser("Hello"), itemId: "turn-1" }, ctx);
  const userLine = renderEntry?.({ data: entries[0]! });
  voice.feed({ tag: "heard", text: asUser("Hello, how are you?"), itemId: "turn-1" }, ctx);
  voice.feed({ tag: "spokenDelta", text: "I'm well", itemId: "reply-1" }, ctx);
  assert.deepEqual(entries.map((entry) => entry.kind), ["heard-live", "spoken-live"]);
  assert.match(userLine?.render(80).join(" ") ?? "", /You  Hello, how are you\?/);
  voice.feed({ tag: "spoken", text: "I'm well.", itemId: "reply-1" }, ctx);
  assert.deepEqual(entries.map((entry) => entry.kind), ["heard-live", "spoken-live", "heard-final", "spoken-final"]);
  assert.equal(renderEntry?.({ data: entries[2]! }), undefined);
  assert.equal(renderEntry?.({ data: entries[3]! }), undefined);
  assert.match(userLine?.render(80).join(" ") ?? "", /You  Hello, how are you\?/);
});

describe("handleCommand", () => {
  it("conversation commands are owned by slash, never the reducer", () => {
    for (const command of [{ tag: "newConversation" }, { tag: "resumeConversation", selector: null }] as const) {
      const next = handleCommand(on(), command);
      assert.equal(next.state.tag, "on");
      assert.deepEqual(next.effects, []);
    }
  });
  it("off + toggle → connecting + reap + spawn", () => {
    const next = handleCommand(off(), { tag: "toggle" });
    assert.equal(next.state.tag, "connecting");
    if (next.state.tag !== "connecting") throw new Error("expected connecting");
    assert.equal(next.state.mic.tag, "open");
    assert.deepEqual(tags(next.effects), ["reapOrphans", "spawn", "paint"]);
    const paint = next.effects.find((effect) => effect.tag === "paint");
    assert.equal(paint && paint.tag === "paint" ? paint.strip?.line1 : undefined, "voice  …  connecting");
    assert.equal(paint && paint.tag === "paint" ? paint.strip?.line2 : undefined, muteHint());
  });

  it("on + toggle → off + quit", () => {
    const next = handleCommand(on(), { tag: "toggle" });
    assert.equal(next.state.tag, "off");
    assert.deepEqual(tags(next.effects), ["quitChild", "paint"]);
    const paint = next.effects.find((effect) => effect.tag === "paint");
    assert.equal(paint && paint.tag === "paint" ? paint.strip : "x", null);
  });

  it("stop flushes uncommitted transcript as leftover history", () => {
    const next = handleCommand(on({
      heardPending: asUser("what about the port"),
      heardItemId: "h1",
      streamingSpoken: "The port is",
      spokenItemId: "s1",
      lastSpoken: "already saved",
    }), { tag: "stop" });
    assert.equal(next.state.tag, "off");
    assert.deepEqual(tags(next.effects), ["recordTurn", "recordTurn", "quitChild", "paint"]);
    const records = next.effects.filter((effect) => effect.tag === "recordTurn");
    assert.equal(records[0] && records[0].tag === "recordTurn" ? records[0].turn.role : undefined, "user");
    assert.match(records[0] && records[0].tag === "recordTurn" ? records[0].turn.text : "", /Leftover transcript, not a new request/);
    assert.match(records[0] && records[0].tag === "recordTurn" ? records[0].turn.text : "", /what about the port/);
    assert.equal(records[1] && records[1].tag === "recordTurn" ? records[1].turn.role : undefined, "assistant");
    assert.match(records[1] && records[1].tag === "recordTurn" ? records[1].turn.text : "", /The port is/);
    assert.doesNotMatch(records.map((effect) => effect.tag === "recordTurn" ? effect.turn.text : "").join("\n"), /already saved/);
  });

  it("a child failure flushes the same leftover transcript", () => {
    const next = step(
      on({ heardPending: asUser("wait"), streamingSpoken: "Sure" }),
      { tag: "childExit", code: 1 },
      world(),
    );
    assert.equal(next.state.tag, "off");
    assert.deepEqual(tags(next.effects).slice(0, 4), ["recordTurn", "recordTurn", "quitChild", "paint"]);
    assert.equal(next.effects.at(-1)?.tag, "notify");
  });

  it("stop + off → no-op", () => {
    const next = handleCommand(off(), { tag: "stop" });
    assert.equal(next.state.tag, "off");
    assert.deepEqual(next.effects, []);
  });

  it("mute while off is the only writer of Voice is off", () => {
    const next = handleCommand(off(), { tag: "toggleMic" });
    assert.equal(next.state.tag, "off");
    assert.deepEqual(next.effects, [
      { tag: "notify", message: "Voice is off. Type /voice to start.", kind: "warning" },
    ]);
  });

  it("mute while on closes the mic and paints muted", () => {
    const next = handleCommand(on(), { tag: "toggleMic" });
    if (next.state.tag !== "on") throw new Error("expected on");
    assert.equal(next.state.mic.tag, "closed");
    assert.deepEqual(tags(next.effects), ["setMic", "paint"]);
    const setMic = next.effects.find((effect) => effect.tag === "setMic");
    assert.equal(setMic && setMic.tag === "setMic" ? setMic.closed : undefined, true);
    const paint = next.effects.find((effect) => effect.tag === "paint");
    assert.equal(paint && paint.tag === "paint" ? paint.strip?.line1 : undefined, "voice  ◌  muted");
    assert.equal(
      paint && paint.tag === "paint" ? paint.strip?.line2 : undefined,
      `/voice unmute   ${detectMuteChord()}   /voice stop`,
    );
  });

  it("setMic to the current value is a no-op", () => {
    const state = on({ mic: { tag: "closed" } });
    const next = handleCommand(state, { tag: "setMic", closed: true });
    assert.deepEqual(next.state, state);
    assert.deepEqual(next.effects, []);
  });
});

describe("step ready / speechStarted", () => {
  it("ready: connecting → on talking", () => {
    const next = step(connecting(), { tag: "ready" }, world({ pid: pid(99), now: 1000 }));
    assert.equal(next.state.tag, "on");
    if (next.state.tag !== "on") throw new Error("expected on");
    assert.equal(next.state.pid, 99);
    assert.equal(next.state.job.tag, "none");
    assert.deepEqual(tags(next.effects), ["paint"]);
    const paint = next.effects.find((effect) => effect.tag === "paint");
    assert.equal(paint && paint.tag === "paint" ? paint.strip?.line1 : undefined, "voice  ●  talking");
  });

  it("speechStarted is a no-op", () => {
    const state = on();
    const next = step(state, { tag: "speechStarted" }, world());
    assert.deepEqual(next.state, state);
    assert.deepEqual(next.effects, []);
  });
});

describe("heard", () => {
  it("holds the words instead of painting them, and never sendUserMessage", () => {
    const next = step(on(), { tag: "heard", text: asUser("what's Tesla at?") }, world({ now: 1000 }));
    if (next.state.tag !== "on") throw new Error("expected on");
    assert.equal(next.state.lastHeard, "what's Tesla at?");
    assert.equal(next.state.heardPending, "what's Tesla at?");
    // The first transcript opens one entry; revisions replace it.
    assert.deepEqual(tags(next.effects), ["upsertFace", "paint"]);
    assert.ok(!tags(next.effects).includes("sendUser"));
    assert.ok(!tags(next.effects).includes("sendWork"));
    assert.ok(!tags(next.effects).includes("speak"));
  });

  it("a revision of the same utterance replaces the held text without painting twice", () => {
    const first = step(on(), { tag: "heard", text: asUser("what's"), itemId: "i1" }, world({ now: 1000 }));
    const second = step(
      first.state,
      { tag: "heard", text: asUser("what's Tesla at?"), itemId: "i1" },
      world({ now: 1200 }),
    );
    if (second.state.tag !== "on") throw new Error("expected on");
    assert.equal(second.state.heardPending, "what's Tesla at?");
    assert.deepEqual(tags(second.effects), ["upsertFace", "paint"], "a revision updates the same entry");
  });

  it("a rewritten revision is still one utterance, not two", () => {
    // STT rewrites words it already returned, so the later text is not an
    // extension of the earlier one. Observed live:
    //   "Yeah, that's good. Yeah, it's just uh..."
    //   "Yeah, that's good, you know, yeah. Okay, so, yeah, I don't..."
    const first = step(
      on(),
      { tag: "heard", text: asUser("Yeah, that's good. Yeah, it's just uh"), itemId: "i9" },
      world({ now: 1000 }),
    );
    const second = step(
      first.state,
      { tag: "heard", text: asUser("Yeah, that's good, you know, yeah. Okay, so"), itemId: "i9" },
      world({ now: 1400 }),
    );
    assert.deepEqual(tags(second.effects), ["upsertFace", "paint"], "a rewrite must update the same entry");
    if (second.state.tag !== "on") throw new Error("expected on");
    assert.equal(second.state.heardPending, "Yeah, that's good, you know, yeah. Okay, so");
  });

  it("a new utterance commits the one before it, so an unanswered turn is not lost", () => {
    const first = step(on(), { tag: "heard", text: asUser("can you hear me"), itemId: "i1" }, world({ now: 1000 }));
    const second = step(
      first.state,
      { tag: "heard", text: asUser("hello again"), itemId: "i2" },
      world({ now: 9000 }),
    );
    const faces = second.effects.filter((effect) => effect.tag === "upsertFace");
    assert.deepEqual(faces.map((effect) => [effect.id, effect.final]), [["i1", true], ["i2", false]]);
  });

  it("the held words land once when Luna answers", () => {
    const heard = step(on(), { tag: "heard", text: asUser("what's Tesla at?") }, world({ now: 1000 }));
    const spoken = step(heard.state, { tag: "spoken", text: "Around $420." }, world({ now: 2000 }));
    if (spoken.state.tag !== "on") throw new Error("expected on");
    assert.equal(spoken.state.heardPending, null, "the held text is cleared once painted");
    const faces = spoken.effects.filter((effect) => effect.tag === "upsertFace");
    assert.deepEqual(
      faces.map((f) => (f.tag === "upsertFace" ? [f.kind, f.text] : null)),
      [["heard", "what's Tesla at?"], ["spoken", "Around $420."]],
      "your whole sentence, then hers, in order",
    );
  });

  it("drops heard while the mic is closed", () => {
    const state = on({ mic: { tag: "closed" } });
    const next = step(state, { tag: "heard", text: asUser("family lunch") }, world());
    assert.deepEqual(next.state, state);
    assert.deepEqual(next.effects, []);
  });
});

describe("work", () => {
  it("records the spoken request before Pi's search message and does not repeat it", () => {
    const next = step(
      on({ heardPending: asUser("Search the latest AI news"), heardItemId: "turn-1" }),
      { tag: "work", id: asWork("news-1"), brief: asUser("Find AI news") },
      world(),
    );
    assert.deepEqual(tags(next.effects).slice(0, 2), ["upsertFace", "recordTurn"]);
    assert.ok(tags(next.effects).includes("sendWork"));
    if (next.state.tag !== "on") throw new Error("expected on");
    assert.equal(next.state.heardPending, null);
    const spoken = step(next.state, { tag: "spoken", text: "Pi is searching" }, world());
    assert.deepEqual(spoken.effects.filter((effect) => effect.tag === "upsertFace").map((effect) => effect.kind), ["spoken"]);
  });

  it("opens a job and sendWork, never speak", () => {
    const next = step(
      on(),
      { tag: "work", id: asWork("w1"), brief: asUser("Tesla stock price") },
      world({ now: 50, leafId: "leaf0" }),
    );
    if (next.state.tag !== "on" || next.state.job.tag !== "running") throw new Error("expected running");
    assert.equal(next.state.job.id, "w1");
    assert.equal(next.state.job.brief, "Tesla stock price");
    assert.equal(next.state.job.bound, false);
    assert.equal(next.state.job.afterEntryId, "leaf0");
    assert.deepEqual(tags(next.effects), ["saveDelegation", "sendWork", "recordTurn", "paint"]);
    const recorded = next.effects.find((effect) => effect.tag === "recordTurn");
    assert.equal(recorded && recorded.tag === "recordTurn" ? recorded.turnKind : undefined, "work");
    assert.match(recorded && recorded.tag === "recordTurn" ? recorded.turn.text : "", /^\[Pi handoff\] Tesla stock price$/);
    const send = next.effects.find((effect) => effect.tag === "sendWork");
    assert.equal(send && send.tag === "sendWork" ? send.deliver : undefined, "plain");
    assert.equal(send && send.tag === "sendWork" ? send.brief : undefined, "Tesla stock price");
    assert.ok(!tags(next.effects).includes("speak"));
    assert.ok(!tags(next.effects).includes("sendUser"));
    const paint = next.effects.find((effect) => effect.tag === "paint");
    assert.equal(
      paint && paint.tag === "paint" ? paint.strip?.line1 : undefined,
      "voice  ●  talking · pi working · 0s",
    );
  });

  it("drops work while the mic is closed and marks the id dropped", () => {
    const state = on({ mic: { tag: "closed" } });
    const next = step(state, { tag: "work", id: asWork("w1"), brief: asUser("Tesla") }, world());
    assert.equal(next.state.tag, "on");
    if (next.state.tag !== "on") throw new Error("expected on");
    assert.equal(next.state.job.tag, "none", "nothing starts while muted");
    assert.deepEqual(tags(next.effects), ["sendJobUpdate"]);
    const update = next.effects.find((effect) => effect.tag === "sendJobUpdate");
    assert.equal(update && update.tag === "sendJobUpdate" ? update.status : undefined, "dropped");
  });

  it("second work steers Pi without aborting the live operation", () => {
    const next = openJob(
      on({ job: runningJob("first") }),
      asWork("w2"),
      asUser("now Chrome"),
      world({ idle: false, now: 9, leafId: "leaf1" }),
    );
    if (next.state.tag !== "on" || next.state.job.tag !== "running") throw new Error("expected running");
    assert.equal(next.state.job.id, "w2");
    assert.equal(next.state.job.brief, "now Chrome");
    const send = next.effects.find((effect) => effect.tag === "sendWork");
    assert.equal(send && send.tag === "sendWork" ? send.deliver : undefined, "steer");
    const superseded = next.effects.find((effect) => effect.tag === "sendJobUpdate");
    assert.equal(superseded && superseded.tag === "sendJobUpdate" ? superseded.status : undefined, "superseded");
    assert.equal(superseded && superseded.tag === "sendJobUpdate" ? superseded.id : undefined, "w1");
    assert.ok(!tags(next.effects).includes("speak"));
  });
  it("a correction preserves the active Pi turn and steers the new routing id", () => {
    const previous = on({ job: runningJob("old search", { bound: true }) });
    const branch = [message("old", "user", jobPrompt(asWork("w1"), asUser("old search")))];
    const next = step(previous, { tag: "work", id: asWork("w2"), brief: asUser("new task") },
      world({ idle: false, branch }));
    assert.ok(!tags(next.effects).includes("abortWork"));
    assert.ok(tags(next.effects).includes("sendWork"));
  });
});

describe("spoken", () => {
  it("does not append a stale final to a newer response", () => {
    const first = step(on(), { tag: "spokenDelta", text: "Old", itemId: "old" }, world());
    const next = step(first.state, { tag: "spokenDelta", text: "New", itemId: "new" }, world());
    assert.deepEqual(next.effects.filter((effect) => effect.tag === "upsertFace").map((effect) => [effect.id, effect.text, effect.final]), [
      ["old", "Old", true], ["new", "New", false],
    ]);
    const stale = step(next.state, { tag: "spoken", text: "Old", itemId: "old" }, world());
    assert.deepEqual(stale.effects, []);
    const final = step(stale.state, { tag: "spoken", text: "New answer", itemId: "new" }, world());
    assert.deepEqual(final.effects.filter((effect) => effect.tag === "upsertFace").map((effect) => [effect.id, effect.text, effect.final]), [
      ["new", "New answer", true],
    ]);
  });
  it("updates Luna's live text before the final entry arrives", () => {
    const first = step(on(), { tag: "spokenDelta", text: "Hello" }, world());
    const second = step(first.state, { tag: "spokenDelta", text: " there" }, world());
    if (second.state.tag !== "on") throw new Error("expected on");
    assert.equal(second.state.streamingSpoken, "Hello there");
    assert.deepEqual(tags(second.effects), ["upsertFace"]);
    const final = step(second.state, { tag: "spoken", text: "Hello there" }, world());
    if (final.state.tag !== "on") throw new Error("expected on");
    assert.equal(final.state.streamingSpoken, "");
    assert.deepEqual(tags(final.effects), ["upsertFace", "paint", "recordTurn"]);
  });
  it("keeps the spoken fragment when the user interrupts and rejects the late final", () => {
    const started = step(on(), { tag: "spokenDelta", text: "I found three", itemId: "reply-1" }, world());
    const interrupted = step(started.state, { tag: "speechStarted" }, world());
    assert.deepEqual(interrupted.effects.filter((effect) => effect.tag === "upsertFace").map((effect) => [effect.text, effect.final]),
      [["I found three", true]]);
    assert.equal(tags(interrupted.effects).filter((tag) => tag === "upsertFace").length, 1);
    const late = step(interrupted.state, { tag: "spoken", text: "I found three stories and more", itemId: "reply-1" }, world());
    assert.deepEqual(late.effects, []);
  });
  it("is the one writer of lastSpoken", () => {
    const next = step(on(), { tag: "spoken", text: "Hey. What's up?" }, world());
    if (next.state.tag !== "on") throw new Error("expected on");
    assert.equal(next.state.lastSpoken, "Hey. What's up?");
    assert.deepEqual(tags(next.effects), ["upsertFace", "paint", "recordTurn"]);
    const face = next.effects.find((effect) => effect.tag === "upsertFace");
    assert.equal(face && face.tag === "upsertFace" ? face.kind : undefined, "spoken");
    assert.ok(!tags(next.effects).includes("speak"));
  });
});

describe("pause mute", () => {
  it("ready does not schedule a quiet-mute", () => {
    const next = step(connecting(), { tag: "ready" }, world({ pid: pid(99), now: 1000 }));
    if (next.state.tag !== "on") throw new Error("expected on");
    assert.equal(next.state.mic.tag, "open");
    assert.ok(!tags(next.effects).includes("setMic"));
    assert.ok(!tags(next.effects).includes("notify"));
  });

  it("heard after a long gap stays open", () => {
    const next = step(on(), { tag: "heard", text: asUser("still here") }, world({ now: 120_000 }));
    if (next.state.tag !== "on") throw new Error("expected on");
    assert.equal(next.state.mic.tag, "open");
    assert.ok(!tags(next.effects).includes("setMic"));
  });
});

describe("agentSettled", () => {
  it("hands Luna a short preview and retains the complete Pi answer", () => {
    const full = "Outcome first. " + "detail ".repeat(1600) + "Final finding.";
    const branch = [
      message("u1", "user", jobPrompt(asWork("w1"), asUser("research"))),
      message("a1", "assistant", [{ type: "text", text: full }], "u1"),
    ];
    const next = step(on({ job: runningJob("research", { bound: true }) }), { tag: "agentSettled" }, world({ branch }));
    const result = next.effects.find((effect) => effect.tag === "postResult");
    assert.ok(result?.tag === "postResult");
    assert.equal(result.full, full);
    assert.ok(result.speak.length <= RESULT_MAX);
    assert.ok(!result.speak.includes("Final finding."));
  });

  it("wrong brief → no postResult", () => {
    const state = on({
      job: runningJob("what's Tesla at?", { bound: true }),
    });
    const next = step(
      state,
      { tag: "agentSettled" },
      world({
        branch: [
          message("leaf0", "assistant", "prior"),
          message("u1", "user", "typed instead", "leaf0"),
          message("a1", "assistant", [{ type: "text", text: "should not speak" }], "u1"),
        ],
      }),
    );
    assert.deepEqual(next.state, state);
    assert.deepEqual(next.effects, []);
  });

  it("matching brief + bound → postResult Pi's whole answer, never speak", () => {
    const brief = "what's Tesla at?";
    const state = on({
      job: runningJob(brief, { bound: true }),
    });
    const next = step(
      state,
      { tag: "agentSettled" },
      world({
        branch: [
          message("leaf0", "assistant", "prior"),
          message("u1", "user", jobPrompt(asWork("w1"), asUser(brief)), "leaf0"),
          message(
            "a1",
            "assistant",
            [{ type: "text", text: "Tesla is around $420.\n---\nA 679-character dump." }],
            "u1",
          ),
        ],
      }),
    );
    if (next.state.tag !== "on") throw new Error("expected on");
    assert.equal(next.state.job.tag, "none");
    assert.ok(tags(next.effects).includes("postResult"));
    assert.ok(!tags(next.effects).includes("speak"));
    const doneUpdate = next.effects.find((effect) => effect.tag === "sendJobUpdate");
    assert.equal(doneUpdate && doneUpdate.tag === "sendJobUpdate" ? doneUpdate.status : undefined, "done");
    const post = next.effects.find((effect) => effect.tag === "postResult");
    assert.equal(
      post && post.tag === "postResult" ? post.speak : undefined,
      "Tesla is around $420.\n---\nA 679-character dump.",
    );
    assert.equal(post && post.tag === "postResult" ? post.id : undefined, "w1");
    const recorded = next.effects.find((effect) => effect.tag === "recordTurn");
    assert.equal(recorded && recorded.tag === "recordTurn" ? recorded.turnKind : undefined, "work");
    assert.match(recorded && recorded.tag === "recordTurn" ? recorded.turn.text : "", /^\[Pi result /);
    assert.match(recorded && recorded.tag === "recordTurn" ? recorded.turn.text : "", /Tesla is around \$420/);
  });

  it("settling with no answer reports failed, never postResult", () => {
    const brief = "what's Tesla at?";
    const state = on({
      job: runningJob(brief, { bound: true }),
    });
    const next = step(
      state,
      { tag: "agentSettled" },
      world({
        branch: [
          message("leaf0", "assistant", "prior"),
          message("u1", "user", jobPrompt(asWork("w1"), asUser(brief)), "leaf0"),
          message("a1", "assistant", [{ type: "text", text: "   " }], "u1"),
        ],
      }),
    );
    if (next.state.tag !== "on") throw new Error("expected on");
    assert.equal(next.state.job.tag, "none");
    assert.ok(!tags(next.effects).includes("postResult"), "nothing to hand over");
    const update = next.effects.find((effect) => effect.tag === "sendJobUpdate");
    assert.equal(update && update.tag === "sendJobUpdate" ? update.status : undefined, "failed");
  });
});

describe("composerInput", () => {
  it("injects typed text to Luna and keeps a running job", () => {
    const next = step(
      on({ job: runningJob("spoken question") }),
      { tag: "composerInput", text: asUser("OrcaRouter Ternary Bonsai") },
      world(),
    );
    if (next.state.tag !== "on") throw new Error("expected on");
    assert.equal(next.state.job.tag, "running");
    assert.equal(next.state.lastHeard, "OrcaRouter Ternary Bonsai");
    assert.deepEqual(tags(next.effects), ["injectUser", "upsertFace", "recordTurn", "paint"]);
    const inject = next.effects.find((effect) => effect.tag === "injectUser");
    assert.equal(inject && inject.tag === "injectUser" ? inject.text : undefined, "OrcaRouter Ternary Bonsai");
    const face = next.effects.find((effect) => effect.tag === "upsertFace");
    assert.equal(face && face.tag === "upsertFace" ? face.kind : undefined, "heard");
    assert.ok(!tags(next.effects).includes("postResult"));
    assert.ok(!tags(next.effects).includes("sendWork"));
  });

  it("blank composer input is a no-op", () => {
    const state = on();
    const next = step(state, { tag: "composerInput", text: asUser("   ") }, world());
    assert.deepEqual(next.state, state);
    assert.deepEqual(next.effects, []);
  });
  it("does not display a typed message twice if the child echoes it", () => {
    const typed = step(on(), { tag: "composerInput", text: asUser("hello") }, world({ now: 1000 }));
    const echo = step(typed.state, { tag: "heard", text: asUser("hello"), itemId: "child-echo" }, world({ now: 1200 }));
    assert.deepEqual(echo.effects, []);
    const later = step(echo.state, { tag: "heard", text: asUser("hello"), itemId: "real-speech" }, world({ now: 7000 }));
    assert.ok(tags(later.effects).includes("upsertFace"));
  });
});

describe("onInput", () => {
  it("handles interactive text while voice is on so Pi does not take the turn", () => {
    const faces: Array<{ kind: string; text: string }> = [];
    const voice = Voice.attach({
      appendEntry: (_type: string, data: { kind: string; text: string }) => {
        faces.push(data);
      },
    } as never);
    (voice as unknown as { state: VoiceState }).state = on();
    const result = voice.onInput(
      { type: "input", text: "the model name", source: "interactive" },
      {
        isIdle: () => false,
        sessionManager: { getBranch: () => [], getLeafId: () => null },
        ui: { setWidget() {}, notify() {} },
      } as never,
    );
    assert.equal(result.action, "handled");
    assert.deepEqual(faces, [{ kind: "heard", text: "the model name" }]);
  });

  it("lets Pi take typed text when voice is off", () => {
    const voice = Voice.attach({} as never);
    const result = voice.onInput(
      { type: "input", text: "clone this", source: "interactive" },
      { isIdle: () => true } as never,
    );
    assert.equal(result.action, "continue");
  });
});

describe("parseLine", () => {
  it("maps the headless wire into domain events", () => {
    assert.deepEqual(parseLine('{"type":"ready"}'), { tag: "ready" });
    assert.deepEqual(parseLine('{"type":"speech_started"}'), { tag: "speechStarted" });
    assert.deepEqual(parseLine('{"type":"heard","text":"hello","item_id":"item_7"}'), {
      tag: "heard",
      text: "hello",
      itemId: "item_7",
    });
    // An older bridge sends no id; grouping then falls back to one-per-utterance.
    assert.deepEqual(parseLine('{"type":"heard","text":"hello"}'), {
      tag: "heard",
      text: "hello",
      itemId: "",
    });
    assert.deepEqual(parseLine('{"type":"spoken","text":"Hey.","item_id":"response-1"}'), {
      tag: "spoken",
      text: "Hey.",
      itemId: "response-1",
    });
    assert.deepEqual(parseLine('{"type":"spoken_delta","text":"Hey","item_id":"response-1"}'), {
      tag: "spokenDelta",
      text: "Hey",
      itemId: "response-1",
    });
    assert.deepEqual(parseLine('{"type":"work","id":"w1","brief":"Tesla stock price"}'), {
      tag: "work",
      id: "w1",
      brief: "Tesla stock price",
    });
    assert.equal(parseLine("not-json"), undefined);
    assert.equal(parseLine('{"type":"heard","text":"  "}'), undefined);
    assert.equal(parseLine('{"type":"transcript","text":"hello"}'), undefined);
  });
});

describe("spawn env / stderr log", () => {
  it("replays only final voice turns from the current Pi branch", () => {
    const face = (kind: string, text: string) => ({ type: "custom", customType: "pi-voice-face", data: { kind, text } });
    const branch = [
      face("heard-live", "draft"),
      face("heard-final", "We discussed AI news."),
      face("spoken-live", "partial"),
      face("spoken-final", "Yes, AI news."),
      face("work", "research"),
    ] as SessionEntry[];
    assert.deepEqual(voiceHistory(branch), [
      { role: "user", text: "We discussed AI news." },
      { role: "assistant", text: "Yes, AI news." },
    ]);
  });

  it("headless child is VOICE_THINKER=luna", () => {
    const env = headlessChildEnv({ PATH: "/bin" }, [{ role: "user", text: "Earlier topic" }]);
    assert.equal(env.VOICE_THINKER, "luna");
    assert.deepEqual(JSON.parse(env.VOICE_HISTORY!), [{ role: "user", text: "Earlier topic" }]);
    assert.equal(env.PATH, "/bin");
  });

  it("stderr log path is lease-adjacent, not inherit", () => {
    const path = stderrLogPath(pid(4242));
    assert.match(path, /pi-voice\.4242\.stderr\.log$/);
  });
});

describe("strip", () => {
  it("talking widget names the bound chord, never ctrl+m as a token", () => {
    const talking = strip(on());
    assert.equal(talking?.line1, "voice  ●  talking");
    assert.equal(talking?.line2, muteHint());
    assert.ok(!talking?.line2.split(/\s+/).includes("ctrl+m"));
  });

  it("shows how long Pi has been working", () => {
    const running = on({ job: runningJob("Tesla", { startedAt: 0 }) });
    assert.equal(strip(running, 9_000)?.line1, "voice  ●  talking · pi working · 9s");
    assert.equal(strip(running, 80_000)?.line1, "voice  ●  talking · pi working · 1m20s");
  });
});

describe("jobMessage", () => {
  it("binds only the matching job and reports working to Voice.app", () => {
    const opened = step(
      on(),
      { tag: "work", id: asWork("w1"), brief: asUser("Tesla") },
      world({ idle: true, now: 1000 }),
    );
    const unrelated = step(opened.state, { tag: "jobMessage", id: asWork("other") }, world({ now: 1000 }));
    assert.deepEqual(unrelated.effects, []);
    const bound = step(opened.state, { tag: "jobMessage", id: asWork("w1") }, world({ now: 1000 }));
    if (bound.state.tag !== "on" || bound.state.job.tag !== "running") throw new Error("expected running");
    assert.equal(bound.state.job.bound, true);
    const update = bound.effects.find((effect) => effect.tag === "sendJobUpdate");
    assert.equal(update && update.tag === "sendJobUpdate" ? update.status : undefined, "working");
  });
});

describe("jobProgress", () => {
  it("records Pi tool activity and mirrors it for the status channel", () => {
    const opened = step(
      on(),
      { tag: "work", id: asWork("w1"), brief: asUser("Tesla") },
      world({ idle: true, now: 1000 }),
    );
    const bound = step(opened.state, { tag: "jobMessage", id: asWork("w1") }, world());
    const branch = [message("u1", "user", jobPrompt(asWork("w1"), asUser("Tesla")))];
    const noted = step(bound.state, { tag: "jobProgress", note: "Pi is searching the web" }, world({ now: 2000, branch }));
    if (noted.state.tag !== "on" || noted.state.job.tag !== "running") throw new Error("expected running");
    assert.equal(noted.state.job.lastNote, "Pi is searching the web");
    const update = noted.effects.find((effect) => effect.tag === "sendJobUpdate");
    assert.equal(update && update.tag === "sendJobUpdate" ? update.note : undefined, "Pi is searching the web");
    assert.ok(tags(noted.effects).includes("paint"), "elapsed refreshes on activity");
  });

  it("repeats and jobless notes are no-ops", () => {
    const idle = step(on(), { tag: "jobProgress", note: "using bash" }, world());
    assert.deepEqual(idle.effects, []);
    const opened = step(
      on(),
      { tag: "work", id: asWork("w1"), brief: asUser("Tesla") },
      world({ idle: true, now: 1000 }),
    );
    const bound = step(opened.state, { tag: "jobMessage", id: asWork("w1") }, world());
    const branch = [message("u1", "user", jobPrompt(asWork("w1"), asUser("Tesla")))];
    const once = step(bound.state, { tag: "jobProgress", note: "Pi is searching the web" }, world({ now: 2000, branch }));
    const twice = step(once.state, { tag: "jobProgress", note: "Pi is searching the web" }, world({ now: 3000, branch }));
    assert.deepEqual(twice.effects, [], "same note twice sends one update");
    const unrelated = step(once.state, { tag: "jobProgress", note: "Pi is editing files" }, world({ branch: [message("u2", "user", "other work")] }));
    assert.deepEqual(unrelated.effects, [], "unrelated work must not update Luna's job");
  });
});

describe("toolProgress", () => {
  it("reports start, partial results, completion and failure without tool output", () => {
    assert.equal(toolProgress("start", "web_search"), "Pi is searching the web");
    assert.equal(toolProgress("update", "web_search"), "Pi is searching the web; results are arriving");
    assert.equal(toolProgress("end", "web_search"), "Pi finished searching the web; reviewing the result");
    assert.equal(toolProgress("error", "web_search"), "Pi's web search failed; Pi is continuing");
  });
});

it("counts web search sources without copying result text into the compact entry", () => {
  assert.equal(webSourceCount("## Query: AI news\nSource: Article one (https://a.test)\nBody\nSource: Article two (https://b.test)"), 2);
});

describe("onBeforeAgentStart", () => {
  it("injects WORK_SECTION and caps thinking only while a job is running", () => {
    const levels: string[] = ["high"];
    const pi = {
      getThinkingLevel: () => levels[levels.length - 1],
      setThinkingLevel: (level: string) => {
        levels.push(level);
      },
    };
    const voice = Voice.attach(pi as never);
    const event = { prompt: jobPrompt(asWork("w1"), asUser("Tesla")), systemPromptOptions: { sections: {} as Record<string, string> } };
    voice.onBeforeAgentStart(event as never, {} as never);
    assert.equal(event.systemPromptOptions.sections.voice, undefined);
    assert.deepEqual(levels, ["high"]);

    (voice as unknown as { state: VoiceState }).state = on({ job: runningJob("Tesla") });
    voice.onBeforeAgentStart(event as never, {} as never);
    assert.equal(event.systemPromptOptions.sections.voice, WORK_SECTION);
    assert.equal(levels.at(-1), "off");
  });

  it("tells Pi to capture the front window and drive the computer itself", () => {
    assert.match(WORK_SECTION, /screencapture -x -l <id>/);
    assert.match(WORK_SECTION, /kCGWindowNumber/);
    assert.match(WORK_SECTION, /use your read tool on that png/);
    assert.match(WORK_SECTION, /then delete the file/);
    assert.match(WORK_SECTION, /Screen Recording permission/);
    assert.match(WORK_SECTION, /computer-use and browser tools/);
    assert.match(WORK_SECTION, /Accessibility permission/);
    assert.match(WORK_SECTION, /Voice cannot click/);
    assert.doesNotMatch(WORK_SECTION, /screenshot/);
    assert.match(WORK_SECTION, /<realtime_delegation>: <input> is the task, and <transcript_delta> is the voice conversation since the previous handoff/);
  });
});

describe("lastAssistantText", () => {
  it("skips thinking/toolCall and joins text blocks of the last assistant after afterEntryId", () => {
    const branch = [
      message("leaf0", "user", "older"),
      message("a0", "assistant", [{ type: "text", text: "old answer" }], "leaf0"),
      message("u1", "user", "what's on my Chrome tab?", "a0"),
      message(
        "a1",
        "assistant",
        [
          { type: "thinking", thinking: "I should look that up" },
          { type: "toolCall", id: "t1", name: "chrome_tab", arguments: { x: 1 } },
          { type: "text", text: "Your tab " },
          { type: "text", text: "is the weather." },
        ],
        "u1",
      ),
    ];
    assert.equal(lastAssistantText(branch, "u1"), "Your tab is the weather.");
    assert.equal(lastAssistantText(branch, "leaf0"), "Your tab is the weather.");
    assert.equal(lastAssistantText(branch, "a1"), undefined);
  });
});

describe("job identity", () => {
  it("puts the job id on the actual message sent to Pi", () => {
    const sent: string[] = [];
    const voice = Voice.attach({ sendUserMessage: (text: string) => sent.push(text) } as never);
    (voice as unknown as { state: VoiceState }).state = on();
    voice.feed({ tag: "work", id: asWork("w1"), brief: asUser("check the news") }, {
      isIdle: () => true,
      sessionManager: { getBranch: () => [], getLeafId: () => null },
      ui: { setWidget: () => {} },
    } as never);
    assert.deepEqual(sent, [jobPrompt(asWork("w1"), asUser("check the news"))]);
  });

  it("shows only the brief and id in Pi's chat and gives the model each job's delegation", () => {
    const sent: string[] = [];
    const entries: { type: "custom"; customType: string; data: unknown }[] = [];
    const pi = {
      sendUserMessage: (text: string) => sent.push(text),
      appendEntry: (customType: string, data: unknown) => entries.push({ type: "custom", customType, data }),
    };
    const ctx = {
      isIdle: () => true,
      sessionManager: { getBranch: () => entries, getLeafId: () => null },
      ui: { setWidget: () => {} },
    } as never;
    const voice = Voice.attach(pi as never);
    (voice as unknown as { state: VoiceState }).state = on();
    voice.feed({ tag: "heard", text: asUser("what's in the news"), itemId: "h1" }, ctx);
    voice.feed({ tag: "spoken", text: "I'll have Pi check.", itemId: "s1" }, ctx);
    voice.feed({ tag: "work", id: asWork("w1"), brief: asUser("check the news") }, ctx);
    // The visible Pi user message is the brief plus the job id, nothing else.
    assert.deepEqual(sent, ["check the news\n\n[Pi voice job id: w1]"]);
    const first = "<realtime_delegation>\n  <input>check the news</input>\n  <transcript_delta>user: what's in the news\nassistant: I'll have Pi check.\nuser: check the news</transcript_delta>\n</realtime_delegation>";
    assert.deepEqual(entries.map((entry) => entry.customType).filter((type) => type === "pi-voice-delegation").length, 1);

    const request = (messages: unknown[]) => voice.onContext({ type: "context", messages } as never, ctx);
    const text = (message: unknown) => (message as { content: { text: string }[] }).content.map((part) => part.text).join("");
    const out = request([{ role: "user", content: [{ type: "text", text: sent[0] }], timestamp: 1 }]);
    assert.equal(text(out!.messages![0]), `${first}\n\n${"[Pi voice job id: w1]"}`);

    // Next handoff: only what was said since w1, and w1 keeps its own delegation.
    (voice as unknown as { state: VoiceState }).state = { ...(voice as unknown as { state: VoiceState }).state, job: { tag: "none" } } as VoiceState;
    voice.feed({ tag: "heard", text: asUser("and the weather"), itemId: "h2" }, ctx);
    voice.feed({ tag: "work", id: asWork("w2"), brief: asUser("check the weather") }, ctx);
    const later = request([
      { role: "user", content: [{ type: "text", text: sent[0] }], timestamp: 1 },
      { role: "assistant", content: [{ type: "text", text: "Headlines…" }], timestamp: 2 },
      { role: "user", content: [{ type: "text", text: sent[1] }], timestamp: 3 },
    ]);
    assert.equal(text(later!.messages![0]), `${first}\n\n[Pi voice job id: w1]`);
    assert.equal(text(later!.messages![2]),
      "<realtime_delegation>\n  <input>check the weather</input>\n  <transcript_delta>user: and the weather\nuser: check the weather</transcript_delta>\n</realtime_delegation>\n\n[Pi voice job id: w2]");

    // After a Pi restart the session entries alone are enough.
    const restarted = Voice.attach(pi as never);
    const again = restarted.onContext({ type: "context", messages: [{ role: "user", content: sent[0], timestamp: 1 }] } as never, ctx);
    assert.equal(text(again!.messages![0]), `${first}\n\n[Pi voice job id: w1]`);
    // Without a voice job message in the request, nothing changes.
    assert.equal(voice.onContext({ type: "context", messages: [{ role: "user", content: "typed", timestamp: 1 }] } as never, ctx), undefined);
  });

  it("matches the exact dispatch even when two jobs have the same brief", () => {
    const brief = asUser("check the news");
    const branch = [
      message("u1", "user", jobPrompt(asWork("old"), brief)),
      message("a1", "assistant", "old answer"),
      message("u2", "user", jobPrompt(asWork("w1"), brief)),
      message("a2", "assistant", "new answer"),
    ];
    assert.equal(lastUserJobId(branch), "w1");
    assert.equal(answerForJob(branch, asWork("w1")), "new answer");
    const next = step(on({ job: runningJob(brief, { bound: true }) }), { tag: "agentSettled" }, world({ branch }));
    assert.equal(next.effects.find((e) => e.tag === "postResult")?.tag, "postResult");
    assert.equal(next.effects.find((e) => e.tag === "postResult" && e.speak === "new answer")?.tag, "postResult");
  });

  it("hands over the superseded answer instead of dropping it silently", () => {
    const branch = [
      message("u1", "user", jobPrompt(asWork("w1"), asUser("check"))),
      message("a1", "assistant", "old answer"),
      message("u2", "user", jobPrompt(asWork("w2"), asUser("newer"))),
    ];
    const next = step(on({ job: runningJob("check", { bound: true }) }), { tag: "agentSettled" }, world({ branch }));
    const post = next.effects.find((e) => e.tag === "postResult");
    assert.equal(post && post.tag === "postResult" ? post.speak : undefined, "old answer");
    const update = next.effects.find((e) => e.tag === "sendJobUpdate");
    assert.equal(update?.tag === "sendJobUpdate" ? update.status : undefined, "superseded");
    const recorded = next.effects.find((e) => e.tag === "recordTurn");
    assert.equal(recorded && recorded.tag === "recordTurn" ? recorded.turnKind : undefined, "work");
  });

  it("relays a replaced voice job's finished answer once, when a new task arrives as Pi answers", () => {
    // Live: Pi answered the FIXME search just as a new spoken task was handed
    // over; the steer marked it superseded and the finding was never spoken.
    const first = on({ job: runningJob("find FIXME", { bound: true }) });
    const opened = step(first, { tag: "work", id: asWork("w2"), brief: asUser("sleep then date") }, world({ idle: false }));
    assert.equal(opened.state.tag === "on" && opened.state.job.tag === "running" ? opened.state.job.prior?.id : undefined, "w1");
    const branch = [
      message("u1", "user", jobPrompt(asWork("w1"), asUser("find FIXME"))),
      message("a1", "assistant", "Found one FIXME in main.py."),
      message("u2", "user", jobPrompt(asWork("w2"), asUser("sleep then date"))),
    ];
    const bound = step(opened.state, { tag: "jobMessage", id: asWork("w2") }, world({ branch, idle: false }));
    const post = bound.effects.find((e) => e.tag === "postResult");
    assert.equal(post && post.tag === "postResult" ? `${post.id}:${post.speak}` : undefined, "w1:Found one FIXME in main.py.");
    const saved = bound.effects.find((e) => e.tag === "recordTurn");
    assert.ok(saved && saved.tag === "recordTurn" && saved.turn.text.startsWith('[Pi result "find FIXME"]'));
    const settled = step(bound.state, { tag: "agentSettled" }, world({ branch: [...branch, message("a2", "assistant", "It is Thursday.")] }));
    const posts = settled.effects.filter((e) => e.tag === "postResult").map((e) => e.tag === "postResult" ? e.id : "");
    assert.deepEqual(posts, ["w2"], "the replaced answer is not relayed twice");
  });

  it("reports a replaced job that Pi finished as done before its result, so both sides call it complete", () => {
    // Decision: a result that finished before the redirect is complete. Voice
    // already marked w1 superseded; without "done" first it framed the answer as
    // partial while the shared log stored it as a complete [Pi result].
    const opened = step(on({ job: runningJob("find FIXME", { bound: true }) }), { tag: "work", id: asWork("w2"), brief: asUser("sleep then date") }, world({ idle: false }));
    const finished = answerEntry("a1", "Found one FIXME in main.py.", "stop");
    const branch = [
      message("u1", "user", jobPrompt(asWork("w1"), asUser("find FIXME"))),
      finished,
      message("u2", "user", jobPrompt(asWork("w2"), asUser("sleep then date"))),
    ];
    const bound = step(opened.state, { tag: "jobMessage", id: asWork("w2") }, world({ branch, idle: false }));
    const order = bound.effects
      .filter((e) => (e.tag === "sendJobUpdate" || e.tag === "postResult") && e.id === "w1")
      .map((e) => e.tag === "sendJobUpdate" ? `update:${e.status}` : e.tag);
    assert.deepEqual(order, ["update:done", "postResult"]);
    const saved = bound.effects.find((e) => e.tag === "recordTurn");
    assert.ok(saved && saved.tag === "recordTurn" && saved.turn.text.startsWith('[Pi result "find FIXME"]'));
  });

  it("keeps a replaced job that Pi was cut off in mid-turn partial on both sides", () => {
    const opened = step(on({ job: runningJob("find FIXME", { bound: true }) }), { tag: "work", id: asWork("w2"), brief: asUser("sleep then date") }, world({ idle: false }));
    const midTurn = answerEntry("a1", "Let me search main.py first.", "toolUse");
    const branch = [
      message("u1", "user", jobPrompt(asWork("w1"), asUser("find FIXME"))),
      midTurn,
      message("u2", "user", jobPrompt(asWork("w2"), asUser("sleep then date"))),
    ];
    assert.equal(answerFinished(branch, asWork("w1")), false);
    const bound = step(opened.state, { tag: "jobMessage", id: asWork("w2") }, world({ branch, idle: false }));
    assert.ok(!bound.effects.some((e) => e.tag === "sendJobUpdate" && e.id === "w1" && e.status === "done"));
    const post = bound.effects.find((e) => e.tag === "postResult");
    assert.equal(post && post.tag === "postResult" ? post.id : undefined, "w1");
    const saved = bound.effects.find((e) => e.tag === "recordTurn");
    assert.ok(saved && saved.tag === "recordTurn" && saved.turn.text.startsWith("[Partial Pi findings; task superseded]"));
  });

  it("does not relay a replaced job that Pi never answered", () => {
    const opened = step(on({ job: runningJob("todo", { bound: true }) }), { tag: "work", id: asWork("w2"), brief: asUser("fixme instead") }, world({ idle: false }));
    const branch = [
      message("u1", "user", jobPrompt(asWork("w1"), asUser("todo"))),
      message("u2", "user", jobPrompt(asWork("w2"), asUser("fixme instead"))),
      message("a2", "assistant", "One FIXME."),
    ];
    const bound = step(opened.state, { tag: "jobMessage", id: asWork("w2") }, world({ branch, idle: false }));
    assert.ok(!tags(bound.effects).includes("postResult"));
    const settled = step(bound.state, { tag: "agentSettled" }, world({ branch }));
    assert.deepEqual(settled.effects.filter((e) => e.tag === "postResult").map((e) => e.tag === "postResult" ? e.id : ""), ["w2"]);
  });

  it("does not take an answer from a later unrelated turn", () => {
    const branch = [
      message("u1", "user", jobPrompt(asWork("w1"), asUser("check"))),
      message("u2", "user", "another request"),
      message("a2", "assistant", "unrelated answer"),
    ];
    assert.equal(answerForJob(branch, asWork("w1")), undefined);
    const next = step(on({ job: runningJob("check", { bound: true }) }), { tag: "agentSettled" }, world({ branch }));
    assert.ok(!tags(next.effects).includes("postResult"));
    const update = next.effects.find((e) => e.tag === "sendJobUpdate");
    assert.equal(update?.tag === "sendJobUpdate" ? update.status : undefined, "superseded");
  });
});

describe("fullResult", () => {
  it("hands over everything, including what used to be cut at ---", () => {
    const answer = "Tesla is around $420.\n---\nA 679-character dump.";
    assert.equal(fullResult(answer), answer);
  });

  it("caps at RESULT_MAX on a word boundary", () => {
    const words = Array.from({ length: 4000 }, () => "stock").join(" ");
    const result = fullResult(words);
    assert.ok(result);
    assert.ok([...result].length <= RESULT_MAX);
    assert.equal(result.endsWith("stock"), true);
  });

  it("returns undefined only when there is nothing to hand over", () => {
    assert.equal(fullResult("   \n  "), undefined);
    // An empty code fence is still Pi saying something; Luna can see it is empty.
    assert.ok(fullResult("```\n```\n"));
  });
});

describe("settleJob", () => {
  it("second settle after job none is keep", () => {
    const state = on();
    const next = settleJob(state, world());
    assert.deepEqual(next.state, state);
    assert.deepEqual(next.effects, []);
  });
});

describe("stopWork", () => {
  function stoppedWorld(brief: string): StepWorld {
    return world({
      idle: false,
      branch: [
        message("leaf0", "assistant", "prior"),
        message("u1", "user", brief === "map the project" ? jobPrompt(asWork("w1"), asUser(brief)) : brief, "leaf0"),
      ],
    });
  }

  function startRunning(brief: string): VoiceState {
    const opened = step(
      on(),
      { tag: "work", id: asWork("w1"), brief: asUser(brief) },
      world({ idle: true, now: 1000 }),
    );
    return step(opened.state, { tag: "jobMessage", id: asWork("w1") }, world({ now: 1000 })).state;
  }

  it("aborts only the voice job's own turn and says what was stopped", () => {
    const brief = "map the project";
    const stopped = step(
      startRunning(brief),
      { tag: "stopWork" },
      stoppedWorld(brief),
    );
    assert.ok(tags(stopped.effects).includes("abortWork"), "must abort the agent operation");
    if (stopped.state.tag !== "on") throw new Error("expected on");
    assert.equal(stopped.state.job.tag, "abandoned", "the job is no longer running");
    const update = stopped.effects.find((e) => e.tag === "sendJobUpdate");
    assert.equal(update && update.tag === "sendJobUpdate" ? update.status : undefined, "stopped");
    const face = stopped.effects.find((e) => e.tag === "upsertFace");
    assert.ok(face && face.tag === "upsertFace" && face.text.startsWith("stopped:"));
  });

  it("marks stopped without aborting when Pi is idle", () => {
    const brief = "map the project";
    const stopped = step(startRunning(brief), { tag: "stopWork" }, world({ idle: true }));
    assert.ok(!tags(stopped.effects).includes("abortWork"), "idle means nothing to kill");
    assert.ok(tags(stopped.effects).includes("sendJobUpdate"), "the phase still reports stopped");
    if (stopped.state.tag !== "on") throw new Error("expected on");
    assert.equal(stopped.state.job.tag, "abandoned");
  });

  it("marks stopped without aborting someone else's turn", () => {
    const stopped = step(
      startRunning("voice question"),
      { tag: "stopWork" },
      stoppedWorld("typed instead"),
    );
    assert.ok(!tags(stopped.effects).includes("abortWork"), "must not kill the user's own work");
    if (stopped.state.tag !== "on") throw new Error("expected on");
    assert.equal(stopped.state.job.tag, "abandoned");
  });

  it("does not abort a Pi turn before its job id is bound", () => {
    const state = on({ job: runningJob("queued", { bound: false }) });
    const stopped = step(state, { tag: "stopWork" }, stoppedWorld("typed instead"));
    assert.ok(!tags(stopped.effects).includes("abortWork"));
  });

  it("stopping with nothing running is a no-op, not a failure", () => {
    const next = step(on(), { tag: "stopWork" }, world());
    assert.deepEqual(next.effects, []);
  });

  it("the wire carries the stop signal", () => {
    assert.deepEqual(parseLine('{"type":"stop_work"}'), { tag: "stopWork" });
  });
});

describe("handoff transcript (Codex active transcript)", () => {
  const delegationOf = (effects: readonly Effect[]) =>
    (effects.find((effect) => effect.tag === "saveDelegation") as { text: string } | undefined)?.text;
  const live = (state: VoiceState) => state as Extract<VoiceState, { tag: "on" }>;

  it("carries only voice since the last handoff and resets on each handoff", () => {
    let state = on();
    const go = (event: Parameters<typeof step>[1]) => {
      const next = step(state, event, world({ now: 1 }));
      state = next.state;
      return next.effects;
    };
    go({ tag: "heard", text: asUser("find fli"), itemId: "h1" });
    go({ tag: "heard", text: asUser("find flights"), itemId: "h1" });
    go({ tag: "spokenDelta", text: "Which ", itemId: "s1" });
    go({ tag: "spoken", text: "Which dates?", itemId: "s1" });
    go({ tag: "composerInput", text: asUser("typed aside") });
    go({ tag: "heard", text: asUser("next friday"), itemId: "h2" });
    const first = delegationOf(go({ tag: "work", id: asWork("w1"), brief: asUser("find flights for next friday") }));
    assert.equal(first,
      "<realtime_delegation>\n  <input>find flights for next friday</input>\n  <transcript_delta>user: find flights\nassistant: Which dates?\nuser: next friday\nuser: find flights for next friday</transcript_delta>\n</realtime_delegation>");
    // Typed text is not a transcript in Codex either.
    assert.doesNotMatch(first!, /typed aside/);
    assert.deepEqual(live(state).transcript, []);
    // Job progress and status items reach the voice model, not the transcript.
    go({ tag: "jobProgress", note: "[STATUS] searching flights" });
    go({ tag: "spoken", text: "Still looking.", itemId: "s2" });
    const second = delegationOf(go({ tag: "work", id: asWork("w2"), brief: asUser("also hotels") }));
    assert.equal(second,
      "<realtime_delegation>\n  <input>also hotels</input>\n  <transcript_delta>assistant: Still looking.\nuser: also hotels</transcript_delta>\n</realtime_delegation>");
    assert.doesNotMatch(second!, /find flights|STATUS/);
  });

  it("keeps the delta when a muted dispatch is dropped", () => {
    const state = on({ mic: { tag: "closed" }, transcript: [{ role: "user", text: "hello" }] });
    const next = step(state, { tag: "work", id: asWork("w1"), brief: asUser("x") }, world());
    assert.equal(delegationOf(next.effects), undefined);
    assert.deepEqual(live(next.state).transcript, [{ role: "user", text: "hello" }]);
  });

  it("starts empty when voice comes up", () => {
    const next = step(connecting(), { tag: "ready" }, world({ pid: pid(1) }));
    assert.equal(live(next.state).transcript, undefined);
  });
});
