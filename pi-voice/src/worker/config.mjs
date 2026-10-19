// Worker configuration: model, credentials and project instructions.
//
// Kept apart from `host.mjs` so it can be tested without starting a harness or
// touching a model. The rules it encodes mirror what Codex does when it spawns a
// worker thread (`readConfig(cwd, { threadStartKind })` in the desktop app):
//
//   - the worker runs on the *session's* model, not a separate default
//     (Codex sends the session's `latestModel` as `backend_model_name`)
//   - it reads the same project instructions as the session
//   - credentials come from the machine's normal store, never from a constant

import { existsSync, readdirSync, readFileSync, realpathSync, renameSync, writeFileSync } from "node:fs";
import { readdir } from "node:fs/promises";
import { homedir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { createModels, createProvider } from "@earendil-works/pi-ai/models";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";
import { anthropicMessagesApi } from "@earendil-works/pi-ai/api/anthropic-messages.lazy";
import { openrouterProvider } from "@earendil-works/pi-ai/providers/openrouter";

/** Where Pi keeps its settings and model catalog. `PI_HOME` overrides for tests. */
export function piHome() {
  return process.env.PI_HOME || join(homedir(), ".pi", "agent");
}

export function readJson(file) {
  try {
    return JSON.parse(readFileSync(file, "utf8"));
  } catch {
    return undefined;
  }
}

// ── credentials ──────────────────────────────────────────────────────────────
// The extension runs inside Pi, which has no provider environment of its own.
// The rest of the stack reads `~/.config/chatbot/env` (run-browser.sh sources the
// same file), so the worker does too. A variable already in the environment wins.
export function loadCredentials(env = process.env) {
  const file = env.CHATBOT_ENV || join(homedir(), ".config", "chatbot", "env");
  if (!existsSync(file)) return { file, loaded: 0 };
  let text;
  try {
    text = readFileSync(file, "utf8");
  } catch {
    return { file, loaded: 0 };
  }
  let loaded = 0;
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const m = line.match(/^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$/);
    if (!m) continue;
    const [, key, rawValue] = m;
    if (env[key] != null && env[key] !== "") continue;
    let value = rawValue.trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    env[key] = value;
    loaded += 1;
  }
  return { file, loaded };
}

/** Whether a provider needs a key from the environment. */
export function hasCredential(provider, env = process.env) {
  // A provider defined in models.json carries its own key (or is a keyless local
  // proxy, which works with any value), so it needs nothing from the environment.
  const inConfig = readJson(join(piHome(), "models.json"))?.providers?.[provider];
  if (inConfig) return true;
  if (provider === "openrouter") return Boolean(env.OPENROUTER_API_KEY);
  if (provider === "openai") return Boolean(env.OPENAI_API_KEY);
  if (provider === "anthropic") return Boolean(env.ANTHROPIC_API_KEY);
  return true;
}

// ── model ────────────────────────────────────────────────────────────────────

/** The api implementation for one `models.json` provider entry. */
function apiFor(api) {
  if (api === "anthropic-messages") return { "anthropic-messages": anthropicMessagesApi() };
  return { "openai-completions": openAICompletionsApi() };
}

/**
 * Build a provider from one `models.json` entry.
 *
 * Model records are kept verbatim apart from the three fields `createProvider`
 * dispatches on, because `models.json` stores them at the provider level:
 * `api` (otherwise "no API implementation for undefined") and `baseUrl`.
 */
export function pinProvider(id, entry) {
  const keyed = Boolean(entry.apiKey);
  return createProvider({
    id,
    name: entry.name ?? id,
    baseUrl: entry.baseUrl,
    ...(entry.compat ? { compat: entry.compat } : {}),
    api: apiFor(entry.api),
    auth: {
      apiKey: {
        name: keyed ? `${id} (from models.json)` : `${id} (keyless)`,
        resolve: async () => ({ auth: { apiKey: entry.apiKey ?? "local" }, source: keyed ? "models.json" : "keyless" }),
        check: async () => ({ type: "api_key", source: keyed ? "models.json" : "keyless" }),
      },
    },
    models: entry.models.map((model) => ({
      type: "chat",
      ...model,
      provider: id,
      api: model.api ?? entry.api ?? "openai-completions",
      baseUrl: model.baseUrl ?? entry.baseUrl,
    })),
  });
}

/** The built-in fallback, used only when no Pi config is readable. */
export const FALLBACK_MODEL = { provider: "openrouter", modelId: "anthropic/claude-haiku-4.5" };

/**
 * Resolve the model to run on.
 *
 * Precedence, highest first:
 *   1. `VOICE_WORKER_PROVIDER` + `VOICE_WORKER_MODEL` (an explicit pin)
 *   2. the user's Pi default provider/model, with its own provider definition
 *   3. the built-in OpenRouter fallback
 *
 * Returns the models collection too, since the caller needs both.
 */
