import type { SessionEntry } from "@earendil-works/pi-coding-agent";
import type { JobUpdateStatus, VoiceHistoryTurn } from "./child.ts";
import type { TranscriptEntry } from "./delegation.ts";
import { leftoverTurn, type TurnKind } from "./history.ts";

export type ChildPid = number & { readonly brand: "ChildPid" };
export type WorkId = string & { readonly brand: "WorkId" };
export type UserText = string & { readonly brand: "UserText" };

/** Leading excerpt of Pi's answer for the [FINAL] channel. The full text is cached beside it. */
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
      /** Latest Pi tool activity, mirrored to Voice.app for the [STATUS] channel. */
      lastNote: string | null;
      /**
       * The voice job this one replaced. Pi may already have answered it before
       * the steer landed; that answer is relayed once it shows up in the branch.
       */
      prior?: { id: WorkId; brief: UserText } | null;
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
      /**
       * Voice transcript since the last handoff (Codex's active transcript): what
       * was heard and what Agent said aloud, never typed text, tools, or job items.
       * Each `work` hands it to Pi in the delegation and starts it again empty.
       */
      transcript?: readonly TranscriptEntry[];
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
  /** Store the job's `<realtime_delegation>` hidden in Pi's session, for the model only. */
  | { tag: "saveDelegation"; id: WorkId; text: string }
  | { tag: "postResult"; id: WorkId; speak: ShortResult; full: string }
  | {
      tag: "sendJobUpdate";
      id: WorkId;
      status: JobUpdateStatus;
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

/** Name shown next to the voice assistant's lines in the transcript (the voice persona's name too). */
export const AGENT_LABEL = "Agent";
/** Name shown next to the user's spoken lines in the transcript. */
export const USER_LABEL = "You";

export function asUser(text: string): UserText {
  return text as UserText;
}

export function asWork(id: string): WorkId {
  return id as WorkId;
}

export function keep(state: VoiceState, effects: Effect[] = []): Step {
  return { state, effects };
}

/** Uncommitted speech only. Completed turns are already in the shared log. */
export function flushLeftovers(state: VoiceState): Effect[] {
  if (state.tag !== "on") return [];
  const effects: Effect[] = [];
  const heard = state.heardPending?.trim() ?? "";
  if (heard) effects.push({ tag: "recordTurn", turn: leftoverTurn("user", heard), turnKind: "voice" });
  const spoken = state.streamingSpoken.trim();
  if (spoken) effects.push({ tag: "recordTurn", turn: leftoverTurn("assistant", spoken), turnKind: "voice" });
  return effects;
}

export function stopLive(state: VoiceState): Step {
  if (state.tag === "off") return keep(state);
  return {
    state: { tag: "off" },
    effects: [...flushLeftovers(state), { tag: "quitChild" }, { tag: "paint", strip: null }],
  };
}

export function fail(state: VoiceState, message: string): Step {
  const effects: Effect[] = [...flushLeftovers(state)];
  if (state.tag !== "off") effects.push({ tag: "quitChild" });
  effects.push({ tag: "paint", strip: null }, { tag: "notify", message, kind: "error" });
  return { state: { tag: "off" }, effects };
}

export function withMic(state: VoiceState, mic: Mic): VoiceState {
  if (state.tag === "off") return state;
  return { ...state, mic };
}
