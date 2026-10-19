// The parallel path: one `pi-durable` worker host, N jobs at once.
//
// This is the default job runner. Each job is its own conversation with its own
// model, tools, thinking level and write policy, so two briefs run side by side —
// what Codex gets from `create_thread`.
//
// The single-session path (`pi.sendUserMessage` + `deliverAs: "steer"`) can only
// run one job: a second brief steers the first away. It stays available as an
// escape hatch with `VOICE_WORKERS=single` (or `steer`), for a machine where the
// worker host cannot start.
//
// The wire is NDJSON, separate from the Voice.app stdio contract: this process
// talks to the extension, not to the voice client. See `contracts/pi-voice.json`
// `worker`.

import { spawn, type ChildProcess } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { JobUpdateStatus, ProgressPhase, ThinkingEffort } from "./child.ts";
import type { Effect, VoiceEvent, WorkId } from "./voice.ts";

const HERE = dirname(fileURLToPath(import.meta.url));

/** The host script this extension ships. */
export function hostPath(): string {
  return process.env.VOICE_WORKER_HOST || join(HERE, "worker/host.mjs");
}

/** Values that turn the durable path off and fall back to single-session steering. */
export const SINGLE_WORKER_MODES = ["single", "steer", "pi", "off"] as const;

/**
 * Whether the durable path is selected. On by default; `VOICE_WORKERS=single`
 * (or `steer`/`pi`/`off`) falls back to the single Pi session.
 */
export function durableWorkers(env: NodeJS.ProcessEnv = process.env): boolean {
  const mode = (env.VOICE_WORKERS ?? "").trim().toLowerCase();
  if (!mode) return true;
  return !(SINGLE_WORKER_MODES as readonly string[]).includes(mode);
}

export type WorkerIn =
  | { op: "spawn"; id: string; brief: string; effort: ThinkingEffort }
  | { op: "stop"; id: string }
  | { op: "status"; id: string; request: string }
  | { op: "list" }
  | { op: "quit" };

/**
 * One line from the host, mapped onto the extension's own event vocabulary so
 * the reducer does not need a second set of cases: a `job` line becomes the same
 * `sendJobUpdate` effect the single-session path produces.
 */
export type WorkerOut =
  | { ev: "ready"; model: string }
  | { ev: "progress"; id: string; tool: string; phase: ProgressPhase; summary: string; active: string }
  | { ev: "job"; id: string; status: JobUpdateStatus; note?: string }
  | { ev: "result"; id: string; speak: string; full: string }
  | { ev: "status"; id: string; payload: string }
  | { ev: "list"; jobs: string }
  | { ev: "error"; message: string };

const PHASES: readonly string[] = ["start", "update", "end", "error"];
const STATUSES: readonly string[] = ["queued", "working", "done", "stopped", "superseded", "dropped", "failed"];

/** Parse one stdout line. Never throws: a bad line is reported, not fatal. */
export function decodeWorkerLine(raw: string): WorkerOut | { ignored: string } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { ignored: "not JSON" };
  }
  if (parsed == null || typeof parsed !== "object") return { ignored: "not an object" };
  const o = parsed as Record<string, unknown>;
  const str = (key: string): string => (typeof o[key] === "string" ? (o[key] as string) : "");
  switch (o.ev) {
    case "ready":
      return { ev: "ready", model: str("model") };
    case "progress": {
      const phase = str("phase");
      if (!PHASES.includes(phase)) return { ignored: `progress: unknown phase ${phase}` };
      return {
        ev: "progress",
        id: str("id"),
        tool: str("tool"),
        phase: phase as ProgressPhase,
        summary: str("summary"),
        active: str("active"),
      };
    }
    case "job": {
      const status = str("status");
      if (!STATUSES.includes(status)) return { ignored: `job: unknown status ${status}` };
      const note = str("note");
      return { ev: "job", id: str("id"), status: status as JobUpdateStatus, ...(note ? { note } : {}) };
    }
    case "result":
      return { ev: "result", id: str("id"), speak: str("speak"), full: str("full") };
    case "status":
      return { ev: "status", id: str("id"), payload: str("payload") };
    case "list":
      return { ev: "list", jobs: str("jobs") };
    case "error":
      return { ev: "error", message: str("message") || "worker error" };
    default:
      return { ignored: `unknown ev ${String(o.ev)}` };
  }
}

