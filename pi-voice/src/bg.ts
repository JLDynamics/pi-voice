// Background coding tasks for Pi, with no voice call involved.
//
// `/bg <task>` hands the task to the same durable worker host the voice jobs use
// (`worker/host.mjs`): each task gets its own conversation, runs in parallel with
// the others and with your chat, and survives a crash. The finished answer comes
// back into the chat as a message, and the main agent can see it.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import type { VoiceEvent, WorkId } from "./voice.ts";
import { WorkerHost } from "./worker.ts";

export const BG_TYPE = "pi-bg";

/** The part of `WorkerHost` the commands use, so tests can stand in for it. */
export interface BgHost {
  spawnJob(id: WorkId, brief: string, effort: "off" | "minimal" | "low" | "medium" | "high"): void;
  stopJob(id: WorkId): void;
  quit(): void;
}

export type BgStatus = "running" | "done" | "stopped" | "failed";

export interface BgJob {
  id: string;
  brief: string;
  status: BgStatus;
  note: string;
}

export interface BgSink {
  notify(text: string): void;
  /** Put a finished answer into the chat so the agent can read it. */
  deliver(id: string, text: string): void;
}

export type BgCommand =
  | { kind: "start"; brief: string }
  | { kind: "list" }
  | { kind: "stop"; id: string }
  | { kind: "help" };

export const BG_HELP = "Usage: /bg <task> to start one, /bg list, /bg stop <id>";

export function parseBg(args: string): BgCommand {
  const text = args.trim();
  if (!text) return { kind: "list" };
  const [head, ...rest] = text.split(/\s+/);
  const word = head.toLowerCase();
  if (word === "list" && rest.length === 0) return { kind: "list" };
  if (word === "help") return { kind: "help" };
  if (word === "stop") return rest[0] ? { kind: "stop", id: rest[0] } : { kind: "help" };
  return { kind: "start", brief: text };
}

export class Background {
  private host: BgHost | undefined;
  private counter = 0;
  readonly jobs = new Map<string, BgJob>();

  private readonly sink: BgSink;
  private readonly startHost: (onEvent: (event: VoiceEvent) => void) => BgHost;

  constructor(sink: BgSink, startHost: (onEvent: (event: VoiceEvent) => void) => BgHost) {
    this.sink = sink;
    this.startHost = startHost;
  }

  run(command: BgCommand): void {
    switch (command.kind) {
      case "help":
        this.sink.notify(BG_HELP);
        return;
      case "list":
        this.sink.notify(this.describe());
        return;
      case "stop": {
        const job = this.jobs.get(command.id);
        if (!job || job.status !== "running") {
          this.sink.notify(`No running background task ${command.id}.`);
          return;
        }
        this.host?.stopJob(command.id as WorkId);
        return;
      }
      case "start":
        this.start(command.brief);
    }
  }

  private start(brief: string): void {
    try {
      this.host ??= this.startHost((event) => this.onEvent(event));
    } catch (error) {
      this.sink.notify(`Could not start background tasks: ${error instanceof Error ? error.message : String(error)}`);
      return;
    }
    const id = `bg-${++this.counter}`;
    this.jobs.set(id, { id, brief, status: "running", note: "queued" });
    this.host.spawnJob(id as WorkId, brief, "off");
    this.sink.notify(`Started ${id}: ${brief}`);
  }

  describe(): string {
    if (this.jobs.size === 0) return "No background tasks yet. Start one with /bg <task>.";
    return [...this.jobs.values()]
      .map((job) => `${job.id}  ${job.status}  ${job.brief}${job.status === "running" ? `  (${job.note})` : ""}`)
      .join("\n");
  }

  onEvent(event: VoiceEvent): void {
    switch (event.tag) {
      case "workerJob": {
        const job = this.jobs.get(event.id);
        if (!job) return;
        if (event.status === "done") job.status = "done";
        else if (event.status === "stopped") job.status = "stopped";
        else if (event.status === "failed") job.status = "failed";
        if (event.note) job.note = event.note;
        if (job.status === "stopped") this.sink.notify(`${job.id} stopped.`);
        if (job.status === "failed") this.sink.notify(`${job.id} failed${event.note ? `: ${event.note}` : "."}`);
        return;
      }
      case "jobProgress":
        // Progress carries no job id on this path, so it only updates the one running task.
        for (const job of this.jobs.values()) if (job.status === "running") job.note = event.note;
        return;
      case "workerResult": {
        const job = this.jobs.get(event.id);
        if (!job) return;
        job.status = "done";
        this.sink.notify(`${job.id} finished.`);
        this.sink.deliver(job.id, event.full || event.speak);
        return;
      }
      case "workerError":
        this.host = undefined;
        for (const job of this.jobs.values()) {
          if (job.status === "running") job.status = "failed";
        }
        this.sink.notify(`Background tasks stopped: ${event.message}`);
        return;
    }
  }

  shutdown(): void {
    this.host?.quit();
    this.host = undefined;
  }
}

export default function (pi: ExtensionAPI): void {
  let current: Background | undefined;
  pi.registerCommand("bg", {
    description: "Run coding tasks in the background: /bg <task>, /bg list, /bg stop <id>",
    handler: async (args, ctx) => {
      current ??= new Background(
        {
          notify: (text) => ctx.ui.notify(text, "info"),
          deliver: (id, text) =>
            pi.sendMessage(
              { customType: BG_TYPE, content: `Background task ${id} finished:\n\n${text}`, display: true, details: { id } },
              { deliverAs: "followUp" },
            ),
        },
        (onEvent) =>
          WorkerHost.start(onEvent, () => {
            // Host log lines are for the log files, not the chat.
          }),
      );
      current.run(parseBg(args));
    },
  });
  pi.on("session_shutdown", () => current?.shutdown());
}
