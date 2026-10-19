import type { SessionEntry } from "@earendil-works/pi-coding-agent";
import type { Effect, Job, ShortResult, Step, StepWorld, UserText, VoiceState, WorkId } from "./voice.ts";
import { DEFAULT_EFFORT, type ThinkingEffort } from "./child.ts";
import { RESULT_MAX, strip } from "./voice.ts";
import { handoffHistoryTurn, workHistoryTurn } from "./history.ts";

function keep(state: VoiceState, effects: Effect[] = []): Step {
  return { state, effects };
}

function asUser(text: string): UserText {
  return text as UserText;
}

function asShort(text: string): ShortResult {
  return text as ShortResult;
}

function paint(state: VoiceState, now: number): Effect {
  return { tag: "paint", strip: strip(state, now) };
}

/** Terminal outcome is sent before findings; caching cannot imply completion. */
export function terminalJob(job: Extract<Job, { tag: "running" }>, world: StepWorld,
  status: "stopped" | "superseded", note?: string): Effect[] {
  const effects: Effect[] = [{ tag: "sendJobUpdate", id: job.id, status, note }];
  const full = (answerForJob(world.branch, job.id) ?? "").trim();
  const speak = fullResult(full);
  if (speak) effects.push(
    { tag: "postResult", id: job.id, speak, full },
    { tag: "recordTurn", turn: { role: "assistant", text: `[Partial Pi findings; task ${status}] ${full}` }, turnKind: "work" },
  );
  return effects;
}

/**
 * A voice job replaced by a newer dispatch is marked superseded at once, but Pi
 * may already have finished it: the steer lands after Pi's current turn, so a
 * second request made while Pi was writing its answer left that answer in the
 * branch with nobody to relay or save it (seen live: a FIXME search answered,
 * then a new task arrived, and the finding was never spoken). Relay it once.
 *
 * Pi finished it: the job is reported done before its result, so Voice frames
 * the answer as complete, matching the `[Pi result …]` row the shared log keeps.
 * Pi was cut off mid-turn (the steer landed after a tool batch): it stays
 * partial on both sides.
 */
export function priorFindings(job: Extract<Job, { tag: "running" }>, world: StepWorld): Effect[] {
  if (!job.prior) return [];
  const full = (answerForJob(world.branch, job.prior.id) ?? "").trim();
  const speak = fullResult(full);
  if (!speak) return [];
  if (!answerFinished(world.branch, job.prior.id)) {
    return [
      { tag: "postResult", id: job.prior.id, speak, full },
      { tag: "recordTurn", turn: { role: "assistant", text: `[Partial Pi findings; task superseded] ${full}` }, turnKind: "work" },
    ];
  }
  return [
    { tag: "sendJobUpdate", id: job.prior.id, status: "done" },
    { tag: "postResult", id: job.prior.id, speak, full },
    { tag: "recordTurn", turn: workHistoryTurn(job.prior.brief, full), turnKind: "work" },
  ];
}

/** Clear `prior` once its answer was relayed, so it is not spoken twice. */
function withoutPrior(job: Extract<Job, { tag: "running" }>, effects: Effect[]): Extract<Job, { tag: "running" }> {
  return effects.length > 0 ? { ...job, prior: null } : job;
}

