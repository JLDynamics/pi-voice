import type {
  BeforeAgentStartEvent,
  ExtensionAPI,
  ExtensionContext,
  InputEvent,
  InputEventResult,
  SessionEntry,
} from "@earendil-works/pi-coding-agent";
import type { AutocompleteItem, Component } from "@earendil-works/pi-tui";
import { clearOwnedLease, liveForeignOwner, reapOrphans, VoiceChild, type VoiceHistoryTurn } from "./child.ts";
import { bindJob, jobIdFromText, jobPrompt, lastUserJobId, openJob, settleJob, terminalJob } from "./work.ts";
import {
  localWhen,
  type TurnKind,
} from "./history.ts";
import { Conversation } from "./conversation.ts";

export type ChildPid = number & { readonly brand: "ChildPid" };
export type WorkId = string & { readonly brand: "WorkId" };
export type UserText = string & { readonly brand: "UserText" };

/** Immediate Pi handoff to Luna; the full answer stays available through pi_results. */
export type ShortResult = string & { readonly brand: "ShortResult" };

/** Mic closed means no capture. Speakers stay live. */
export type Mic = { tag: "open" } | { tag: "closed" };

/**
 * Pi work in THIS session. Independent of the voice floor.
 * Luna keeps talking while tag === "running".
 */
export type Job =
  | { tag: "none" }
  | {
      tag: "running";
      id: WorkId;
      brief: UserText;
      startedAt: number;
      bound: boolean;
      afterEntryId: string | null;
      /** Latest Pi tool activity, mirrored to Voice.app for `pi_status`. */
      lastNote: string | null;
    }
  | { tag: "abandoned"; id: WorkId };

export type VoiceState =
  | { tag: "off" }
  | { tag: "connecting"; mic: Mic; startedAt: number }
  | {
      tag: "on";
      pid: ChildPid;
      mic: Mic;
      job: Job;
      /** Last Luna utterance, painted only. Writer is the child via `spoken`. */
      lastSpoken: string | null;
      streamingSpoken: string;
      spokenItemId: string | null;
      interruptedSpokenId?: string | null;
      lastHeard: UserText | null;
      /** Latest transcript revision; its visible entry keeps the original position. */
      heardPending: UserText | null;
      /** Server item id of the held utterance; its revisions all share it. */
      heardItemId: string | null;
      typedEcho?: { text: string; until: number };
    };

export type VoiceCommand =
  | { tag: "toggle" }
  | { tag: "stop" }
  | { tag: "setMic"; closed: boolean }
  | { tag: "toggleMic" }
  | { tag: "newConversation" }
  | { tag: "resumeConversation"; selector: string | null };

export type VoiceEvent =
  | { tag: "ready" }
  | { tag: "error"; message: string }
  | { tag: "requestError"; message: string }
  | { tag: "childExit"; code: number | null }
  | { tag: "heard"; text: UserText; itemId?: string }
  | { tag: "spoken"; text: string; itemId?: string }
  | { tag: "spokenDelta"; text: string; itemId?: string }
  | { tag: "work"; id: WorkId; brief: UserText }
  | { tag: "stopWork" }
  | { tag: "speechStarted" }
  | { tag: "jobMessage"; id: WorkId }
  | { tag: "agentSettled" }
  | { tag: "jobProgress"; note: string }
  | { tag: "composerInput"; text: UserText }
  | { tag: "shutdown" };

export type Strip = { line1: string; line2: string };

export type Effect =
  | { tag: "reapOrphans" }
  | { tag: "spawn" }
  | { tag: "quitChild" }
  | { tag: "setMic"; closed: boolean }
  | { tag: "interruptSpeech" }
  | { tag: "injectUser"; text: UserText }
  | { tag: "sendWork"; id: WorkId; brief: UserText; deliver: "plain" | "steer" }
  | { tag: "postResult"; id: WorkId; speak: ShortResult; full: string }
  | {
      tag: "sendJobUpdate";
      id: WorkId;
      status: "queued" | "working" | "done" | "stopped" | "superseded" | "dropped" | "failed";
      note?: string;
    }
  | { tag: "abortWork" }
  | { tag: "recordTurn"; turn: VoiceHistoryTurn; turnKind: TurnKind }
  | { tag: "paint"; strip: Strip | null }
  | { tag: "notify"; message: string; kind: "info" | "warning" | "error" }
  | { tag: "upsertFace"; id: string; kind: "heard" | "spoken" | "work"; text: string; final: boolean };

