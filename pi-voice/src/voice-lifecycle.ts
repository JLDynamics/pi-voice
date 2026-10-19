import type { TurnKind } from "./history.ts";
import {
  AGENT_LABEL,
  fail,
  keep,
  stopLive,
  type Step,
  type StepWorld,
  type VoiceEvent,
  type VoiceState,
} from "./voice-core.ts";
import { strip } from "./voice-face.ts";

export function stepLifecycle(state: VoiceState, event: VoiceEvent, world: StepWorld): Step {
  if (event.tag === "requestError") return keep(state, [{ tag: "notify", kind: "error",
    message: `${event.message} Your message is saved. You can ask ${AGENT_LABEL} to retry.` }]);
  if (event.tag === "shutdown") return stopLive(state);
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

  // The durable host reports ready while the child is still connecting (it is
  // started at spawn time), so it must be accepted in both states or the first
  // job would be dispatched down the single-session path.
  if (event.tag === "workerReady") {
    if (state.tag === "off") return keep(state);
    const next: VoiceState = { ...state, worker: { model: event.model } };
    return keep(next, [{ tag: "paint", strip: state.tag === "on" ? strip(next, world.now) : null }]);
  }

  // The durable host failed. Say so once and fall back: a voice call without a
  // job runner is far worse than one that steers a single session.
  if (event.tag === "workerError") {
    if (state.tag === "off") return keep(state);
    const { worker: _dropped, ...rest } = state as typeof state & { worker?: unknown };
    const next = rest as VoiceState;
    return keep(next, [
      { tag: "notify", message: `Parallel workers unavailable (${event.message}); using the single Pi session.`, kind: "warning" },
      { tag: "paint", strip: state.tag === "on" ? strip(next, world.now) : null },
    ]);
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
      ...(state.worker ? { worker: state.worker } : {}),
    };
    return {
      state: live,
      effects: [{ tag: "paint", strip: strip(live) }],
    };
  }

  return keep(state);
}
