import { delegationForHandoff } from "./delegation.ts";
import type { TurnKind } from "./history.ts";
import {
  bindJob,
  lastUserJobId,
  openJob,
  priorFindings,
  settleJob,
  terminalJob,
} from "./work.ts";
import {
  keep,
  type Effect,
  type Step,
  type StepWorld,
  type VoiceEvent,
  type VoiceState,
} from "./voice-core.ts";
import { strip } from "./voice-face.ts";

export function stepJob(state: VoiceState, event: VoiceEvent, world: StepWorld): Step {
  if (state.tag !== "on") return keep(state);

  if (event.tag === "work") {
    // A dispatch Agent made while muted never reaches Pi; tell Voice.app the
    // id is dead so the job mirror does not report a phantom job.
    if (state.mic.tag === "closed") {
      return keep(state, [{ tag: "sendJobUpdate", id: event.id, status: "dropped" }]);
    }
    // Pi's user message is written as soon as sendWork runs. Save the spoken
    // request first, or the search appears in the transcript before the words
    // that prompted it. The later `spoken` event must not save it again.
    const pending = state.heardPending;
    // Like Codex, the handoff takes the transcript so far and the next one starts empty.
    const delegation: Effect = { tag: "saveDelegation", id: event.id,
      text: delegationForHandoff(state.transcript ?? [], event.brief) };
    const ready: VoiceState = pending
      ? { ...state, heardPending: null, heardItemId: null, transcript: [] }
      : { ...state, transcript: [] };
    const job = openJob(ready, event.id, event.brief, world);
    const record: Effect[] = pending
      ? [{ tag: "recordTurn", turn: { role: "user", text: pending }, turnKind: "voice" as TurnKind }]
      : [];
    return pending
      ? { state: job.state, effects: [{ tag: "upsertFace", id: state.heardItemId!, kind: "heard", text: pending, final: true }, ...record, delegation, ...job.effects] }
      : { state: job.state, effects: [delegation, ...job.effects] };
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
      ...priorFindings(state.job, world),
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

  if (event.tag === "jobMessage") return bindJob(state, event.id, world);
  if (event.tag === "agentSettled") return settleJob(state, world);

  return keep(state);
}