export type Step = { state: VoiceState; effects: readonly Effect[] };

export type StepWorld = {
  idle: boolean;
  branch: SessionEntry[];
  now: number;
  pid?: ChildPid;
  leafId?: string | null;
};

/**
 * A guard against a runaway answer filling the voice conversation window, not
 * a shape for speech. Luna decides what is worth saying; see `fullResult`.
 */
export const RESULT_MAX = 1500;

/**
 * Injected into Pi's system prompt while voice work is running.
 *
 * This used to demand a two-sentence spoken head, a `---` line, then detail,
 * and only the head was ever read out. That made Pi write in a shape nobody
 * wanted — and when it did not follow the format, the fallback guessed badly
 * and the answer went missing. Pi now answers the way it always does; Luna
 * reads the whole thing and speaks about it in her own words.
 */
export const WORK_SECTION = [
  "Luna is on the voice line in this same session.",
  "She reads your full answer and tells the user what matters, in her own words, out loud.",
  "So write for the terminal as usual — do not write for speech, and do not split your answer for her.",
  "Lead with the outcome in a sentence or two, then the detail; that is what she relays first.",
  "The user's messages reach you as speech transcripts: they may be unpunctuated or misrecognised.",
  "The Pi voice job id at the end of a delegated request is only for routing; do not mention it in your answer.",
  "Keep it concise and action-oriented.",
].join(" ");

const WIDGET_KEY = "pi-voice";
const FACE_TYPE = "pi-voice-face";

/** Final voice turns are already in Pi's session branch when /voice restarts. */
export function voiceHistory(branch: SessionEntry[]): VoiceHistoryTurn[] {
  const turns: VoiceHistoryTurn[] = [];
  for (const entry of branch) {
    if (entry.type !== "custom" || entry.customType !== FACE_TYPE) continue;
    const data = entry.data as { kind?: string; text?: string } | undefined;
    if (!data || typeof data.text !== "string") continue;
    const role = data.kind === "heard" || data.kind === "heard-final" ? "user"
      : data.kind === "spoken" || data.kind === "spoken-final" ? "assistant" : null;
    const text = data.text.trim();
    if (role && text) turns.push({ role, text: text.slice(0, 2000) });
  }
  return turns.slice(-20);
}

export function detectMuteChord(): "alt+m" | "ctrl+shift+m" {
  const term = `${process.env.TERM ?? ""}\n${process.env.TERM_PROGRAM ?? ""}`.toLowerCase();
  if (term.includes("kitty") || Boolean(process.env.KITTY_WINDOW_ID)) return "ctrl+shift+m";
  return "alt+m";
}

export function muteHint(): string {
  return `/voice mute   ${detectMuteChord()}   /voice stop`;
}

/** Name shown next to the voice assistant's lines in the transcript (the persona is still Luna). */
export const AGENT_LABEL = "agent";

/** Pi crashes if any custom render line is wider than the terminal. */
export function faceLines(who: string, text: string, width: number): string[] {
  const w = Math.max(1, Math.floor(width));
  const body = String(text).replace(/\s+/g, " ").trim();
  const full = body ? `${who}  ${body}` : who;
  return wrapToWidth(full, w);
}

/** Show saved voice entries in Pi's ordinary transcript, including during a call. */
export function sessionFaceLines(who: string, text: string, width: number, voiceActive: boolean): string[] {
  void voiceActive;
  return faceLines(who, text, width);
}

/** Keep the live reply in the transcript, with paragraphs and a stable label. */
export function spokenFaceLines(text: string, width: number): string[] {
  return text.split(/\r?\n/).flatMap((paragraph, index) =>
    wrapToWidth(`${index === 0 ? `${AGENT_LABEL}  ` : " ".repeat(AGENT_LABEL.length + 2)}${paragraph}`, Math.max(1, Math.floor(width))));
}

function wrapToWidth(text: string, width: number): string[] {
  const words = text.split(/(\s+)/);
  const lines: string[] = [];
  let current = "";
  for (const word of words) {
    if (!word) continue;
    if (current.length + word.length <= width) {
      current += word;
      continue;
    }
    if (current.trim()) lines.push(current.trimEnd());
    let rest = word.trimStart();
    while (rest.length > width) {
      lines.push(rest.slice(0, width));
      rest = rest.slice(width);
    }
    current = rest;
  }
  if (current) lines.push(current);
  return lines.length > 0 ? lines : [""];
}

