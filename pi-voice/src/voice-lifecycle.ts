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

  return keep(state);
}