/**
 * The worker host process. One per voice call: the host holds the `pi-durable`
 * session, so starting a second would split the jobs across two harnesses.
 */
export class WorkerHost {
  private readonly proc: ChildProcess;
  private leftover = "";
  private readonly ignored = new Set<string>();

  private constructor(proc: ChildProcess) {
    this.proc = proc;
  }

  static start(onEvent: (event: VoiceEvent) => void, onProblem: (line: string) => void): WorkerHost {
    const script = hostPath();
    if (!existsSync(script)) throw new Error(`worker host missing: ${script}`);
    const proc = spawn(process.execPath, [script], {
      stdio: ["pipe", "pipe", "pipe"],
      env: process.env,
    });
    const host = new WorkerHost(proc);
    proc.stdout?.setEncoding("utf8");
    proc.stdout?.on("data", (chunk: string) => host.ingest(chunk, onEvent, onProblem));
    proc.stderr?.on("data", (chunk: Buffer) => {
      // The host logs one line per event there; surface it as a note, not a turn.
      for (const line of chunk.toString().split("\n")) if (line.trim()) onProblem(line.trim());
    });
    proc.on("exit", (code) => {
      onEvent({ tag: "workerError", message: `worker host exited (${code ?? "signal"})` });
    });
    return host;
  }

  private ingest(chunk: string, onEvent: (event: VoiceEvent) => void, onProblem: (line: string) => void): void {
    this.leftover += chunk;
    let nl = this.leftover.indexOf("\n");
    while (nl >= 0) {
      const line = this.leftover.slice(0, nl).trim();
      this.leftover = this.leftover.slice(nl + 1);
      if (line) {
        const decoded = decodeWorkerLine(line);
        if ("ignored" in decoded) {
          // One line per distinct reason, so a bad sender does not flood the log.
          if (!this.ignored.has(decoded.ignored)) {
            this.ignored.add(decoded.ignored);
            onProblem(`ignored worker line: ${decoded.ignored}`);
          }
        } else {
          onEvent(toVoiceEvent(decoded));
        }
      }
      nl = this.leftover.indexOf("\n");
    }
  }

  send(msg: WorkerIn): void {
    const stdin = this.proc.stdin;
    if (!stdin || stdin.destroyed) return;
    stdin.write(`${JSON.stringify(msg)}\n`);
  }

  spawnJob(id: WorkId, brief: string, effort: ThinkingEffort): void {
    this.send({ op: "spawn", id, brief, effort });
  }

  steerJob(id: WorkId, brief: string, effort: ThinkingEffort): void {
    // The host treats `spawn` on a live id as a steer, which is what the voice
    // model's "a second brief replaces the task" means.
    this.send({ op: "spawn", id, brief, effort });
  }

  stopJob(id: WorkId): void {
    this.send({ op: "stop", id });
  }

  askStatus(id: WorkId | "", request: string): void {
    this.send({ op: "status", id, request });
  }

  quit(): void {
    this.send({ op: "quit" });
    const proc = this.proc;
    setTimeout(() => {
      if (proc.exitCode == null && proc.signalCode == null) proc.kill("SIGTERM");
    }, 3000).unref?.();
  }

  kill(): void {
    try {
      this.proc.kill("SIGKILL");
    } catch {
      // already gone
    }
  }
}

/**
 * Map a host line onto the extension's event vocabulary.
 *
 * `result` becomes a `jobMessage`-free path: the host already knows the answer,
 * so the extension posts it straight to Voice.app through the existing `spoken`
 * bookkeeping rather than re-reading a session branch that does not contain it.
 */