export function asUser(text: string): UserText {
  return text as UserText;
}

export function asWork(id: string): WorkId {
  return id as WorkId;
}

function keep(state: VoiceState, effects: Effect[] = []): Step {
  return { state, effects };
}

function stopLive(state: VoiceState): Step {
  if (state.tag === "off") return keep(state);
  return {
    state: { tag: "off" },
    effects: [{ tag: "quitChild" }, { tag: "paint", strip: null }],
  };
}

function fail(state: VoiceState, message: string): Step {
  const effects: Effect[] = [];
  if (state.tag !== "off") effects.push({ tag: "quitChild" });
  effects.push({ tag: "paint", strip: null }, { tag: "notify", message, kind: "error" });
  return { state: { tag: "off" }, effects };
}

function withMic(state: VoiceState, mic: Mic): VoiceState {
  if (state.tag === "off") return state;
  return { ...state, mic };
}

export function strip(state: VoiceState, now: number = Date.now()): Strip | null {
  if (state.tag === "off") return null;
  if (state.tag === "connecting") {
    return { line1: "voice  …  connecting", line2: muteHint() };
  }
  if (state.mic.tag === "closed") {
    return {
      line1: "voice  ◌  muted",
      line2: `/voice unmute   ${detectMuteChord()}   /voice stop`,
    };
  }
  // Speech recognition still waits for an audio segment before it supplies text.
  const hearing = state.heardPending ? "  ·  hearing you" : "";
  if (state.job.tag === "running") {
    return { line1: `voice  ●  talking · pi working · ${elapsedShort(now - state.job.startedAt)}${hearing}`, line2: muteHint() };
  }
  return { line1: `voice  ●  talking${hearing}`, line2: muteHint() };
}