export function resolveModel(env = process.env) {
  const models = createModels();
  const home = piHome();
  const catalog = readJson(join(home, "models.json"))?.providers ?? {};
  const settings = readJson(join(home, "settings.json")) ?? {};

  const pinned = {
    provider: env.VOICE_WORKER_PROVIDER?.trim(),
    modelId: env.VOICE_WORKER_MODEL?.trim(),
  };
  if (pinned.provider && pinned.modelId) {
    if (catalog[pinned.provider]) {
      models.setProvider(pinProvider(pinned.provider, catalog[pinned.provider]));
      return { models, model: pinned, source: "VOICE_WORKER_* + models.json" };
    }
    if (pinned.provider === "openrouter") {
      models.setProvider(openrouterProvider());
      return { models, model: pinned, source: "VOICE_WORKER_*" };
    }
  }

  const providerId = settings.defaultProvider;
  const modelId = settings.defaultModel;
  if (providerId && modelId && catalog[providerId]) {
    models.setProvider(pinProvider(providerId, catalog[providerId]));
    if (models.getModel(providerId, modelId) !== undefined) {
      return { models, model: { provider: providerId, modelId }, source: "Pi default" };
    }
  }

  models.setProvider(openrouterProvider());
  return { models, model: { ...FALLBACK_MODEL }, source: "built-in fallback" };
}

// ── project instructions ─────────────────────────────────────────────────────
// Codex resolves a worker's config from the source conversation's cwd, so a
// spawned thread reads the same project instructions as the session. This does
// the same for the files Pi itself reads at the top of a project.
export const INSTRUCTION_FILES = ["AGENTS.md", "CLAUDE.md"];
/** Cap keeps a large rules file from crowding out the job; the head holds the rules. */
export const INSTRUCTIONS_MAX = 24_000;

export function projectInstructions(cwd = process.cwd()) {
  const parts = [];
  for (const name of INSTRUCTION_FILES) {
    const file = join(cwd, name);
    try {
      if (!existsSync(file)) continue;
      const text = readFileSync(file, "utf8").trim();
      if (!text) continue;
      const body = text.length > INSTRUCTIONS_MAX ? `${text.slice(0, INSTRUCTIONS_MAX)}\n…(truncated)` : text;
      parts.push(`### ${name}\n\n${body}`);
    } catch {
      // An unreadable file is not a reason to fail the job.
    }
  }
  return parts.length === 0 ? undefined : parts.join("\n\n");
}

/** The prompt section text, or undefined when the project has no rules files. */
export function instructionsText(cwd) {
  const text = projectInstructions(cwd);
  if (!text) return undefined;
  return `These are this project's instructions. Follow them for every task.\n\n${text}`;
}

// ── skills ───────────────────────────────────────────────────────────────────
// Pi's skills are Markdown instructions plus shell, so listing and loading them
// is what lets a worker follow the same procedure the session would.
export function skillRoots(cwd = process.cwd()) {
  return [
    join(homedir(), ".pi", "agent", "skills"),
    join(homedir(), ".agents", "skills"),
    join(cwd, ".pi", "skills"),
  ];
}