export function openJob(
  state: VoiceState,
  id: WorkId,
  brief: UserText,
  world: StepWorld,
  effort: ThinkingEffort = DEFAULT_EFFORT,
): Step {
  if (state.tag !== "on" || state.mic.tag !== "open") return keep(state);
  const effects: Effect[] = [];
  // Single-session path: a new routing id for the same live Pi operation, so
  // the old job is superseded (steered away) but its context survives. Explicit
  // stop_thinking still owns cancellation.
  //
  // Durable path: every job is its own conversation, so a second brief is a
  // second job running alongside the first. Nothing is superseded — that is the
  // difference the parallel workers buy, and the reason this branch exists.
  if (!state.worker && state.job.tag === "running") {
    effects.push({ tag: "sendJobUpdate", id: state.job.id, status: "superseded", note: "Task updated; Pi continues" });
  }
  const deliver = state.job.tag === "running" || !world.idle ? "steer" : "plain";
  const job: Job = {
    tag: "running",
    id,
    brief,
    startedAt: world.now,
    bound: false,
    afterEntryId: world.leafId ?? null,
    lastNote: null,
    effort,
    prior: state.job.tag === "running" ? { id: state.job.id, brief: state.job.brief } : null,
  };
  const next: VoiceState = { ...state, job };
  effects.push(
    // Durable workers run N jobs at once, so a second brief is its own job
    // rather than a steer. The single-session path keeps the steer.
    state.worker
      ? { tag: "workerSpawn", id, brief, effort }
      : { tag: "sendWork", id, brief, deliver },
    // A worker job leaves no trace in Pi's session, so the transcript would be
    // silent while it runs. This custom entry makes it visible; Pi keeps custom
    // entries out of model context, so the job does not enter Pi's conversation.
    ...(state.worker
      ? [{ tag: "upsertFace" as const, id: `work:${id}`, kind: "work" as const, text: brief, final: true }]
      : []),
    { tag: "recordTurn", turn: handoffHistoryTurn(brief), turnKind: "work" },
    paint(next, world.now),
  );
  return { state: next, effects };
}

export function bindJob(state: VoiceState, id: WorkId, world?: StepWorld): Step {
  if (state.tag !== "on" || state.job.tag !== "running" || state.job.bound || state.job.id !== id) return keep(state);
  const findings = world ? priorFindings(state.job, world) : [];
  const next: VoiceState = { ...state, job: { ...withoutPrior(state.job, findings), bound: true } };
  return keep(next, [{ tag: "sendJobUpdate", id: state.job.id, status: "working" }, ...findings]);
}

export function settleJob(state: VoiceState, world: StepWorld): Step {
  if (state.tag !== "on" || state.job.tag !== "running" || !state.job.bound) return keep(state);
  const findings = priorFindings(state.job, world);
  if (findings.length > 0) {
    // Relay the replaced job's answer first; the current job settles as usual.
    const rest = settleJob({ ...state, job: withoutPrior(state.job, findings) }, world);
    return { state: rest.state, effects: [...findings, ...rest.effects] };
  }
  if (lastUserJobId(world.branch) !== state.job.id) {
    if (!hasJobMessage(world.branch, state.job.id)) return keep(state);
    const next: VoiceState = { ...state, job: { tag: "none" } };
    return { state: next, effects: [
      ...terminalJob(state.job, world, "superseded", "Another Pi request took over"),
      paint(next, world.now),
    ] };
  }
  const full = (answerForJob(world.branch, state.job.id) ?? "").trim();
  const speak = fullResult(full);
  const next: VoiceState = { ...state, job: { tag: "none" } };
  if (!speak) {
    // Pi settled with nothing to say (every turn errored, or an empty
    // answer). Report it as failed — not done — so the status channel can tell
    // Agent the job died instead of going quiet.
    const failed: Effect = {
      tag: "sendJobUpdate",
      id: state.job.id,
      status: "failed",
      note: "Pi finished with no answer",
    };
    return { state: next, effects: [failed, paint(next, world.now)] };
  }
  const settled: Effect = { tag: "sendJobUpdate", id: state.job.id, status: "done" };
  return {
    state: next,
    effects: [
      settled,
      { tag: "postResult", id: state.job.id, speak, full },
      { tag: "recordTurn", turn: workHistoryTurn(state.job.brief, full), turnKind: "work" },
      paint(next, world.now),
    ],
  };
}

/**
 * First part of Pi's answer for Luna's immediate spoken handoff.
 *
 * This used to extract a speakable fragment here — the head above a `---`
 * line, else the first two sentences, markup stripped, capped at 240. That
 * decided what mattered without understanding the answer, and it was usually
 * wrong: Pi's closing "Done, anything else?" is the last message, the finding
 * often sits in sentence four, and code fences were deleted outright, so the
 * one value being asked about could vanish before anyone read it.
 *
 * Pi's full answer is carried separately and cached in Voice.app. The voice
 * model hears this leading excerpt on the [FINAL] channel. Pi is prompted to
 * lead with the outcome, so the excerpt can be spoken without filling the
 * voice context. More detail is another spawn_thinking, not a paging tool.
 */
export function fullResult(assistantText: string): ShortResult | undefined {
  const text = assistantText.trim();
  if (!text) return;
  return asShort(capGraphemes(text, RESULT_MAX));
}

