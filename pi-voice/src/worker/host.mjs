#!/usr/bin/env node
/**
 * Durable worker host for pi-voice.
 *
 * One process, many jobs. Each `spawn` creates a `pi-durable` conversation with
 * its own model, thinking level and tool loop, so a second job no longer has to
 * destroy the first — the thing Codex gets from `create_thread`.
 *
 * The protocol is NDJSON on stdin/stdout, deliberately close to the extension's
 * existing vocabulary (`spawn`/`stop`/`status`, `progress`/`job`/`result`) so the
 * reducer and the Swift mirror keep their meaning.
 *
 *   in   {op:"spawn", id, brief, effort}   start (or steer) a job
 *        {op:"stop", id}                   abort one job
 *        {op:"status", id?, request?}      read state without touching it
 *        {op:"list"}                       every live job
 *        {op:"quit"}
 *   out  {ev:"ready"}
 *        {ev:"progress", id, tool, phase, summary, active}
 *        {ev:"job", id, status, note?}
 *        {ev:"result", id, speak, full}
 *        {ev:"status", id, payload}
 *        {ev:"error", message}
 *
 * Tools mirror what the voice prompt already tells Pi to do (bash, read, write,
 * edit, grep, plus screen capture through bash), so a worker is interchangeable
 * with the single-session path for every brief the voice model can send.
 */
