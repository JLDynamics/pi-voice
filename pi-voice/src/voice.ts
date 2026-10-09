import type {
  BeforeAgentStartEvent,
  ContextEvent,
  ContextEventResult,
  ExtensionAPI,
  ExtensionContext,
  InputEvent,
  InputEventResult,
  SessionEntry,
} from "@earendil-works/pi-coding-agent";
import type { AutocompleteItem, Component } from "@earendil-works/pi-tui";
import { clearOwnedLease, liveForeignOwner, reapOrphans, VoiceChild, type VoiceHistoryTurn } from "./child.ts";
import {
  jobIdFromText,
  jobPrompt,
  DELEGATION_TYPE,
  delegationsFromBranch,
  withDelegations,
} from "./work.ts";
import {
  localWhen,
} from "./history.ts";
import { Conversation } from "./conversation.ts";
import {
  asUser,
  keep,
  stopLive,
  withMic,
  AGENT_LABEL,
  USER_LABEL,
  type Effect,
  type Mic,
  type Step,
  type StepWorld,
  type VoiceCommand,
  type VoiceEvent,
  type VoiceState,
  type WorkId,
} from "./voice-core.ts";
import {
  faceLines,
  sessionFaceLines,
  spokenFaceLines,
  strip,
} from "./voice-face.ts";
import { stepLifecycle } from "./voice-lifecycle.ts";
import { stepTranscript } from "./voice-transcript.ts";
import { stepJob } from "./voice-job.ts";

export type {
  ChildPid,
  Effect,
  Job,
  Mic,
  ShortResult,
  Step,
  StepWorld,
  Strip,
  UserText,
  VoiceCommand,
  VoiceEvent,
  VoiceState,
  WorkId,
} from "./voice-core.ts";
export {
  AGENT_LABEL,
  asUser,
  asWork,
  RESULT_MAX,
  USER_LABEL,
} from "./voice-core.ts";
export {
  detectMuteChord,
  faceLines,
  muteHint,
  sessionFaceLines,
  spokenFaceLines,
  strip,
} from "./voice-face.ts";

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
  "Agent, the voice assistant, is on the voice line in this same session.",
  "It reads your full answer and tells the user what matters, in its own words, out loud.",
  "So write for the terminal as usual — do not write for speech, and do not split your answer for it.",
  "Lead with the outcome in a sentence or two, then the detail; that is what it relays first.",
  "The user's messages reach you as speech transcripts: they may be unpunctuated or misrecognised.",
  "The Pi voice job id at the end of a delegated request is only for routing; do not mention it in your answer.",
  "Keep it concise and action-oriented.",
  "If the brief is about the screen, this window, or a visible app, capture the frontmost window yourself.",
  "Take the first on-screen layer-0 kCGWindowNumber (the CGWindowID, not a System Events window id), run `screencapture -x -l <id>` to a temp png, use your read tool on that png, answer from the image, then delete the file.",
  "The voice line never receives the image, so describe what you saw in text.",
  "If capture fails, say the terminal that launched Pi needs Screen Recording permission. Do not guess the screen.",
  "If the brief asks to click, type, fill a form, or navigate a UI or browser, do it with the computer-use and browser tools already installed in this session.",
  "If that control fails, say the same terminal needs Accessibility permission. Voice cannot click.",
  "A delegated request arrives as <realtime_delegation>: <input> is the task, and <transcript_delta> is the voice conversation since the previous handoff, which the user does not see in this chat.",
  "Use the transcript to understand the task; do not treat it as a new request.",
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
  switch (event.tag) {
    case "ready":
    case "error":
    case "requestError":
    case "childExit":
    case "shutdown":
    case "speechStarted":
      return stepLifecycle(state, event, world);
    case "spokenDelta":
    case "composerInput":
    case "heard":
    case "spoken":
      return stepTranscript(state, event, world);
    case "work":
    case "stopWork":
    case "jobProgress":
    case "jobMessage":
    case "agentSettled":
      return stepJob(state, event, world);
  }
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
          return { render: (width: number) => faceLines(USER_LABEL, voice.heardTexts.get(id) ?? "", width), invalidate() {} };
        }
        if (data?.kind === "spoken-live" && data.id) {
          if (!voice.spokenTexts.has(data.id)) voice.spokenTexts.set(data.id, data.text ?? "");
          const id = data.id;
          return { render: (width: number) => spokenFaceLines(voice.spokenTexts.get(id) ?? "", width), invalidate() {} };
        }
        if (!data?.text) return;
        const who = data.kind === "heard" ? USER_LABEL : data.kind === "spoken" ? AGENT_LABEL : "pi";
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
  /**
   * Delegations sent in this process, by job id. The session copy (DELEGATION_TYPE
   * entries) is what survives a restart; this covers a branch read that misses one.
   */
  private readonly delegations = new Map<WorkId, string>();
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
      { value: "mute", label: "mute", description: `Mute ${AGENT_LABEL}'s mic` },
      { value: "unmute", label: "unmute", description: `Unmute ${AGENT_LABEL}'s mic` },
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

  /**
   * Pi's `context` hook: every voice job's message reaches the model as its
   * `<realtime_delegation>`, while the chat keeps the brief and the job id.
   */
  onContext(event: ContextEvent, ctx?: ExtensionContext): ContextEventResult | undefined {
    const branch = ctx?.sessionManager?.getBranch?.() ?? this.lastCtx?.sessionManager?.getBranch?.() ?? [];
    const found = delegationsFromBranch(branch);
    for (const [id, text] of this.delegations) if (!found.has(id)) found.set(id, text);
    const messages = withDelegations(event.messages, found);
    return messages ? { messages } : undefined;
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
      case "saveDelegation":
        this.delegations.set(effect.id, effect.text);
        this.pi.appendEntry?.(DELEGATION_TYPE, { id: effect.id, text: effect.text });
        return;
      case "sendWork": {
        const prompt = jobPrompt(effect.id, effect.brief);
        if (effect.deliver === "steer") this.pi.sendUserMessage(prompt, { deliverAs: "steer" });
        else this.pi.sendUserMessage(prompt);
        return;
      }
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
