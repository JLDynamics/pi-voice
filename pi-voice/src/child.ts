import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { createWriteStream, existsSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ChildPid, ShortResult, UserText, VoiceEvent, WorkId } from "./voice.ts";

export type VoiceLease = {
  ownerPid: number;
  voicePid: number;
  executable: string;
};

type HeadlessIn =
  | { type: "quit" }
  | { type: "mute"; muted: boolean }
  | { type: "interrupt" }
  | { type: "user"; text: string }
  | { type: "result"; id: string; speak: string; full: string }
  | { type: "job_update"; id: string; status: JobUpdateStatus; note?: string };

export type JobUpdateStatus = "queued" | "working" | "done" | "stopped" | "superseded" | "dropped" | "failed";

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), "../..");

export function voiceBin(): string {
  return (
    process.env.VOICE_BIN ||
    join(REPO_ROOT, "macos/Voice/build/Voice.app/Contents/MacOS/Voice")
  );
}

export function leasePath(): string {
  return join(process.env.TMPDIR || tmpdir(), "pi-voice.lease.json");
}

export function stderrLogPath(pid: ChildPid): string {
  return join(process.env.TMPDIR || tmpdir(), `pi-voice.${pid}.stderr.log`);
}

export function headlessChildEnv(base: NodeJS.ProcessEnv = process.env, history: VoiceHistoryTurn[] = []): NodeJS.ProcessEnv {
  return { ...base, VOICE_THINKER: "luna", VOICE_HISTORY: JSON.stringify(history) };
}

export type VoiceHistoryTurn = { role: "user" | "assistant"; text: string };

export class VoiceChild {
  readonly pid: ChildPid;
  private readonly proc: ChildProcess;

  private constructor(pid: ChildPid, proc: ChildProcess) {
    this.pid = pid;
    this.proc = proc;
  }

  static spawn(onEvent: (event: VoiceEvent) => void, history: VoiceHistoryTurn[] = []): VoiceChild {
    const bin = voiceBin();
    if (!existsSync(bin)) {
      throw new Error(`Voice binary missing. Build it with ${join(REPO_ROOT, "macos/Voice/scripts/build.sh")}`);
    }
    const proc = spawn(bin, ["--headless"], {
      stdio: ["pipe", "pipe", "pipe"],
      env: headlessChildEnv(process.env, history),
    });
    if (proc.pid == null) throw new Error("Voice spawn returned no pid");
    const pid = proc.pid as ChildPid;
    const child = new VoiceChild(pid, proc);
    writeLease({ ownerPid: process.pid, voicePid: proc.pid, executable: bin });
    const log = createWriteStream(stderrLogPath(pid), { flags: "a" });
    proc.stderr?.pipe(log);
    let leftover = "";
    proc.stdout?.setEncoding("utf8");
    proc.stdout?.on("data", (chunk: string) => {
      leftover += chunk;
      let nl = leftover.indexOf("\n");
      while (nl >= 0) {
        const line = leftover.slice(0, nl);
        leftover = leftover.slice(nl + 1);
        const event = parseLine(line);
        if (event) onEvent(event);
        nl = leftover.indexOf("\n");
      }
    });
    proc.on("error", (err) => onEvent({ tag: "error", message: err.message }));
    proc.on("exit", (code) => {
      log.end();
      clearOwnedLease();
      onEvent({ tag: "childExit", code });
    });
    return child;
  }

  setMuted(closed: boolean): void {
    this.send({ type: "mute", muted: closed });
  }

  interrupt(): void {
    this.send({ type: "interrupt" });
  }

  ingestUser(text: UserText): void {
    const trimmed = String(text).trim();
    if (!trimmed) return;
    this.send({ type: "user", text: trimmed });
  }

  postResult(id: WorkId, speak: ShortResult, full: string): void {
    this.send({ type: "result", id, speak, full });
  }

  sendJobUpdate(id: WorkId, status: JobUpdateStatus, note?: string): void {
    if (note) this.send({ type: "job_update", id, status, note });
    else this.send({ type: "job_update", id, status });
  }

  quit(): void {
    this.send({ type: "quit" });
    const proc = this.proc;
    setTimeout(() => {
      if (proc.exitCode == null && proc.signalCode == null) proc.kill("SIGTERM");
    }, 2000).unref?.();
    setTimeout(() => {
      if (proc.exitCode == null && proc.signalCode == null) proc.kill("SIGKILL");
    }, 4000).unref?.();
  }

  kill(): void {
    try {
      this.proc.kill("SIGKILL");
    } catch {
      // already gone
    }
  }