export function lastUserText(branch: SessionEntry[]): UserText | undefined {
  for (let i = branch.length - 1; i >= 0; i--) {
    const message = sessionMessage(branch[i]);
    if (message?.role !== "user") continue;
    return asUser(visibleText(message.content));
  }
}

/**
 * The visible Pi message for a voice job: the brief, then the id that links one
 * agent answer to one voice job. The model sees the job's Codex-style delegation
 * instead; see `withDelegations`. The job id stays last so routing still matches.
 */
export function jobPrompt(id: WorkId, brief: UserText): string {
  return `${brief}\n\n${jobMarker(id)}`;
}

function jobMarker(id: WorkId): string {
  return `[Pi voice job id: ${id}]`;
}

/**
 * Hidden session entry holding the delegation sent with one voice job. Pi draws a
 * custom entry only through a registered renderer (none for this type) and never
 * puts custom entries in model context, so it is stored but not shown.
 */
export const DELEGATION_TYPE = "pi-voice-delegation";

export type DelegationEntry = { id: WorkId; text: string };

/** Every stored delegation on this branch, by job id. */
export function delegationsFromBranch(branch: readonly SessionEntry[]): Map<WorkId, string> {
  const found = new Map<WorkId, string>();
  for (const entry of branch) {
    if (entry.type !== "custom" || (entry as { customType?: string }).customType !== DELEGATION_TYPE) continue;
    const data = (entry as { data?: Partial<DelegationEntry> }).data;
    if (data && typeof data.id === "string" && typeof data.text === "string" && data.text) {
      found.set(data.id as WorkId, data.text);
    }
  }
  return found;
}

/** A conversation message as Pi's `context` hook hands it over (only what is read here). */
type ContextMessage = { role: string; content?: unknown };

/**
 * Give the model each voice job as Codex gives its backend agent a handoff: the
 * `<realtime_delegation>` block (brief + voice transcript since the previous
 * handoff) in place of the visible brief, with the job id kept last.
 *
 * Pi's `context` hook runs before each model request on a copy of the
 * conversation; nothing it returns is saved or drawn, so the chat keeps showing
 * the brief and the id. Every job message on the branch is rewritten, not only
 * the latest, so earlier handoffs' transcript stays in view on later turns, as it
 * does in Codex where the delegation is the stored user message.
 * Returns undefined when no message changes.
 */
export function withDelegations<M extends ContextMessage>(messages: readonly M[], delegations: ReadonlyMap<WorkId, string>): M[] | undefined {
  if (delegations.size === 0) return;
  let next: M[] | undefined;
  messages.forEach((message, at) => {
    if (message.role !== "user") return;
    const id = jobIdFromText(visibleText(message.content));
    const delegation = id ? delegations.get(id) : undefined;
    if (!id || !delegation) return;
    const text = { type: "text", text: `${delegation}\n\n${jobMarker(id)}` };
    const rest = Array.isArray(message.content)
      ? message.content.filter((part) => !(part && typeof part === "object" && (part as { type?: unknown }).type === "text"))
      : [];
    next ??= messages.slice();
    next[at] = { ...message, content: [text, ...rest] } as M;
  });
  return next;
}

export function lastUserJobId(branch: SessionEntry[]): WorkId | undefined {
  const text = lastUserText(branch);
  return text ? jobIdFromText(text) : undefined;
}

export function jobIdFromText(text: string): WorkId | undefined {
  const match = text.match(/(?:^|\n\n)\[Pi voice job id: ([^\]\r\n]+)\]$/);
  return match?.[1] as WorkId | undefined;
}