/** Every installed skill with a SKILL.md, first root winning, sorted by name. */
export async function findSkills(cwd = process.cwd()) {
  const found = [];
  for (const root of skillRoots(cwd)) {
    let entries;
    try {
      entries = await readdir(root, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of entries) {
      if (found.some((s) => s.name === entry.name)) continue;
      const dir = join(root, entry.name);
      const file = join(dir, "SKILL.md");
      // Follow symlinks and plain dirs alike; a linked skill is still a skill.
      if (!existsSync(file)) continue;
      found.push({ name: entry.name, file, dir });
    }
  }
  return found.sort((a, b) => a.name.localeCompare(b.name));
}

// ── write policy ─────────────────────────────────────────────────────────────
// A background worker never asks a question: nobody is attached to it, so a
// prompt would deadlock the job. Codex answers that the same way it does for its
// own workers — no prompting, but an inherited limit on where writes may land
// (`sandboxPolicy`, defaulting toward `workspace-write`, never full access).
//
// Pi has no sandbox setting to inherit, so the limit is defined here: read
// anywhere, write only inside the workspace, and refuse with a reason the voice
// model can say out loud.
export const SANDBOX_MODES = ["workspace-write", "read-only", "unrestricted"];
export const DEFAULT_SANDBOX = "workspace-write";

/** The configured mode. An unknown value falls back to the safe default. */
export function sandboxMode(env = process.env) {
  const raw = (env.VOICE_WORKER_SANDBOX ?? "").trim().toLowerCase();
  return SANDBOX_MODES.includes(raw) ? raw : DEFAULT_SANDBOX;
}

/** Directories a worker may write inside: the workspace, plus any extra roots. */
export function workspaceRoots(cwd = process.cwd(), env = process.env) {
  const extra = (env.VOICE_WORKER_WRITE_ROOTS ?? "")
    .split(":")
    .map((p) => p.trim())
    .filter(Boolean);
  const roots = [cwd, ...extra];
  const seen = new Set();
  for (const root of roots) {
    try {
      seen.add(realpathSync(root));
    } catch {
      // A missing extra root is ignored; the workspace itself always exists.
    }
  }
  return [...seen];
}

/**
 * Whether `target` may be written. Checked on the resolved path, so `..` and a
 * symlink cannot step outside the workspace.
 */
export function writablePath(target, roots) {
  if (!target) return false;
  const absolute = resolve(String(target));
  let resolved;
  try {
    resolved = realpathSync(absolute);
  } catch {
    // Not created yet: resolve the parent, which must exist and be allowed.
    try {
      resolved = join(realpathSync(dirname(absolute)), basename(absolute));
    } catch {
      resolved = absolute;
    }
  }
  return roots.some((root) => resolved === root || resolved.startsWith(root.endsWith("/") ? root : `${root}/`));
}

/** The refusal a tool returns, phrased for a spoken answer. */
export function writeRefusal(target, roots) {
  return (
    `Refused: ${target} is outside the workspace, and this worker may only write inside it `
    + `(${roots.join(", ")}). Say that plainly; do not retry with another path. `
    + `The user can widen it with VOICE_WORKER_WRITE_ROOTS or VOICE_WORKER_SANDBOX=unrestricted.`
  );
}

/**
 * An SBPL profile for `sandbox-exec`: writes only inside `roots` plus the temp
 * dirs and devices a shell needs. Mirrors the profile the Python research `bash`
 * tool already uses, widened from temp-only to the workspace.
 */
export function sandboxProfile(roots) {
  const allow = roots.map((root) => `      (subpath ${JSON.stringify(root)})`).join("\n");
  return `(version 1)
(allow default)
(deny file-write*
  (require-not
    (require-any
${allow}
      (subpath "/private/tmp")
      (subpath "/private/var/folders")
      (literal "/dev/null")
      (literal "/dev/tty")
      (regex #"^/dev/fd/"))))
`;
}

// ── worker state ─────────────────────────────────────────────────────────────
// Where a job's conversation is kept. Memory would make the work invisible to
// everything outside the host process and lose it when the host exits, which is
// exactly why "I can't see what Pi is doing" happened.
export function workerStateRoot(env = process.env) {
  const explicit = (env.VOICE_WORKER_STATE ?? "").trim();
  if (explicit) return explicit;
  return join(homedir(), ".config", "pi-voice", "workers");
}

/** How many times one job may be resumed before it is declared dead. */
export const MAX_RESUMES = 2;
/** How many jobs a restart resumes at once (Codex caps this at 2 as well). */
export const MAX_CONCURRENT_RESUMES = 2;

/** The run directories under the state root, newest first. */
export function runDirectories(root = workerStateRoot()) {
  let names;
  try {
    names = readdirSync(root, { withFileTypes: true })
      .filter((e) => e.isDirectory() && e.name.startsWith("run-"))
      .map((e) => e.name)
      .sort()
      .reverse();
  } catch {
    return [];
  }
  return names.map((name) => join(root, name));
}

/**
 * The directory to reopen on start, or undefined to begin a fresh run.
 *
 * `VOICE_WORKER_RESUME` may name a directory, or `none` to never resume. Without
 * it the newest run that still has unfinished jobs is reused, which is what makes
 * a host crash recoverable: the saved conversation is where the work stopped.
 */
export function resumeTarget(root = workerStateRoot(), env = process.env) {
  const choice = (env.VOICE_WORKER_RESUME ?? "").trim();
  if (choice && ["none", "off", "0"].includes(choice.toLowerCase())) return undefined;
  if (choice) return choice;
  for (const dir of runDirectories(root)) {
    const jobs = readJobs(dir);
    if (jobs.some((j) => !j.finished)) return dir;
  }
  return undefined;
}

/** The saved job records for one run directory. */
export function readJobs(dir) {
  try {
    const data = JSON.parse(readFileSync(join(dir, "jobs.json"), "utf8"));
    return Array.isArray(data) ? data : [];
  } catch {
    return [];
  }
}

/** Write the job records for one run directory, atomically. */
export function writeJobs(dir, jobs) {
  try {
    const file = join(dir, "jobs.json");
    const tmp = `${file}.${process.pid}.tmp`;
    writeFileSync(tmp, JSON.stringify(jobs, null, 2), { mode: 0o600 });
    renameSync(tmp, file);
    return true;
  } catch {
    return false;
  }
}