  private send(msg: HeadlessIn): void {
    const stdin = this.proc.stdin;
    if (!stdin || stdin.destroyed) return;
    stdin.write(`${JSON.stringify(msg)}\n`);
  }
}

export function parseLine(raw: string): VoiceEvent | undefined {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return;
  }
  if (parsed == null || typeof parsed !== "object" || Array.isArray(parsed)) return;
  const object = parsed as { type?: unknown; message?: unknown; text?: unknown; id?: unknown; brief?: unknown; item_id?: unknown };
  if (object.type === "ready") return { tag: "ready" };
  if (object.type === "error") {
    return {
      tag: "error",
      message: typeof object.message === "string" && object.message ? object.message : "Voice failed",
    };
  }
  if (object.type === "speech_started") return { tag: "speechStarted" };
  if (object.type === "heard") {
    const text = typeof object.text === "string" ? object.text.trim() : "";
    if (!text) return;
    // The server item id is stable across the revisions of one utterance, and
    // is the only reliable way to tell "the same sentence again, re-transcribed"
    // from "a new sentence". The text itself cannot: STT rewrites words it
    // already returned, so a later revision is often not an extension of the
    // earlier one ("that's good. Yeah, it's just uh" -> "that's good, you know,
    // yeah. Okay, so").
    const itemId = typeof object.item_id === "string" ? object.item_id : "";
    return { tag: "heard", text: text as UserText, itemId };
  }
  if (object.type === "spoken") {
    const text = typeof object.text === "string" ? object.text.trim() : "";
    if (!text) return;
    return { tag: "spoken", text, itemId: typeof object.item_id === "string" ? object.item_id : "" };
  }
  if (object.type === "spoken_delta") {
    const text = typeof object.text === "string" ? object.text : "";
    if (!text) return;
    return { tag: "spokenDelta", text, itemId: typeof object.item_id === "string" ? object.item_id : "" };
  }
  if (object.type === "stop_work") return { tag: "stopWork" };
  if (object.type === "work") {
    const id = typeof object.id === "string" ? object.id.trim() : "";
    const brief = typeof object.brief === "string" ? object.brief.trim() : "";
    if (!id || !brief) return;
    return { tag: "work", id: id as WorkId, brief: brief as UserText };
  }
}

export function liveForeignOwner(): string | undefined {
  const lease = readLease();
  if (!lease) return;
  if (lease.ownerPid !== process.pid && pidAlive(lease.ownerPid)) {
    return "Voice is already running in another Pi window";
  }
}

export async function reapOrphans(): Promise<string | undefined> {
  const blocked = liveForeignOwner();
  if (blocked) return blocked;
  const lease = readLease();
  if (!lease) return;
  if (pidAlive(lease.voicePid) && isHeadlessVoice(lease.voicePid, lease.executable)) {
    killPid(lease.voicePid, "SIGTERM");
    await sleep(200);
    if (pidAlive(lease.voicePid)) killPid(lease.voicePid, "SIGKILL");
  }
  clearLease();
}

export function clearOwnedLease(): void {
  const lease = readLease();
  if (!lease || lease.ownerPid === process.pid) clearLease();
}

function isHeadlessVoice(pid: number, executable: string): boolean {
  const args = processArgs(pid);
  if (!args) return false;
  if (!args.includes("--headless")) return false;
  const exe = executable.trim();
  if (!exe) return false;
  return args.includes(exe) || args.includes(basename(exe));
}

function processArgs(pid: number): string | undefined {
  try {
    const out = execFileSync("ps", ["-p", String(pid), "-ww", "-o", "args="], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
    return out || undefined;
  } catch {
    return;
  }
}

function pidAlive(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return (err as NodeJS.ErrnoException).code === "EPERM";
  }
}

function killPid(pid: number, signal: NodeJS.Signals): void {
  try {
    process.kill(pid, signal);
  } catch {
    // already gone
  }
}

function readLease(): VoiceLease | undefined {
  try {
    const parsed = JSON.parse(readFileSync(leasePath(), "utf8")) as Partial<VoiceLease>;
    if (
      typeof parsed.ownerPid === "number" &&
      typeof parsed.voicePid === "number" &&
      typeof parsed.executable === "string"
    ) {
      return { ownerPid: parsed.ownerPid, voicePid: parsed.voicePid, executable: parsed.executable };
    }
  } catch {
    return;
  }
}

function writeLease(lease: VoiceLease): void {
  writeFileSync(leasePath(), `${JSON.stringify(lease)}\n`);
}

function clearLease(): void {
  try {
    unlinkSync(leasePath());
  } catch {
    // missing is the desired end state
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