/** Short activity notes for Luna; tool arguments and results never cross the bridge. */
export function toolProgress(phase: "start" | "update" | "end" | "error", name: string): string {
  const tool = name.toLowerCase();
  const activity = tool === "web_search" || tool === "source_check" ? "searching the web"
    : tool === "fetch_content" || tool === "get_search_content" ? "reading sources"
    : tool === "read" || tool === "grep" || tool === "find" || tool === "ls" ? "reading files"
    : tool === "edit" || tool === "write" || tool === "apply_patch" ? "editing files"
    : tool === "bash" ? "running a command"
    : "using a tool";
  const step = tool === "web_search" || tool === "source_check" ? "web search"
    : tool === "fetch_content" || tool === "get_search_content" ? "source reading"
    : tool === "read" || tool === "grep" || tool === "find" || tool === "ls" ? "file reading"
    : tool === "edit" || tool === "write" || tool === "apply_patch" ? "file edit"
    : tool === "bash" ? "command"
    : "tool";
  if (phase === "start") return `Pi is ${activity}`;
  if (phase === "update") return `Pi is ${activity}; results are arriving`;
  if (phase === "error") return `Pi's ${step} failed; Pi is continuing`;
  return `Pi finished ${activity}; reviewing the result`;
}

/**
 * A short, speakable name for a tool call's target: the query, the path, the
 * command's first word. Codex shows the same thing next to its status line
 * ("Searching the web for <query>"). Only the head is kept, and only what the
 * user already knows about their own request — never a result.
 */
export function toolSummary(name: string, args: unknown): string {
  if (!args || typeof args !== "object") return "";
  const a = args as Record<string, unknown>;
  const pick = (...keys: string[]): string => {
    for (const key of keys) {
      const value = a[key];
      if (typeof value === "string" && value.trim()) return value.trim();
    }
    return "";
  };
  const tool = name.toLowerCase();
  const raw = tool === "web_search" || tool === "source_check" ? pick("query", "q")
    : tool === "fetch_content" ? pick("url")
    : tool === "read" || tool === "write" || tool === "edit" ? pick("path", "file_path", "file")
    : tool === "grep" || tool === "find" ? pick("pattern", "query", "glob")
    : tool === "ls" ? pick("path")
    : tool === "bash" ? pick("command")
    : "";
  const flat = raw.replace(/\s+/g, " ").trim();
  if (!flat) return "";
  return flat.length > 60 ? `${flat.slice(0, 59)}…` : flat;
}

/** What `check_thinking` answers: live state, never a guess and never a steer. */
export type Snapshot = {
  request: string;
  status: "working" | "idle" | "settled";
  id?: string;
  brief?: string;
  elapsed_s?: number;
  doing?: string;
  running?: readonly string[];
  answer?: string;
  answer_chars?: number;
  outcome?: string;
  ago_s?: number;
  note?: string;
};

const SNAPSHOT_HEAD = 900;

/**
 * Build the answer to `check_thinking` from Pi's own session.
 *
 * This is the read-only coordination verb the voice model was missing: Codex's
 * voice agent can `read_thread`/`wait_threads` a worker, while Luna could only
 * wait for a throttled `[STATUS]` or a finished `[FINAL]`. Nothing here mutates
 * the job; `request` is echoed so the model can match answer to question.
 */
export function snapshotFor(state: VoiceState, world: StepWorld, id: WorkId | "", request: string): Snapshot {
  if (state.tag !== "on") return { request, status: "idle", note: "Voice is not on in this session." };
  const wanted = id || (state.job.tag === "running" ? state.job.id : "");
  const job = state.job.tag === "running" && (!wanted || state.job.id === wanted) ? state.job : null;
  if (job) {
    const answer = (answerForJob(world.branch, job.id) ?? "").trim();
    const snap: Snapshot = {
      request,
      status: "working",
      id: job.id,
      brief: job.brief,
      elapsed_s: Math.max(0, Math.floor((world.now - job.startedAt) / 1000)),
    };
    if (job.lastNote) snap.doing = job.lastNote;
    if (job.active && job.active.length > 0) snap.running = job.active;
    if (answer) {
      snap.answer = answer.length > SNAPSHOT_HEAD ? `${answer.slice(0, SNAPSHOT_HEAD - 1)}…` : answer;
      snap.answer_chars = answer.length;
    }
    snap.note = answer
      ? "This is Pi's answer so far and the task may still be running. Relay it as a short spoken update in your own words. Do not call stop_thinking for a status question."
      : "Pi has not written an answer yet. Say briefly what it is doing; do not invent findings. Do not call stop_thinking for a status question.";
    return snap;
  }
  const last = lastAssistantText(world.branch, null);
  if (last) {
    return {
      request,
      status: "settled",
      answer: last.length > SNAPSHOT_HEAD ? `${last.slice(0, SNAPSHOT_HEAD - 1)}…` : last,
      answer_chars: last.length,
      note: "No voice job is running. This is Pi's last answer in the session.",
    };
  }
  return { request, status: "idle", note: "Nothing has been handed to Pi in this call." };
}

