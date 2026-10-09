import { appendTranscript, setTranscript } from "./delegation.ts";
import {
  asUser,
  keep,
  type Effect,
  type Step,
  type StepWorld,
  type VoiceEvent,
  type VoiceState,
} from "./voice-core.ts";
import { strip } from "./voice-face.ts";

export function stepTranscript(state: VoiceState, event: VoiceEvent, world: StepWorld): Step {
  if (state.tag !== "on") return keep(state);

  if (event.tag === "spokenDelta") {
    if (event.itemId && event.itemId === state.interruptedSpokenId) return keep(state);
    const id = event.itemId || state.spokenItemId || `luna:${world.now}`;
    const newTurn = state.spokenItemId !== null && id !== state.spokenItemId;
    const text = (newTurn ? "" : state.streamingSpoken) + event.text;
    const next: VoiceState = { ...state, streamingSpoken: text, spokenItemId: id,
      interruptedSpokenId: id === state.interruptedSpokenId ? state.interruptedSpokenId : null,
      transcript: appendTranscript(state.transcript ?? [], "assistant", event.text, id) };
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
      transcript: setTranscript(state.transcript ?? [], "user", text, itemId),
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
    const next: VoiceState = { ...state, lastSpoken: text, streamingSpoken: "", spokenItemId: null, heardPending: null, heardItemId: null,
      transcript: setTranscript(state.transcript ?? [], "assistant", text, id) };
    effects.push({ tag: "upsertFace", id, kind: "spoken", text, final: true }, { tag: "paint", strip: strip(next, world.now) });
    effects.push({
      tag: "recordTurn",
      turn: { role: "assistant", text },
      turnKind: state.job.tag === "running" ? "progress" : "voice",
    });
    return { state: next, effects };
  }

  return keep(state);
}