function elapsedShort(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m${s % 60}s`;
}

export function parseSlash(args: string): VoiceCommand | { tag: "unknown"; raw: string } {
  const raw = args.trim();
  const [token, ...rest] = raw.split(/\s+/);
  const head = token?.toLowerCase() ?? "";
  if (head === "" || head === "start") return { tag: "toggle" };
  if (head === "stop") return { tag: "stop" };
  if (head === "mute") return { tag: "toggleMic" };
  if (head === "unmute") return { tag: "setMic", closed: false };
  if (head === "new") return { tag: "newConversation" };
  if (head === "resume") {
    const selector = rest.join(" ").trim();
    return { tag: "resumeConversation", selector: selector || null };
  }
  return { tag: "unknown", raw };
}

export function handleCommand(state: VoiceState, command: VoiceCommand): Step {
  // Conversation switching is owned by Voice.slash, which never reaches the
  // reducer; this keeps the reducer total over the widened command type.
  if (command.tag === "newConversation" || command.tag === "resumeConversation") return keep(state);
  if (command.tag === "stop") {
    if (state.tag === "off") return keep(state);
    return stopLive(state);
  }
  if (command.tag === "toggle") {
    if (state.tag !== "off") return stopLive(state);
    const connecting: VoiceState = {
      tag: "connecting",
      mic: { tag: "open" },
      startedAt: Date.now(),
    };
    return {
      state: connecting,
      effects: [
        { tag: "reapOrphans" },
        { tag: "spawn" },
        { tag: "paint", strip: strip(connecting) },
      ],
    };
  }
  if (state.tag === "off") {
    return keep(state, [
      { tag: "notify", message: "Voice is off. Type /voice to start.", kind: "warning" },
    ]);
  }
  const closed = command.tag === "toggleMic" ? state.mic.tag === "open" : command.closed;
  const mic: Mic = closed ? { tag: "closed" } : { tag: "open" };
  if (state.mic.tag === mic.tag) return keep(state);
  const next = withMic(state, mic);
  return { state: next, effects: [{ tag: "setMic", closed }, { tag: "paint", strip: strip(next) }] };
}

export function step(state: VoiceState, event: VoiceEvent, world: StepWorld): Step {
  if (event.tag === "requestError") return keep(state, [{ tag: "notify", kind: "error",
    message: `${event.message} Your message is saved. You can ask Luna to retry.` }]);
  if (event.tag === "shutdown") return handleCommand(state, { tag: "stop" });
  if (event.tag === "speechStarted") {
    if (state.tag !== "on" || !state.streamingSpoken || !state.spokenItemId) return keep(state);
    const next: VoiceState = { ...state, interruptedSpokenId: state.spokenItemId,
      streamingSpoken: "", spokenItemId: null };
    const cut = state.streamingSpoken.trim();
    return keep(next, [
      { tag: "upsertFace", id: state.spokenItemId, kind: "spoken", text: cut, final: true },
      ...(cut ? [{ tag: "recordTurn" as const,
        turn: { role: "assistant" as const, text: cut },
        turnKind: (state.job.tag === "running" ? "progress" : "voice") as TurnKind }] : []),
    ]);
  }

  if (event.tag === "error" || event.tag === "childExit") {
    if (state.tag === "off") return keep(state);
    const message =
      event.tag === "error"
        ? event.message || "Voice failed"
        : `Voice exited${event.code == null ? "" : ` (${event.code})`}`;
    return fail(state, message);
  }

  if (event.tag === "ready") {
    if (state.tag !== "connecting" || world.pid == null) return keep(state);
    const live: VoiceState = {
      tag: "on",
      pid: world.pid,
      mic: state.mic,
      job: { tag: "none" },
      lastSpoken: null,
      streamingSpoken: "",
      spokenItemId: null,
      lastHeard: null,
      heardPending: null,
      heardItemId: null,
    };
    return {
      state: live,
      effects: [{ tag: "paint", strip: strip(live) }],
    };
  }

  if (state.tag !== "on") return keep(state);

  if (event.tag === "spokenDelta") {
    if (event.itemId && event.itemId === state.interruptedSpokenId) return keep(state);
    const id = event.itemId || state.spokenItemId || `luna:${world.now}`;
    const newTurn = state.spokenItemId !== null && id !== state.spokenItemId;
    const text = (newTurn ? "" : state.streamingSpoken) + event.text;
    const next: VoiceState = { ...state, streamingSpoken: text, spokenItemId: id,
      interruptedSpokenId: id === state.interruptedSpokenId ? state.interruptedSpokenId : null };
    const effects: Effect[] = [];
    if (newTurn && state.streamingSpoken) {
      effects.push({ tag: "upsertFace", id: state.spokenItemId!, kind: "spoken", text: state.streamingSpoken.trim(), final: true });
    }
    effects.push({ tag: "upsertFace", id, kind: "spoken", text, final: false });
    return keep(next, effects);
  }

  if (event.tag === "composerInput") {
    const text = asUser(String(event.text).trim());
    if (!text) return keep(state);
    const next: VoiceState = { ...state, lastHeard: text, typedEcho: { text, until: world.now + 5000 } };
    return {
      state: next,
      effects: [
        { tag: "injectUser", text },
        { tag: "upsertFace", id: `typed:${world.now}`, kind: "heard", text, final: true },
        { tag: "recordTurn", turn: { role: "user", text }, turnKind: "voice" },
        { tag: "paint", strip: strip(next, world.now) },
      ],
    };
  }

  if (event.tag === "heard") {
    if (state.mic.tag === "closed") return keep(state);
    const text = asUser(String(event.text).trim());
    if (!text) return keep(state);
    if (state.typedEcho?.text === text && world.now <= state.typedEcho.until) {
      return keep({ ...state, typedEcho: undefined });
    }
    const effects: Effect[] = [];
    // One utterance is transcribed several times as it grows, and each revision
    // returns the whole thing — sometimes with earlier words rewritten, so the
    // text alone cannot tell a revision from a new sentence. The server item id
    // can: it is stable across the revisions of one utterance. Same id replaces
    // what is held; a different id means you started a new sentence, so the
    // previous one is committed rather than lost.
    const itemId = event.itemId || state.heardItemId || `heard:${world.now}`;
    const sameUtterance = state.heardPending !== null && itemId === state.heardItemId;
    if (state.heardPending && !sameUtterance) {
      effects.push({ tag: "upsertFace", id: state.heardItemId!, kind: "heard", text: state.heardPending, final: true });
      effects.push({ tag: "recordTurn", turn: { role: "user", text: state.heardPending }, turnKind: "voice" });
    }
    const next: VoiceState = {
      ...state,
      lastHeard: text,
      heardPending: text,
      heardItemId: itemId,
      typedEcho: undefined,
    };
    effects.push({ tag: "upsertFace", id: itemId, kind: "heard", text, final: false });
    effects.push({ tag: "paint", strip: strip(next, world.now) });
    return { state: next, effects };
  }

  if (event.tag === "spoken") {
    const text = event.text.trim();
    if (!text) return keep(state);
    if (event.itemId && event.itemId === state.interruptedSpokenId) return keep(state);
    if (state.spokenItemId && event.itemId && event.itemId !== state.spokenItemId) return keep(state);
    const effects: Effect[] = [];
    if (state.heardPending) {
      effects.push({ tag: "upsertFace", id: state.heardItemId!, kind: "heard", text: state.heardPending, final: true });
      effects.push({ tag: "recordTurn", turn: { role: "user", text: state.heardPending }, turnKind: "voice" });
    }
    const id = event.itemId || state.spokenItemId || `luna:${world.now}`;
    const next: VoiceState = { ...state, lastSpoken: text, streamingSpoken: "", spokenItemId: null, heardPending: null, heardItemId: null };
    effects.push({ tag: "upsertFace", id, kind: "spoken", text, final: true }, { tag: "paint", strip: strip(next, world.now) });
    effects.push({
      tag: "recordTurn",
      turn: { role: "assistant", text },
      turnKind: state.job.tag === "running" ? "progress" : "voice",
    });
    return { state: next, effects };
  }

  if (event.tag === "work") {
    // A dispatch Luna made while muted never reaches Pi; tell Voice.app the
    // id is dead so `pi_status` does not report a phantom job.
    if (state.mic.tag === "closed") {
      return keep(state, [{ tag: "sendJobUpdate", id: event.id, status: "dropped" }]);
    }
    // Pi's user message is written as soon as sendWork runs. Save the spoken
    // request first, or the search appears in the transcript before the words
    // that prompted it. The later `spoken` event must not save it again.
    const pending = state.heardPending;
    const ready = pending ? { ...state, heardPending: null, heardItemId: null } : state;
    const job = openJob(ready, event.id, event.brief, world);
    const record: Effect[] = pending
      ? [{ tag: "recordTurn", turn: { role: "user", text: pending }, turnKind: "voice" as TurnKind }]
      : [];
    return pending
      ? { state: job.state, effects: [{ tag: "upsertFace", id: state.heardItemId!, kind: "heard", text: pending, final: true }, ...record, ...job.effects] }
      : job;
  }

  if (event.tag === "stopWork") {
    // Nothing running is not an error: the user asked for it to stop, and it
    // is stopped. Luna says so rather than reporting a failure.
    if (state.job.tag !== "running") return keep(state);
    // Abort only the voice job's own turn. ctx.abort() kills whatever the
    // agent is streaming — when the brief was never bound, Pi is idle, or the
    // user has since typed their own work, there is nothing of ours to kill
    // and aborting would take down someone else's turn.
    const ownsTurn =
      !world.idle && state.job.bound && lastUserJobId(world.branch) === state.job.id;
    const next: VoiceState = { ...state, job: { tag: "abandoned", id: state.job.id } };
    const effects: Effect[] = [
      ...terminalJob(state.job, world, "stopped"),
      { tag: "upsertFace", id: `work:${state.job.id}`, kind: "work", text: `stopped: ${state.job.brief}`, final: true },
      { tag: "paint", strip: strip(next, world.now) },
    ];
    if (ownsTurn) effects.unshift({ tag: "abortWork" });
    return { state: next, effects };
  }

  if (event.tag === "jobProgress") {
    if (state.job.tag !== "running" || !state.job.bound || lastUserJobId(world.branch) !== state.job.id) return keep(state);
    const note = event.note.trim();
    if (!note || state.job.lastNote === note) return keep(state);
    const next: VoiceState = { ...state, job: { ...state.job, lastNote: note } };
    return {
      state: next,
      effects: [
        {
          tag: "sendJobUpdate",
          id: state.job.id,
          status: state.job.bound ? "working" : "queued",
          note,
        },
        { tag: "paint", strip: strip(next, world.now) },
      ],
    };
  }

  if (event.tag === "jobMessage") return bindJob(state, event.id);
  if (event.tag === "agentSettled") return settleJob(state, world);

  return keep(state);
}

export class Voice {
  static attach(pi: ExtensionAPI): Voice {
    const voice = new Voice(pi);
    if (typeof pi.registerEntryRenderer === "function") {
      pi.registerEntryRenderer<{ kind: string; text?: string; id?: string }>(FACE_TYPE, (entry) => {
        const data = entry.data;
        if (data?.kind === "spoken-final" && data.id && data.text != null) {
          voice.spokenTexts.set(data.id, data.text);
          return;
        }
        if (data?.kind === "heard-final" && data.id && data.text != null) {
          voice.heardTexts.set(data.id, data.text);
          return;
        }
        if (data?.kind === "heard-live" && data.id) {
          if (!voice.heardTexts.has(data.id)) voice.heardTexts.set(data.id, data.text ?? "");
          const id = data.id;
          return { render: (width: number) => faceLines("you", voice.heardTexts.get(id) ?? "", width), invalidate() {} };
        }
        if (data?.kind === "spoken-live" && data.id) {
          if (!voice.spokenTexts.has(data.id)) voice.spokenTexts.set(data.id, data.text ?? "");
          const id = data.id;
          return { render: (width: number) => spokenFaceLines(voice.spokenTexts.get(id) ?? "", width), invalidate() {} };
        }
        if (!data?.text) return;
        const who = data.kind === "heard" ? "you" : data.kind === "spoken" ? AGENT_LABEL : "pi";
        const text = data.text;
        const face: Component = {
          render: (width: number) => sessionFaceLines(who, text, width, voice.state.tag !== "off"),
          invalidate() {},
        };
        return face;
      });
    }
    return voice;
  }

  private readonly pi: ExtensionAPI;
  private state: VoiceState = { tag: "off" };
  private child: VoiceChild | undefined;
  private epoch = 0;
  private lastCtx: ExtensionContext | undefined;
  private pendingMute: boolean | undefined;
  private reapJob: Promise<string | undefined> | undefined;
  private thinkingRestore: string | undefined;
  private conversation: Conversation | null = null;
  private readonly finalFaceIds = new Set<string>();
  private readonly startedSpokenIds = new Set<string>();
  private readonly spokenTexts = new Map<string, string>();
  private readonly startedHeardIds = new Set<string>();
  private readonly heardTexts = new Map<string, string>();
  private requestTranscriptRender: (() => void) | undefined;

  private constructor(pi: ExtensionAPI) {
    this.pi = pi;
  }

  slash(args: string, ctx: ExtensionContext): void {
    this.lastCtx = ctx;
    const parsed = parseSlash(args);
    if (parsed.tag === "unknown") {
      ctx.ui.notify(`Unknown /voice argument: ${parsed.raw}`, "warning");
      return;
    }
    if (parsed.tag === "newConversation") {
      try {
        this.history().fresh();
      } catch (error) {
        ctx.ui.notify(`Voice history unavailable, keeping current conversation: ${String(error)}`, "warning");
        return;
      }
      this.switchConversation(ctx, "New voice conversation started.");
      return;
    }
    if (parsed.tag === "resumeConversation") {
      try {
        this.resumeConversation(parsed.selector, ctx);
      } catch (error) {
        ctx.ui.notify(`Voice history unavailable: ${String(error)}`, "warning");
      }
      return;
    }
    if (parsed.tag === "toggle" && this.state.tag === "off") {
      const blocked = liveForeignOwner();
      if (blocked) {
        ctx.ui.notify(blocked, "warning");
        return;
      }
    }
    const starting = parsed.tag === "toggle" && this.state.tag === "off";
    if (starting) {
      this.finalFaceIds.clear();
    }
    this.commit(handleCommand(this.state, parsed), ctx);
  }

  completions(prefix: string): AutocompleteItem[] {
    const p = prefix.trim().toLowerCase();
    if (!p) return [];
    return [
      { value: "mute", label: "mute", description: "Mute Luna's mic" },
      { value: "unmute", label: "unmute", description: "Unmute Luna's mic" },
      { value: "stop", label: "stop", description: "Stop voice in this session" },
      { value: "new", label: "new", description: "Start a fresh voice conversation" },
      { value: "resume", label: "resume", description: "List or resume a past voice conversation" },
    ].filter((item) => item.value.startsWith(p));
  }

  onInput(event: InputEvent, ctx: ExtensionContext): InputEventResult {
    this.lastCtx = ctx;
    if (event.source === "extension") return { action: "continue" };
    if (event.source === "interactive" && this.state.tag === "on") {
      this.feed({ tag: "composerInput", text: asUser(event.text) }, ctx);
      return { action: "handled" };
    }
    return { action: "continue" };
  }

  onBeforeAgentStart(event: BeforeAgentStartEvent, _ctx: ExtensionContext): void {
    if (this.state.tag === "on" && this.state.job.tag === "running" &&
        jobIdFromText(event.prompt) === this.state.job.id) {
      event.systemPromptOptions.sections.voice = WORK_SECTION;
      this.capJobThinking();
    } else {
      delete event.systemPromptOptions.sections.voice;
    }
  }

  feed(event: VoiceEvent, ctx?: ExtensionContext): void {
    if (ctx) this.lastCtx = ctx;
    this.commit(
      step(this.state, event, {
        idle: ctx?.isIdle?.() ?? this.lastCtx?.isIdle?.() ?? true,
        branch: ctx?.sessionManager?.getBranch?.() ?? this.lastCtx?.sessionManager?.getBranch?.() ?? [],
        now: Date.now(),
        pid: this.child?.pid,
        leafId: ctx?.sessionManager?.getLeafId?.() ?? this.lastCtx?.sessionManager?.getLeafId?.() ?? null,
      }),
      ctx ?? this.lastCtx,
    );
    if (event.tag === "shutdown") this.conversation?.close();
  }

  private capJobThinking(): void {
    if (typeof this.pi.getThinkingLevel !== "function" || typeof this.pi.setThinkingLevel !== "function") return;
    const level = this.pi.getThinkingLevel();
    if (level === "off" || level === "low" || level === "minimal") return;
    if (this.thinkingRestore == null) this.thinkingRestore = level;
    this.pi.setThinkingLevel("off");
  }

  private restoreThinking(): void {
    if (this.thinkingRestore == null || typeof this.pi.setThinkingLevel !== "function") return;
    const saved = this.thinkingRestore;
    this.thinkingRestore = undefined;
    this.pi.setThinkingLevel(saved as "off");
  }

  private commit(next: Step, ctx?: ExtensionContext): void {
    const wasRunning = this.state.tag === "on" && this.state.job.tag === "running";
    this.state = next.state;
    (globalThis as typeof globalThis & { __piVoiceActive?: boolean }).__piVoiceActive = this.state.tag !== "off";
    const nowRunning = this.state.tag === "on" && this.state.job.tag === "running";
    if (wasRunning && !nowRunning) this.restoreThinking();
    for (const effect of next.effects) this.apply(effect, ctx);
  }

  private apply(effect: Effect, ctx?: ExtensionContext): void {
    switch (effect.tag) {
      case "reapOrphans":
        this.reapJob = reapOrphans();
        return;
      case "spawn": {
        const epoch = this.epoch;
        void this.startChild(epoch, ctx);
        return;
      }
      case "quitChild":
        this.requestTranscriptRender = undefined;
        this.epoch += 1;
        this.child?.quit();
        this.child = undefined;
        this.pendingMute = undefined;
        this.thinkingRestore = undefined;
        clearOwnedLease();
        return;
      case "setMic":
        this.pendingMute = effect.closed;
        this.child?.setMuted(effect.closed);
        return;
      case "interruptSpeech":
        this.child?.interrupt();
        return;
      case "injectUser":
        this.child?.ingestUser(effect.text);
        return;
      case "sendWork":
        if (effect.deliver === "steer") this.pi.sendUserMessage(jobPrompt(effect.id, effect.brief), { deliverAs: "steer" });
        else this.pi.sendUserMessage(jobPrompt(effect.id, effect.brief));
        return;
      case "postResult":
        this.child?.postResult(effect.id, effect.speak, effect.full);
        return;
      case "sendJobUpdate":
        this.child?.sendJobUpdate(effect.id, effect.status, effect.note);
        return;
      case "abortWork":
        // ExtensionContext.abort() stops the agent operation in flight. The
        // voice line is unaffected: Luna keeps listening and talking through it.
        ctx?.abort();
        return;
      case "recordTurn":
        this.conversation?.record(effect.turn, effect.turnKind);
        return;
      case "paint":
        if (effect.strip) {
          const lines = [effect.strip.line1, effect.strip.line2];
          ctx?.ui.setWidget(WIDGET_KEY, (tui) => {
            this.requestTranscriptRender = () => tui.requestRender();
            return { render: () => lines, invalidate() {} };
          });
        } else ctx?.ui.setWidget(WIDGET_KEY, undefined);
        return;
      case "notify":
        ctx?.ui.notify(effect.message, effect.kind);
        return;
      case "upsertFace":
        if (effect.kind === "spoken") {
          if (this.finalFaceIds.has(effect.id)) return;
          this.spokenTexts.set(effect.id, effect.text);
          if (!this.startedSpokenIds.has(effect.id) && !effect.final) {
            this.startedSpokenIds.add(effect.id);
            this.pi.appendEntry?.(FACE_TYPE, { kind: "spoken-live", id: effect.id, text: effect.text });
          } else if (effect.final) {
            this.finalFaceIds.add(effect.id);
            this.pi.appendEntry?.(FACE_TYPE, this.startedSpokenIds.has(effect.id)
              ? { kind: "spoken-final", id: effect.id, text: effect.text }
              : { kind: "spoken", text: effect.text });
          }
          this.requestTranscriptRender?.();
          return;
        }
        if (effect.kind === "heard") {
          if (this.finalFaceIds.has(effect.id)) return;
          this.heardTexts.set(effect.id, effect.text);
          if (effect.final && !this.startedHeardIds.has(effect.id)) {
            this.finalFaceIds.add(effect.id);
            this.pi.appendEntry?.(FACE_TYPE, { kind: "heard", text: effect.text });
            return;
          }
          if (!this.startedHeardIds.has(effect.id)) {
            this.startedHeardIds.add(effect.id);
            this.pi.appendEntry?.(FACE_TYPE, { kind: "heard-live", id: effect.id, text: effect.text });
          }
          if (effect.final) {
            this.finalFaceIds.add(effect.id);
            this.pi.appendEntry?.(FACE_TYPE, { kind: "heard-final", id: effect.id, text: effect.text });
          }
          this.requestTranscriptRender?.();
          return;
        }
        if (effect.final && !this.finalFaceIds.has(effect.id)) {
          this.finalFaceIds.add(effect.id);
          this.pi.appendEntry?.(FACE_TYPE, { kind: effect.kind, text: effect.text });
        }
        return;
    }
  }

  private async startChild(epoch: number, ctx?: ExtensionContext): Promise<void> {
    const blocked = await (this.reapJob ?? reapOrphans());
    this.reapJob = undefined;
    if (this.epoch !== epoch) return;
    if (blocked) {
      this.feed({ tag: "error", message: blocked }, ctx);
      return;
    }
    try {
      const branch = (ctx ?? this.lastCtx)?.sessionManager?.getBranch?.() ?? [];
      const history = this.history().replay(voiceHistory(branch));
      this.child = VoiceChild.spawn((event) => {
        if (this.epoch !== epoch) return;
        this.feed(event, this.lastCtx ?? ctx);
      }, history);
      if (this.pendingMute != null) this.child.setMuted(this.pendingMute);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      this.feed({ tag: "error", message }, ctx);
    }
  }

  /**
   * The conversation cabinet for this folder, opened on first use. History
   * policy (selection, migration, replay, fallback) lives in Conversation;
   * this only owns the instance and surfaces its warnings once each.
   */
  private history(): Conversation {
    return this.conversation ??= new Conversation(process.cwd(), message => {
      this.lastCtx?.ui.notify(message, "warning");
    });
  }

  private switchConversation(ctx: ExtensionContext, note: string): void {
    const live = this.state.tag !== "off";
    if (live) this.commit(handleCommand(this.state, { tag: "stop" }), ctx);
    ctx.ui.notify(live ? `${note} Voice stopped — type /voice to begin it.` : note, "info");
  }

  private resumeConversation(selector: string | null, ctx: ExtensionContext): void {
    if (selector == null) {
      const listed = this.history().list();
      if (listed.length === 0) {
        ctx.ui.notify("No earlier voice conversations yet. /voice new starts a fresh one.", "info");
        return;
      }
      const lines = listed.map((item, index) =>
        `${index + 1}. ${item.first || "(no user turns)"} — ${item.turns} turns, ${item.last ? localWhen(item.last) : "unknown time"}`);
      ctx.ui.notify(`Past voice conversations (Pi session unchanged):\n${lines.join("\n")}\nType /voice resume <number> to go back to one.`, "info");
      return;
    }
    const error = this.history().resume(selector);
    if (error) {
      ctx.ui.notify(error, "warning");
      return;
    }
    this.switchConversation(ctx, "Voice conversation switched.");
  }
}

export { fullResult, lastAssistantText, lastUserText } from "./work.ts";
export { headlessChildEnv, parseLine, stderrLogPath } from "./child.ts";
