import { delegationForHandoff } from "./delegation.ts";
import type { TurnKind } from "./history.ts";
import { snapshotFor } from "./work.ts";
import {
  bindJob,  lastUserJobId,
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
    const job = openJob(ready, event.id, event.brief, world, event.effort);
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
    // Progress is also how the strip learns Pi is working before the job is
    // bound; only the note is mirrored, and only for our own live job.
    if (state.job.tag !== "running" || (state.job.bound && lastUserJobId(world.branch) !== state.job.id)) return keep(state);
    const note = event.note.trim();
    if (!note || (state.job.lastNote === note && !event.tool)) return keep(state);
    const active = event.active ?? state.job.active ?? [];
    const next: VoiceState = { ...state, job: { ...state.job, lastNote: note, active } };
    const effects: Effect[] = [
      { tag: "paint", strip: strip(next, world.now) },
    ];
    // A structured transition goes straight to Voice so its status line names
    // the tool and counts the rest (Codex renders its items the same way).
    if (event.tool && event.phase) {
      effects.unshift({
        tag: "sendProgress",
        id: state.job.id,
        tool: event.tool,
        phase: event.phase,
        summary: event.summary ?? "",
        active: active.join(","),
      });
    }
    effects.unshift({
      tag: "sendJobUpdate",
      id: state.job.id,
      status: state.job.bound ? "working" : "queued",
      note,
    });
    return { state: next, effects };
  }

  if (event.tag === "statusRequest") {
    // `check_thinking` is a read. It never binds, steers, or settles the job.
    // On the durable path the host owns the state, so it answers instead.
    if (state.worker) {
      return keep(state, [{ tag: "workerAsk", id: event.id, request: event.request }]);
    }
    const snap = snapshotFor(state, world, event.id, event.request);
    return keep(state, [{ tag: "sendSnapshot", id: event.id, payload: JSON.stringify(snap) }]);
  }

  // ── durable worker path ────────────────────────────────────────────────────
  // The host reports each job independently, so a second job never has to
  // destroy the first. Everything below mirrors what the single-session path
  // does for one job, keyed by id.
  if (event.tag === "workerStatus") {
    // The host built this payload from its own job record; pass it through.
    return keep(state, [{ tag: "sendSnapshot", id: event.id, payload: event.payload }]);
  }

  if (event.tag === "workerList") {
    return keep(state, [{ tag: "sendSnapshot", id: "", payload: event.jobs }]);
  }

  if (event.tag === "workerJob") {
    // A terminal status for a job we are not tracking is still worth telling
    // Voice.app: the mirror must not show a phantom job.
    if (event.status === "failed") {
      return keep(state, [
        { tag: "upsertFace", id: `work:${event.id}:done`, kind: "work", text: `failed — ${event.note ?? "no answer"}`, final: true },
        { tag: "sendJobUpdate", id: event.id, status: "failed", note: event.note },
        { tag: "notify", message: `Pi job failed: ${event.note ?? "no answer"}`, kind: "warning" },
      ]);
    }
    if (event.status === "stopped" || event.status === "superseded") {
      return keep(state, [
        { tag: "upsertFace", id: `work:${event.id}:done`, kind: "work", text: `${event.status} — ${event.note ?? ""}`.trim(), final: true },
        { tag: "sendJobUpdate", id: event.id, status: event.status, note: event.note },
      ]);
    }
    return keep(state, [{ tag: "sendJobUpdate", id: event.id, status: event.status, note: event.note }]);
  }

  if (event.tag === "workerResult") {
    // The host already has the full answer; post it and settle that one job.
    const job = state.job.tag === "running" && state.job.id === event.id ? state.job : null;
    const next: VoiceState = job ? { ...state, job: { tag: "none" } } : state;
    return {
      state: next,
      effects: [
        {
          tag: "upsertFace",
          id: `work:${event.id}:done`,
          kind: "work",
          text: `done — ${event.full.length} chars`,
          final: true,
        },
        { tag: "sendJobUpdate", id: event.id, status: "done" },
        { tag: "postResult", id: event.id, speak: event.speak as never, full: event.full },
        { tag: "recordTurn", turn: { role: "assistant", text: `[Pi result] ${event.full}` }, turnKind: "work" },
        ...(job ? [{ tag: "paint", strip: strip(next, world.now) } as const] : []),
      ],
    };
  }

  if (event.tag === "jobMessage") return bindJob(state, event.id, world);
  if (event.tag === "agentSettled") return settleJob(state, world);

  return keep(state);
}