/** pi-web-access writes one Source line per result in its web_search text. */
export function webSourceCount(result: string): number {
  return (result.match(/^Source:\s/gm) ?? []).length;
}

export function answerForJob(branch: SessionEntry[], id: WorkId): string | undefined {
  const answer = answerMessage(branch, id);
  return answer ? visibleText(answer.content) : undefined;
}

/** Stop reasons that mean Pi was cut off before it ended its turn. */
const CUT_OFF = new Set(["toolUse", "error", "aborted", "length"]);

/**
 * Whether the job's answer ended Pi's turn. A steer is delivered after Pi's
 * current tool batch, so the last answer before it can be a mid-turn step
 * (`toolUse`). Entries without a stop reason count as finished.
 */
export function answerFinished(branch: SessionEntry[], id: WorkId): boolean {
  const answer = answerMessage(branch, id);
  return answer != null && !CUT_OFF.has(answer.stopReason ?? "");
}

function answerMessage(branch: SessionEntry[], id: WorkId): { content: unknown; stopReason?: string } | undefined {
  let marker = -1;
  for (let i = branch.length - 1; i >= 0; i--) {
    const message = sessionMessage(branch[i]);
    if (message?.role !== "user") continue;
    if (jobIdFromText(visibleText(message.content)) === id) {
      marker = i;
      break;
    }
  }
  if (marker < 0) return;
  let answer: { content: unknown; stopReason?: string } | undefined;
  for (let i = marker + 1; i < branch.length; i++) {
    const message = sessionMessage(branch[i]);
    if (message?.role === "user") break;
    if (message?.role === "assistant") answer = message;
  }
  return answer;
}

function hasJobMessage(branch: SessionEntry[], id: WorkId): boolean {
  return branch.some((entry) => {
    const message = sessionMessage(entry);
    return message?.role === "user" && jobIdFromText(visibleText(message.content)) === id;
  });
}

export function lastAssistantText(branch: SessionEntry[], afterEntryId: string | null): string | undefined {
  let start = 0;
  if (afterEntryId != null) {
    const idx = branch.findIndex((entry) => entry.id === afterEntryId);
    if (idx >= 0) start = idx + 1;
  }
  let last: SessionEntry | undefined;
  for (let i = start; i < branch.length; i++) {
    const message = sessionMessage(branch[i]);
    if (message?.role === "assistant") last = branch[i];
  }
  if (!last) return;
  const text = visibleText(sessionMessage(last)?.content);
  if (!text.trim()) return;
  return text;
}



function capGraphemes(text: string, max: number): string {
  const units = graphemes(text);
  if (units.length <= max) return text;
  const cut = units.slice(0, max).join("");
  const sentence = cut.match(/^[\s\S]*[.!?](?=\s|$)/);
  if (sentence && sentence[0].trim()) return sentence[0].trim();
  const word = cut.replace(/\s+\S*$/, "").trim();
  return word || cut.trim();
}

function graphemes(text: string): string[] {
  const Segmenter = (Intl as typeof Intl & { Segmenter?: typeof Intl.Segmenter }).Segmenter;
  if (typeof Segmenter === "function") {
    return [...new Segmenter("en", { granularity: "grapheme" }).segment(text)].map((part) => part.segment);
  }
  return [...text];
}

function sessionMessage(entry: SessionEntry | undefined): { role: string; content: unknown; stopReason?: string } | undefined {
  if (!entry || entry.type !== "message") return;
  const message = (entry as { message?: { role?: unknown; content?: unknown; stopReason?: unknown } }).message;
  if (!message || typeof message.role !== "string") return;
  const stopReason = typeof message.stopReason === "string" ? message.stopReason : undefined;
  return { role: message.role, content: message.content, stopReason };
}

function visibleText(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  const parts: string[] = [];
  for (const block of content) {
    if (block && typeof block === "object" && (block as { type?: unknown }).type === "text") {
      const text = (block as { text?: unknown }).text;
      if (typeof text === "string") parts.push(text);
    }
  }
  return parts.join("");
}