import { execFile } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, unlinkSync } from "node:fs";
import { readFile, readdir, writeFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { promisify } from "node:util";
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import {
  findSkills,
  MAX_CONCURRENT_RESUMES,
  MAX_RESUMES,
  readJobs,
  resumeTarget,
  workerStateRoot,
  writeJobs,
  hasCredential,
  instructionsText,
  loadCredentials,
  resolveModel,
  sandboxMode,
  sandboxProfile,
  shellEnv,
  writablePath,
  workspaceRoots,
  writeRefusal,
} from "./config.mjs";
import { Type } from "typebox";
import {
  AssistantEntry,
  createRegistry,
  defineExtension,
  defineTool,
  section,
  Harness,
  MemoryStorage,
} from "@earendil-works/pi-durable";
import { openNodeJsonlStorage } from "@earendil-works/pi-durable/storage/jsonl/node";

const exec = promisify(execFile);
const context = BACKGROUND_CONTEXT;

/** Characters of a finished answer the voice model gets as its spoken excerpt. */
const RESULT_MAX = 1500;
/** Answer head handed to `status` while a job is still running. */
const SNAPSHOT_HEAD = 900;

const emit = (obj) => process.stdout.write(`${JSON.stringify(obj)}\n`);
const note = (line) => process.stderr.write(`${new Date().toISOString()} [worker] ${line}\n`);

/** Per-job bookkeeping the protocol needs; the durable state is the transcript. */
const jobs = new Map();

// ── write policy ─────────────────────────────────────────────────────────────
// No prompts: a background worker has nobody to ask. Limits instead, and a
// refusal the voice model can speak.
const SANDBOX = sandboxMode();
const WRITE_ROOTS = workspaceRoots();
const SANDBOX_BIN = "/usr/bin/sandbox-exec";

/** Whether a tool may write `target`, and the refusal when it may not. */
function allowWrite(api, target) {
  if (SANDBOX === "unrestricted") return undefined;
  if (SANDBOX === "read-only") {
    toolProgress(api.conversationId, "write", "error", target);
    return `Refused: this worker is read-only and cannot write ${target}. Say that plainly.`;
  }
  if (writablePath(target, WRITE_ROOTS)) return undefined;
  toolProgress(api.conversationId, "write", "error", target);
  return writeRefusal(target, WRITE_ROOTS);
}

// ── tools ────────────────────────────────────────────────────────────────────
// Every tool reports progress through the job it belongs to, the same shape the
// extension's tool_execution_* hooks produce.
function toolProgress(conversationId, tool, phase, summary) {
  const job = [...jobs.values()].find((j) => j.conversationId === conversationId);
  if (!job) return;
  if (phase === "start") job.active.add(tool);
  if (phase === "end" || phase === "error") job.active.delete(tool);
  job.lastNote = describe(tool, phase, summary);
  emit({ ev: "progress", id: job.id, tool, phase, summary, active: [...job.active].join(",") });
  emit({ ev: "job", id: job.id, status: "working", note: job.lastNote });
}

function describe(tool, phase, summary) {
  const label = tool === "web_search" || tool === "source_check" ? "searching the web"
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

function head(text, max) {
  const flat = String(text).replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}

function summaryOf(name, args) {
  const pick = (...keys) => {
    for (const k of keys) if (typeof args?.[k] === "string" && args[k].trim()) return args[k].trim();
    return "";
  };
  const raw = name === "read" || name === "write" || name === "edit" ? pick("path")
    : name === "grep" ? pick("pattern")
    : name === "bash" ? pick("command")
    : "";
  const flat = raw.replace(/\s+/g, " ");
  return flat.length > 60 ? `${flat.slice(0, 59)}…` : flat;
}

const bashTool = defineTool({
  name: "bash",
  description:
    "Run a shell command in the project folder and return its output. Use it for files, git, "
    + "research (curl), and screen capture (screencapture -x -l <window id> to a temp png, then read that png).",
  parameters: Type.Object({
    command: Type.String({ description: "The command to run." }),
    timeout: Type.Optional(Type.Number({ description: "Seconds. Default 20, max 120." })),
  }),
  replay: "unsafe",
  execute: async (args, api) => {
    toolProgress(api.conversationId, "bash", "start", summaryOf("bash", args));
    try {
      const seconds = Math.min(120, Math.max(1, args.timeout ?? 20));
      // A shell can write anywhere, so the policy has to be enforced by the OS
      // here, not by inspecting the command string. `sandbox-exec` is the same
      // mechanism the Python research tool already relies on.
      const sandboxed = SANDBOX !== "unrestricted" && existsSync(SANDBOX_BIN);
      const bin = sandboxed ? SANDBOX_BIN : "/bin/sh";
      const argv = sandboxed
        ? ["-p", sandboxProfile(SANDBOX === "read-only" ? [] : WRITE_ROOTS), "/bin/sh", "-c", args.command]
        : ["-c", args.command];
      if (!sandboxed && SANDBOX !== "unrestricted") {
        note(`warning: ${SANDBOX_BIN} missing; bash writes are not confined to the workspace`);
      }
      const { stdout, stderr } = await exec(bin, argv, {
        timeout: seconds * 1000,
        maxBuffer: 4 * 1024 * 1024,
        cwd: process.cwd(),
        env: shellEnv(),
      });
      toolProgress(api.conversationId, "bash", "end", summaryOf("bash", args));
      return { output: [{ type: "text", text: `${stdout}${stderr}`.trim() || "(no output)" }] };
    } catch (error) {
      toolProgress(api.conversationId, "bash", "error", summaryOf("bash", args));
      return {
        output: [{ type: "text", text: `command failed: ${error.message}` }],
        isError: true,
      };
    }
  },
});

const readTool = defineTool({
  name: "read",
  description: "Read a text file and return its contents.",
  parameters: Type.Object({ path: Type.String({ description: "Absolute path or path relative to the project folder." }) }),
  execute: async (args, api) => {
    toolProgress(api.conversationId, "read", "start", summaryOf("read", args));
    try {
      const text = await readFile(args.path, "utf8");
      toolProgress(api.conversationId, "read", "end", summaryOf("read", args));
      return { output: [{ type: "text", text: text.length > 60_000 ? `${text.slice(0, 60_000)}\n…(truncated)` : text }] };
    } catch (error) {
      toolProgress(api.conversationId, "read", "error", summaryOf("read", args));
      return { output: [{ type: "text", text: `could not read ${args.path}: ${error.message}` }], isError: true };
    }
  },
});

const writeTool = defineTool({
  name: "write",
  description: "Write a file, replacing it if it exists.",
  parameters: Type.Object({
    path: Type.String({ description: "File to write." }),
    content: Type.String({ description: "Full new contents." }),
  }),
  replay: "unsafe",
  execute: async (args, api) => {
    const refusal = allowWrite(api, args.path);
    if (refusal) return { output: [{ type: "text", text: refusal }], isError: true };
    toolProgress(api.conversationId, "write", "start", summaryOf("write", args));
    try {
      await writeFile(args.path, args.content, "utf8");
      toolProgress(api.conversationId, "write", "end", summaryOf("write", args));
      return { output: [{ type: "text", text: `wrote ${args.content.length} characters to ${args.path}` }] };
    } catch (error) {
      toolProgress(api.conversationId, "write", "error", summaryOf("write", args));
      return { output: [{ type: "text", text: `could not write ${args.path}: ${error.message}` }], isError: true };
    }
  },
});

const editTool = defineTool({
  name: "edit",
  description: "Replace one exact string in a file with another. The old string must appear exactly once.",
  parameters: Type.Object({
    path: Type.String({ description: "File to edit." }),
    old: Type.String({ description: "Exact text to replace." }),
    new: Type.String({ description: "Replacement text." }),
  }),
  replay: "unsafe",
  execute: async (args, api) => {
    const refusal = allowWrite(api, args.path);
    if (refusal) return { output: [{ type: "text", text: refusal }], isError: true };
    toolProgress(api.conversationId, "edit", "start", summaryOf("edit", args));
    try {
      const before = await readFile(args.path, "utf8");
      const at = before.indexOf(args.old);
      if (at < 0) {
        toolProgress(api.conversationId, "edit", "error", summaryOf("edit", args));
        return { output: [{ type: "text", text: "that text does not appear in the file" }], isError: true };
      }
      if (before.indexOf(args.old, at + 1) >= 0) {
        toolProgress(api.conversationId, "edit", "error", summaryOf("edit", args));
        return { output: [{ type: "text", text: "that text appears more than once; include more context" }], isError: true };
      }
      await writeFile(args.path, before.slice(0, at) + args.new + before.slice(at + args.old.length), "utf8");
      toolProgress(api.conversationId, "edit", "end", summaryOf("edit", args));
      return { output: [{ type: "text", text: `edited ${args.path}` }] };
    } catch (error) {
      toolProgress(api.conversationId, "edit", "error", summaryOf("edit", args));
      return { output: [{ type: "text", text: `could not edit ${args.path}: ${error.message}` }], isError: true };
    }
  },
});

const grepTool = defineTool({
  name: "grep",
  description: "Search a folder for a pattern and return matching lines with file and line number.",
  parameters: Type.Object({
    pattern: Type.String({ description: "Regular expression." }),
    path: Type.Optional(Type.String({ description: "Folder to search. Default the project folder." })),
  }),
  execute: async (args, api) => {
    toolProgress(api.conversationId, "grep", "start", summaryOf("grep", args));
    try {
      const { stdout } = await exec("rg", ["-n", "--no-heading", "-m", "200", args.pattern, args.path ?? "."], {
        timeout: 20_000,
        maxBuffer: 2 * 1024 * 1024,
        cwd: process.cwd(),
      });
      toolProgress(api.conversationId, "grep", "end", summaryOf("grep", args));
      return { output: [{ type: "text", text: stdout.trim() || "no matches" }] };
    } catch (error) {
      // rg exits 1 for "no matches", which is not an error worth reporting.
      const empty = error.code === 1 || error.code === "1";
      toolProgress(api.conversationId, "grep", empty ? "end" : "error", summaryOf("grep", args));
      return empty
        ? { output: [{ type: "text", text: "no matches" }] }
        : { output: [{ type: "text", text: `grep failed: ${error.message}` }], isError: true };
    }
  },
});


// ── ls / find: the rest of Pi's built-in set ─────────────────────────────────
// Same names and arguments as Pi's own tools, so a brief written for the session
// works unchanged in a worker.
const lsTool = defineTool({
  name: "ls",
  description: "List a folder. Returns names with a trailing slash for folders.",
  parameters: Type.Object({
    path: Type.Optional(Type.String({ description: "Folder to list. Default the project folder." })),
  }),
  execute: async (args, api) => {
    toolProgress(api.conversationId, "ls", "start", args.path ?? ".");
    try {
      const dir = args.path ?? ".";
      const entries = await readdir(dir, { withFileTypes: true });
      toolProgress(api.conversationId, "ls", "end", dir);
      const lines = entries
        .sort((a, b) => Number(b.isDirectory()) - Number(a.isDirectory()) || a.name.localeCompare(b.name))
        .map((e) => (e.isDirectory() ? `${e.name}/` : e.name));
      return { output: [{ type: "text", text: lines.join("\n") || "(empty folder)" }] };
    } catch (error) {
      toolProgress(api.conversationId, "ls", "error", args.path ?? ".");
      return { output: [{ type: "text", text: `could not list ${args.path ?? "."}: ${error.message}` }], isError: true };
    }
  },
});

const findTool = defineTool({
  name: "find",
  description: "Find files by glob pattern, newest first. Skips node_modules and .git.",
  parameters: Type.Object({
    pattern: Type.String({ description: 'Glob, e.g. "*.swift" or "**/*.test.ts".' }),
    path: Type.Optional(Type.String({ description: "Folder to search. Default the project folder." })),
  }),
  execute: async (args, api) => {
    toolProgress(api.conversationId, "find", "start", args.pattern);
    try {
      const { stdout } = await exec(
        "rg",
        ["--files", "--glob", args.pattern, "--glob", "!node_modules", "--glob", "!.git", args.path ?? "."],
        { timeout: 20_000, maxBuffer: 2 * 1024 * 1024, cwd: process.cwd() },
      );
      const files = stdout.trim().split("\n").filter(Boolean).slice(0, 400);
      toolProgress(api.conversationId, "find", "end", args.pattern);
      return { output: [{ type: "text", text: files.join("\n") || "no files match" }] };
    } catch (error) {
      const empty = error.code === 1 || error.code === "1";
      toolProgress(api.conversationId, "find", empty ? "end" : "error", args.pattern);
      return empty
        ? { output: [{ type: "text", text: "no files match" }] }
        : { output: [{ type: "text", text: `find failed: ${error.message}` }], isError: true };
    }
  },
});

// ── skills ───────────────────────────────────────────────────────────────────
// Pi's skills are Markdown instructions plus shell. Loading one here means a
// worker can follow the same procedure the session would.
/** Cap on a loaded SKILL.md, so one huge skill cannot crowd out the job. */
const SKILL_MAX = 40_000;

const skillTool = defineTool({
  name: "skill",
  description:
    "List the installed skills, or load one by name to follow its instructions. "
    + "Call with no name to list them; call with a name before doing a task that matches one.",
  parameters: Type.Object({
    name: Type.Optional(Type.String({ description: "Skill to load, e.g. \"leanrea\". Omit to list." })),
  }),
  execute: async (args, api) => {
    const skills = await findSkills();
    if (!args.name) {
      const list = skills.map((s) => `- ${s.name}`).join("\n");
      return { output: [{ type: "text", text: `Skills available:\n${list || "(none installed)"}` }] };
    }
    const wanted = args.name.trim().toLowerCase();
    const skill = skills.find((s) => s.name.toLowerCase() === wanted);
    if (!skill) {
      return {
        output: [{ type: "text", text: `No skill named ${args.name}. Available: ${skills.map((s) => s.name).join(", ")}` }],
        isError: true,
      };
    }
    toolProgress(api.conversationId, "skill", "start", skill.name);
    try {
      const text = await readFile(skill.file, "utf8");
      toolProgress(api.conversationId, "skill", "end", skill.name);
      const body = text.length > SKILL_MAX ? `${text.slice(0, SKILL_MAX)}\n…(truncated)` : text;
      return {
        output: [{
          type: "text",
          text: `Instructions for the ${skill.name} skill (${skill.file}). Follow them. Paths in it are relative to ${dirname(skill.file)}.\n\n${body}`,
        }],
      };
    } catch (error) {
      toolProgress(api.conversationId, "skill", "error", skill.name);
      return { output: [{ type: "text", text: `could not load ${skill.name}: ${error.message}` }], isError: true };
    }
  },
});

// ── screen ───────────────────────────────────────────────────────────────────
// The one thing voice users ask for that no text tool covers. Pi does this by
// running screencapture itself; the worker gets it as a real tool because it
// cannot see the incantation in the session's prompt.
const screenshotTool = defineTool({
  name: "screenshot",
  description:
    "Look at what the user is looking at: capture the frontmost window and return it as an image. "
    + "Use it whenever the request refers to the screen, this window, or a visible app. "
    + "Pass frontmost=false for the whole screen.",
  parameters: Type.Object({
    frontmost: Type.Optional(Type.Boolean({ description: "Capture only the frontmost window. Default true." })),
  }),
  replay: "safe",
  execute: async (args, api) => {
    toolProgress(api.conversationId, "screenshot", "start", "");
    const out = join(tmpdir(), `pi-voice-shot-${Date.now()}-${process.pid}.png`);
    try {
      const wholeScreen = args.frontmost === false;
      let captured = false;
      if (!wholeScreen) {
        // The frontmost on-screen layer-0 window id (CGWindowID, not a System
        // Events window id), the same rule Pi's session follows.
        const { stdout } = await exec(
          "osascript",
          ["-l", "JavaScript", "-e",
            'ObjC.import("CoreGraphics"); let w = ObjC.deepUnwrap(ObjC.castRefToObject($.CGWindowListCopyWindowInfo(1, 0))); ' +
            'let m = w.filter(x => x.kCGWindowLayer === 0 && x.kCGWindowAlpha > 0 && x.kCGWindowIsOnscreen).map(x => x.kCGWindowNumber); m.length ? String(m[0]) : ""'],
          { timeout: 10_000 },
        );
        const id = stdout.trim().split("\n").pop() ?? "";
        if (/^\d+$/.test(id)) {
          await exec("screencapture", ["-x", "-l", id, out], { timeout: 15_000 });
          captured = existsSync(out);
        }
      }
      if (!captured) {
        await exec("screencapture", ["-x", out], { timeout: 15_000 });
      }
      if (!existsSync(out)) throw new Error("screencapture produced no file");
      const data = readFileSync(out).toString("base64");
      toolProgress(api.conversationId, "screenshot", "end", "");
      return {
        output: [
          { type: "text", text: `Screenshot of ${wholeScreen || !captured ? "the screen" : "the frontmost window"}.` },
          { type: "image", data, mimeType: "image/png" },
        ],
      };
    } catch (error) {
      toolProgress(api.conversationId, "screenshot", "error", "");
      return {
        output: [{
          type: "text",
          text: `could not capture the screen: ${error.message}. The terminal that launched Pi may need Screen Recording permission. Do not guess what is on screen.`,
        }],
        isError: true,
      };
    } finally {
      try {
        if (existsSync(out)) unlinkSync(out);
      } catch {
        // leaving a temp file is harmless
      }
    }
  },
});

// Project rules go in a prompt section, so every job in this host sees them.
const instructionsSection = section("project_instructions", () => instructionsText());

const extension = defineExtension({
  name: "pi-voice-worker",
  tools: [bashTool, readTool, writeTool, editTool, grepTool, lsTool, findTool, skillTool, screenshotTool],
  sections: [instructionsSection],
});

// ── harness ──────────────────────────────────────────────────────────────────
const credentials = loadCredentials();
note(`credentials: ${credentials.loaded} value(s) from ${credentials.file}`);
const resolvedModel = resolveModel();
const models = resolvedModel.models;

const registry = createRegistry();
registry.install(extension);

// One directory per host run, so two calls never interleave their conversations
// and a finished job stays readable afterwards. A run that still has unfinished
// jobs is reopened instead, which is how a crashed host recovers its work.
const resumeDir = resumeTarget();
const stateRoot = resumeDir ?? join(workerStateRoot(), `run-${new Date().toISOString().replace(/[:.]/g, "-")}-${process.pid}`);
let storage = new MemoryStorage();
let storageKind = `memory (${stateRoot} could not be used)`;
try {
  mkdirSync(stateRoot, { recursive: true, mode: 0o700 });
  storage = await openNodeJsonlStorage(stateRoot, context, { fsync: false });
  storageKind = stateRoot;
} catch (error) {
  note(`could not open worker state at ${stateRoot}: ${error?.message ?? error}; keeping it in memory only`);
}
note(`worker state: ${storageKind}`);

const harness = await Harness.open(storage, { models, registry }, context);

/**
 * The saved record of every job in this run.
 *
 * The conversations alone are not enough: `pi-durable` knows a conversation has an
 * unsettled submission, but not which voice job id it belongs to, and the voice
 * model addresses jobs by id. Keeping the mapping on disk is what lets a restart
 * say "job X resumed" instead of silently adopting an anonymous conversation.
 */
const jobRecords = readJobs(stateRoot);
const saveJobs = () => writeJobs(stateRoot, jobRecords);
const recordJob = (job) => {
  const at = jobRecords.findIndex((r) => r.id === job.id);
  const entry = {
    id: job.id,
    conversationId: job.conversationId,
    brief: job.brief,
    effort: job.effort ?? "off",
    startedAt: job.startedAt,
    finished: false,
    resumes: job.resumes ?? 0,
  };
  if (at >= 0) jobRecords[at] = { ...jobRecords[at], ...entry };
  else jobRecords.push(entry);
  saveJobs();
};
const finishJob = (id, outcome) => {
  const at = jobRecords.findIndex((r) => r.id === id);
  if (at >= 0) {
    jobRecords[at] = { ...jobRecords[at], finished: true, outcome };
    saveJobs();
  }
};
const model = resolvedModel.model;
if (!hasCredential(model.provider)) {
  const where = credentials.file;
  const message =
    `No credential for ${model.provider}. Set it in ${where} (or the environment) `
    + `and start voice again. The worker cannot run without it.`;
  note(message);
  emit({ ev: "error", message });
  process.exit(2);
}
const root = await harness.root(context, { agent: { model } });
emit({ ev: "ready", model: `${model.provider}/${model.modelId}`, state: storageKind, resumed: Boolean(resumeDir) });
note(`ready with ${model.provider}/${model.modelId} (${resolvedModel.source})`);
if (resumeDir) note(`reopened ${resumeDir}`);
void resumeUnfinished().catch((error) => note(`resume failed: ${error?.stack ?? error}`));

/** Send one job to a fresh conversation and stream its life back. */
async function spawn(id, brief, effort) {
  if (jobs.has(id)) return steer(id, brief);
  const conversation = await root.commit(
    (tx) => tx.createConversation({ ownership: { kind: "ownerless" } }),
    context,
  );
  const handle = await harness.conversation(conversation.id, context);
  const level = ["off", "minimal", "low", "medium", "high"].includes(effort) ? effort : "off";
  await handle.configure({ model, thinkingLevel: level }, context);

  const job = {
    id,
    conversationId: conversation.id,
    brief,
    effort: level,
    startedAt: Date.now(),
    active: new Set(),
    lastNote: null,
    settled: false,
    submission: null,
  };
  jobs.set(id, job);
  recordJob(job);
  emit({ ev: "job", id, status: "queued", note: `effort ${level}` });

  const submission = await handle.submit(
    {
      type: "input",
      content:
        `${brief}\n\n`
        + "Work in the project folder. Use the tools you have. "
        + "Lead with the outcome in a sentence or two, then the detail. "
        + "The answer is read aloud by a voice assistant, so do not paste long paths or code.",
    },
    context,
  );
  job.submission = submission;
  emit({ ev: "job", id, status: "working" });

  await settle(job, handle, submission);
}

/**
 * Wait for one submission and report its outcome. Shared by a fresh spawn and by
 * a resume, so a recovered job follows exactly the same path as a new one.
 */
async function settle(job, handle, submission) {
  const { id } = job;
  let settled;
  try {
    settled = await submission.wait(context);
  } catch (error) {
    // A resumed submission can fail to reattach (its run is gone). Report it
    // rather than leaving the voice model waiting on a job that cannot finish.
    const message = String(error?.message ?? error);
    note(`job ${id} lost its run: ${message}`);
    emit({ ev: "job", id, status: "failed", note: `worker lost the job: ${message}` });
    finishJob(id, "failed");
    jobs.delete(id);
    return;
  }
  job.settled = true;
  if (settled.status !== "done") {
    // `reason` is the only useful part: "no_model", "no_credential", a provider
    // error. A bare "unanswered" is undiagnosable, which cost a debugging round.
    const reason = [settled.reason, settled.detail].filter(Boolean).join(": ");
    note(`job ${id} ${settled.status}${reason}`);
    emit({ ev: "job", id, status: "failed", note: `worker ${settled.status}${reason}` });
    finishJob(id, "failed");
    jobs.delete(id);
    return;
  }
  const entry = await handle.commit((tx) => tx.entry(AssistantEntry, settled.answer), context);
  const full = (entry?.model?.[0]?.content ?? [])
    .filter((part) => part.type === "text")
    .map((part) => part.text)
    .join("")
    .trim();
  if (!full) {
    emit({ ev: "job", id, status: "failed", note: "worker finished with no answer" });
    finishJob(id, "failed");
    jobs.delete(id);
    return;
  }
  job.answer = full;
  emit({ ev: "job", id, status: "done" });
  emit({ ev: "result", id, speak: head(full, RESULT_MAX), full });
  finishJob(id, "done");
  note(`job ${id} done (${full.length} chars)`);
}

/** A second brief for a running job: steer the same conversation. */
async function steer(id, brief) {
  const job = jobs.get(id);
  if (!job) return spawn(id, brief, "off");
  const handle = await harness.conversation(job.conversationId, context);
  job.brief = brief;
  emit({ ev: "job", id, status: "working", note: "steered" });
  await handle.submit({ type: "input", content: brief, whenBusy: "steer" }, context);
}

async function stop(id) {
  const job = jobs.get(id);
  if (!job) {
    emit({ ev: "job", id, status: "stopped", note: "nothing running" });
    return;
  }
  const handle = await harness.conversation(job.conversationId, context);
  await handle.abort(context);
  jobs.delete(id);
  emit({ ev: "job", id, status: "stopped" });
}

/** Read-only: the job's phase, activity, and the answer so far. */
async function status(id, request) {
  const job = id ? jobs.get(id) : [...jobs.values()].sort((a, b) => b.startedAt - a.startedAt)[0];
  if (!job) {
    emit({ ev: "status", id: id ?? "", payload: JSON.stringify({ request, status: "idle", note: "Nothing has been handed to the worker." }) });
    return;
  }
  const payload = {
    request: request ?? "",
    status: job.settled ? "settled" : "working",
    id: job.id,
    brief: head(job.brief, 140),
    elapsed_s: Math.max(0, Math.round((Date.now() - job.startedAt) / 1000)),
  };
  if (job.lastNote) payload.doing = job.lastNote;
  if (job.active.size > 0) payload.running = [...job.active];
  if (job.answer) {
    payload.answer = head(job.answer, SNAPSHOT_HEAD);
    payload.answer_chars = job.answer.length;
  } else {
    payload.note = "The worker has not written an answer yet. Say briefly what it is doing; do not invent findings. Do not call stop_thinking for a status question.";
  }
  emit({ ev: "status", id: job.id, payload: JSON.stringify(payload) });
}

function list() {
  emit({
    ev: "list",
    jobs: [...jobs.values()].map((j) => ({
      id: j.id,
      brief: head(j.brief, 80),
      status: j.settled ? "settled" : "working",
      elapsed_s: Math.max(0, Math.round((Date.now() - j.startedAt) / 1000)),
      active: [...j.active],
    })),
  });
}

// ── resume ───────────────────────────────────────────────────────────────────
/**
 * Pick up jobs a previous host left behind.
 *
 * A crash mid-job used to lose the work silently while the voice call kept
 * talking, so `check_thinking` reported a job that no longer existed. Reopening
 * the storage is enough for `pi-durable` to continue the run; this re-registers
 * the job under its voice id so status, stop and the result all still work.
 *
 * Bounded like Codex: at most MAX_CONCURRENT_RESUMES at once, and a job that has
 * already been resumed MAX_RESUMES times is declared dead instead of retried.
 */
async function resumeUnfinished() {
  if (!resumeDir) return;
  const unfinished = jobRecords.filter((r) => !r.finished);
  if (unfinished.length === 0) return;
  harness.resume();

  let inspection;
  try {
    inspection = await harness.inspect(context);
  } catch (error) {
    note(`could not inspect saved work in ${resumeDir}: ${error?.message ?? error}`);
    return;
  }

  const live = inspection.submissions.filter((s) => s.status === "queued" || s.status === "placed");
  let started = 0;
  for (const record of unfinished) {
    if (started >= MAX_CONCURRENT_RESUMES) {
      emit({ ev: "job", id: record.id, status: "failed",
             note: `not resumed: ${MAX_CONCURRENT_RESUMES} other job(s) were already recovering` });
      finishJob(record.id, "failed");
      continue;
    }
    const resumes = (record.resumes ?? 0) + 1;
    if (resumes > MAX_RESUMES) {
      note(`job ${record.id} failed ${resumes - 1} times already; not resuming again`);
      emit({ ev: "job", id: record.id, status: "failed",
             note: `gave up after ${MAX_RESUMES} restarts` });
      finishJob(record.id, "failed");
      continue;
    }

    const submission = live.find((s) => s.conversationId === record.conversationId);
    if (!submission) {
      // The conversation is saved but nothing is pending: the run ended (or never
      // started) before the host died, so there is nothing to pick up.
      note(`job ${record.id} has no unfinished submission; marking it failed`);
      emit({ ev: "job", id: record.id, status: "failed",
             note: "worker restarted and the job was not running" });
      finishJob(record.id, "failed");
      continue;
    }

    const handle = await harness.conversation(record.conversationId, context);
    if (!handle) {
      emit({ ev: "job", id: record.id, status: "failed", note: "worker lost the conversation" });
      finishJob(record.id, "failed");
      continue;
    }

    const job = {
      id: record.id,
      conversationId: record.conversationId,
      brief: record.brief,
      effort: record.effort,
      startedAt: record.startedAt,
      active: new Set(),
      lastNote: "resumed after a restart",
      settled: false,
      resumes,
      submission: null,
    };
    jobs.set(record.id, job);
    recordJob(job);
    started += 1;
    note(`resuming job ${record.id} (attempt ${resumes})`);
    emit({ ev: "job", id: record.id, status: "working", note: `resumed after a restart (attempt ${resumes})` });

    const reclaim = await harness.submission(submission.id, context, record.conversationId);
    if (!reclaim) {
      emit({ ev: "job", id: record.id, status: "failed", note: "worker could not reattach the job" });
      finishJob(record.id, "failed");
      jobs.delete(record.id);
      continue;
    }
    void settle(job, handle, reclaim).catch((error) => {
      note(`resumed job ${record.id} failed: ${error?.stack ?? error}`);
      emit({ ev: "job", id: record.id, status: "failed", note: String(error?.message ?? error) });
      finishJob(record.id, "failed");
      jobs.delete(record.id);
    });
  }
}

// ── stdin loop ───────────────────────────────────────────────────────────────
let buffer = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  buffer += chunk;
  let nl = buffer.indexOf("\n");
  while (nl >= 0) {
    const line = buffer.slice(0, nl).trim();
    buffer = buffer.slice(nl + 1);
    if (line) void handle(line);
    nl = buffer.indexOf("\n");
  }
});
process.stdin.on("end", () => void shutdown());

async function handle(line) {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    emit({ ev: "error", message: "not JSON" });
    return;
  }
  try {
    switch (msg.op) {
      case "spawn":
        if (!msg.id || !msg.brief) return emit({ ev: "error", message: "spawn needs id and brief" });
        // Fire and forget: the caller must not wait for a long job.
        void spawn(msg.id, msg.brief, msg.effort).catch((error) =>
          emit({ ev: "job", id: msg.id, status: "failed", note: String(error?.message ?? error) }),
        );
        return;
      case "stop":
        await stop(msg.id);
        return;
      case "status":
        await status(msg.id, msg.request);
        return;
      case "list":
        list();
        return;
      case "quit":
        await shutdown();
        return;
      default:
        emit({ ev: "error", message: `unknown op ${msg.op}` });
    }
  } catch (error) {
    note(`op ${msg.op} failed: ${error?.stack ?? error}`);
    emit({ ev: "error", message: String(error?.message ?? error) });
  }
}

let closing = false;
async function shutdown() {
  if (closing) return;
  closing = true;
  try {
    await Promise.race([harness.close(context), new Promise((r) => setTimeout(r, 3000))]);
  } catch (error) {
    note(`close failed: ${error?.message ?? error}`);
  }
  process.exit(0);
}