export function toVoiceEvent(out: WorkerOut): VoiceEvent {
  switch (out.ev) {
    case "job":
      // The reducer's `jobProgress` needs a note; a status-only line is a phase.
      return { tag: "workerJob", id: out.id as WorkId, status: out.status, note: out.note };
    case "progress":
      return {
        tag: "jobProgress",
        // The same wording the single-session path uses, so the status line and
        // the [STATUS] channel read identically whichever path runs the job.
        note: activityNote(out.tool, out.phase, out.summary),
        tool: out.tool,
        phase: out.phase,
        summary: out.summary,
        active: out.active ? out.active.split(",").filter(Boolean) : [],
      };
    case "result":
      return { tag: "workerResult", id: out.id as WorkId, speak: out.speak, full: out.full };
    case "status":
      return { tag: "workerStatus", id: out.id as WorkId | "", payload: out.payload };
    case "list":
      return { tag: "workerList", jobs: out.jobs };
    case "ready":
      return { tag: "workerReady", model: out.model };
    case "error":
      // The host reporting a problem must not end the call: it degrades to the
      // single-session path so the user keeps talking.
      return { tag: "workerError", message: out.message };
  }
}



/**
 * Start the durable host when the call asked for it.
 *
 * Started once per call, not per job: the host owns the `pi-durable` session, so
 * a second host would split the jobs across two harnesses and neither would see
 * the other's state. Returns undefined when the single-session path is in use.
 */
export interface WorkerHandle {
  spawnJob(id: WorkId, brief: string, effort: ThinkingEffort): void;
  stopJob(id: WorkId): void;
  askStatus(id: WorkId | "", request: string): void;
  quit(): void;
}

export function startWorkersIfEnabled(
  epoch: number,
  isCurrent: (epoch: number) => boolean,
  onEvent: (event: VoiceEvent) => void,
  onProblem: (line: string) => void,
): WorkerHandle | undefined {
  if (!durableWorkers()) return undefined;
  let host: WorkerHost;
  try {
    host = WorkerHost.start((event) => {
      if (!isCurrent(epoch)) return;
      onEvent(event);
    }, onProblem);
  } catch (error) {
    // Now that the durable path is the default, a host that cannot start must
    // degrade to the single Pi session rather than end the call.
    const message = error instanceof Error ? error.message : String(error);
    onProblem(`worker host unavailable: ${message}`);
    onEvent({ tag: "workerError", message });
    return undefined;
  }
  return {
    spawnJob: (id, brief, effort) => host.spawnJob(id, brief, effort),
    stopJob: (id) => host.stopJob(id),
    askStatus: (id, request) => host.askStatus(id, request),
    quit: () => host.quit(),
  };
}

/**
 * Apply the three worker effects. Kept here so `voice.ts` stays under its line
 * budget and the durable path has one place to change.
 */
export function applyWorkerEffect(
  effect: Extract<Effect, { tag: "workerSpawn" | "workerStop" | "workerAsk" }>,
  workers: WorkerHandle | undefined,
): void {
  if (effect.tag === "workerSpawn") workers?.spawnJob(effect.id, effect.brief, effect.effort);
  else if (effect.tag === "workerStop") workers?.stopJob(effect.id);
  else workers?.askStatus(effect.id, effect.request);
}

/** "Pi is running a command (git status)" — never "Pi is bash". */
export function activityNote(tool: string, phase: ProgressPhase, summary: string): string {
  const label = tool === "web_search" || tool === "source_check" ? "searching the web"
    : tool === "fetch_content" || tool === "get_search_content" ? "reading sources"
    : tool === "read" || tool === "grep" ? "reading files"
    : tool === "edit" || tool === "write" ? "editing files"
    : tool === "bash" ? "running a command"
    : tool === "ls" ? "listing files"
    : tool === "find" ? "searching for files"
    : tool === "skill" ? "loading a skill"
    : tool === "screenshot" ? "looking at the screen"
    : "using a tool";
  const verb = phase === "end" ? `finished ${label}` : phase === "error" ? `${label} failed` : label;
  return `Pi is ${verb}${summary ? ` (${summary})` : ""}`;
}
