// The worker must look like the session it serves: same model, same project
// rules. These check the resolution rules without starting a harness or a model.
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
// @ts-expect-error -- plain .mjs module, no types
import {
  FALLBACK_MODEL,
  INSTRUCTIONS_MAX,
  instructionsText,
  projectInstructions,
  resolveModel,
  shellEnv,
} from "./config.mjs";

/** A throwaway PI_HOME with a settings.json and models.json. */
function fakePiHome({ settings, providers }: { settings?: unknown; providers?: Record<string, unknown> } = {}) {
  const home = mkdtempSync(join(tmpdir(), "pi-home-"));
  if (settings) writeFileSync(join(home, "settings.json"), JSON.stringify(settings));
  if (providers) writeFileSync(join(home, "models.json"), JSON.stringify({ providers }));
  return home;
}

const localProvider = {
  name: "local",
  baseUrl: "http://127.0.0.1:9/v1",
  api: "openai-completions",
  apiKey: "from-config",
  models: [
    { id: "local/model-a", name: "A", input: ["text"], reasoning: false, contextWindow: 1000, maxTokens: 100 },
    { id: "local/model-b", name: "B", input: ["text"], reasoning: false, contextWindow: 1000, maxTokens: 100 },
  ],
};

describe("worker model resolution", () => {
  it("runs on the Pi default, the way Codex gives a worker the session's model", () => {
    const home = fakePiHome({
      settings: { defaultProvider: "local", defaultModel: "local/model-b" },
      providers: { local: localProvider },
    });
    const prev = process.env.PI_HOME;
    process.env.PI_HOME = home;
    try {
      const { models, model, source } = resolveModel({});
      assert.deepEqual(model, { provider: "local", modelId: "local/model-b" });
      assert.equal(source, "Pi default");
      // The provider must be usable, not just named.
      const resolved = models.getModel("local", "local/model-b");
      assert.ok(resolved, "the model must resolve inside the provider");
      assert.equal(resolved.api, "openai-completions");
      assert.equal(resolved.baseUrl, "http://127.0.0.1:9/v1", "models.json baseUrl reaches the model");
      assert.equal(resolved.provider, "local");
    } finally {
      if (prev == null) delete process.env.PI_HOME;
      else process.env.PI_HOME = prev;
    }
  });

  it("stamps api and baseUrl onto models, which models.json keeps at provider level", () => {
    // Without this the provider throws "no API implementation for undefined",
    // which is exactly how this broke the first time.
    const home = fakePiHome({
      settings: { defaultProvider: "local", defaultModel: "local/model-a" },
      providers: { local: localProvider },
    });
    const prev = process.env.PI_HOME;
    process.env.PI_HOME = home;
    try {
      const { models } = resolveModel({});
      for (const id of ["local/model-a", "local/model-b"]) {
        const m = models.getModel("local", id);
        assert.ok(m, `${id} must resolve`);
        assert.equal(m.api, "openai-completions", `${id} needs the provider's api`);
        assert.equal(m.baseUrl, "http://127.0.0.1:9/v1", `${id} needs the provider's baseUrl`);
      }
    } finally {
      if (prev == null) delete process.env.PI_HOME;
      else process.env.PI_HOME = prev;
    }
  });

  it("lets VOICE_WORKER_* pin a provider from models.json", () => {
    const home = fakePiHome({
      settings: { defaultProvider: "other", defaultModel: "other/x" },
      providers: { local: localProvider },
    });
    const prev = process.env.PI_HOME;
    process.env.PI_HOME = home;
    try {
      const { model, source } = resolveModel({ VOICE_WORKER_PROVIDER: "local", VOICE_WORKER_MODEL: "local/model-a" });
      assert.deepEqual(model, { provider: "local", modelId: "local/model-a" });
      assert.match(String(source), /VOICE_WORKER/);
    } finally {
      if (prev == null) delete process.env.PI_HOME;
      else process.env.PI_HOME = prev;
    }
  });

  it("falls back only when no Pi config is readable", () => {
    const home = fakePiHome({});
    const prev = process.env.PI_HOME;
    process.env.PI_HOME = home;
    try {
      const { model, source } = resolveModel({});
      assert.deepEqual(model, FALLBACK_MODEL);
      assert.equal(source, "built-in fallback");
    } finally {
      if (prev == null) delete process.env.PI_HOME;
      else process.env.PI_HOME = prev;
    }
  });

  it("falls back when the default names a model its provider does not list", () => {
    const home = fakePiHome({
      settings: { defaultProvider: "local", defaultModel: "local/missing" },
      providers: { local: localProvider },
    });
    const prev = process.env.PI_HOME;
    process.env.PI_HOME = home;
    try {
      const { model } = resolveModel({});
      assert.deepEqual(model, FALLBACK_MODEL, "an unresolvable default must not become a broken worker");
    } finally {
      if (prev == null) delete process.env.PI_HOME;
      else process.env.PI_HOME = prev;
    }
  });
});

describe("worker project instructions", () => {
  it("reads AGENTS.md and CLAUDE.md from the working folder, like the session does", () => {
    const dir = mkdtempSync(join(tmpdir(), "proj-"));
    writeFileSync(join(dir, "AGENTS.md"), "# Rules\nNever touch port 8766.");
    writeFileSync(join(dir, "CLAUDE.md"), "Read this first.");
    writeFileSync(join(dir, "README.md"), "not an instruction file");
    const text = projectInstructions(dir);
    assert.ok(text);
    assert.match(text, /Never touch port 8766/);
    assert.match(text, /Read this first/);
    assert.match(text, /### AGENTS\.md/);
    assert.match(text, /### CLAUDE\.md/);
    assert.doesNotMatch(text, /not an instruction file/, "README is not a rules file");
  });

  it("returns undefined when the project has no rules, so no empty section is sent", () => {
    const dir = mkdtempSync(join(tmpdir(), "proj-"));
    assert.equal(projectInstructions(dir), undefined);
    assert.equal(instructionsText(dir), undefined);
  });

  it("skips an empty rules file instead of sending a blank section", () => {
    const dir = mkdtempSync(join(tmpdir(), "proj-"));
    writeFileSync(join(dir, "AGENTS.md"), "   \n\n");
    assert.equal(projectInstructions(dir), undefined);
  });

  it("caps a huge rules file at the head, where the rules are", () => {
    const dir = mkdtempSync(join(tmpdir(), "proj-"));
    writeFileSync(join(dir, "CLAUDE.md"), `FIRST-RULE ${"x".repeat(INSTRUCTIONS_MAX * 2)} LAST-RULE`);
    const text = projectInstructions(dir) ?? "";
    assert.ok(text.length < INSTRUCTIONS_MAX + 200, `capped: ${text.length}`);
    assert.match(text, /FIRST-RULE/);
    assert.doesNotMatch(text, /LAST-RULE/);
    assert.match(text, /truncated/);
  });

  it("survives a missing folder", () => {
    assert.equal(projectInstructions("/nonexistent-project-folder-xyz"), undefined);
  });
});

describe("worker skills", () => {
  it("discovers every installed skill, across both roots", async () => {
    // @ts-expect-error -- plain .mjs module
    const { findSkills } = await import("./config.mjs");
    const skills = await findSkills();
    const names = skills.map((s: { name: string }) => s.name);
    assert.ok(names.length > 0, "at least one skill is installed on this machine");
    assert.deepEqual(names, [...names].sort(), "sorted for a stable listing");
    assert.equal(new Set(names).size, names.length, "no duplicate names across roots");
    for (const s of skills) {
      assert.ok(s.file.endsWith("/SKILL.md"), `${s.name} points at a SKILL.md`);
    }
  });

  it("finds a skill under a project-local .pi/skills root too", async () => {
    const { mkdtempSync: mk, writeFileSync: wr, mkdirSync: mkdir } = await import("node:fs");
    const dir = mk("proj-");
    mkdir(join(dir, ".pi", "skills", "local-only"), { recursive: true });
    wr(join(dir, ".pi", "skills", "local-only", "SKILL.md"), "# Local\nDo the thing.");
    // @ts-expect-error -- plain .mjs module
    const { findSkills } = await import("./config.mjs");
    const skills = await findSkills(dir);
    assert.ok(skills.some((s: { name: string }) => s.name === "local-only"), "project skills are visible");
  });

  it("ignores a folder without a SKILL.md", async () => {
    const { mkdtempSync: mk, mkdirSync: mkdir, writeFileSync: wr } = await import("node:fs");
    const dir = mk("proj-");
    mkdir(join(dir, ".pi", "skills", "not-a-skill"), { recursive: true });
    wr(join(dir, ".pi", "skills", "not-a-skill", "README.md"), "nope");
    // @ts-expect-error -- plain .mjs module
    const { findSkills } = await import("./config.mjs");
    const skills = await findSkills(dir);
    assert.equal(skills.some((s: { name: string }) => s.name === "not-a-skill"), false);
  });
});

describe("worker tool set", () => {
  // The durable worker must offer the same tools the session has for text work,
  // or turning it on would silently downgrade what "Pi" can do. Pi's builtins
  // are bash/edit/find/grep/ls/read/write; skills and the screen are the two
  // things a voice user asks for that no builtin covers.
  it("refuses writes through the tool layer, not only in the shell", async () => {
    const { readFileSync } = await import("node:fs");
    const { fileURLToPath } = await import("node:url");
    const src = readFileSync(fileURLToPath(new URL("./host.mjs", import.meta.url)), "utf8");
    // Both file-writing tools must check before touching anything.
    const write = src.slice(src.indexOf('name: "write"'), src.indexOf('name: "edit"'));
    const edit = src.slice(src.indexOf('name: "edit"'), src.indexOf('name: "grep"'));
    for (const [tool, body] of [["write", write], ["edit", edit]] as const) {
      assert.match(body, /allowWrite\(api, args\.path\)/, `${tool} must consult the policy`);
    }
    // bash can write anywhere, so the OS must enforce it.
    assert.match(src, /sandboxProfile\(/, "bash must run under sandbox-exec");
  });

  it("publishes Pi's builtin tool names, plus skill and screenshot", async () => {
    const { readFileSync } = await import("node:fs");
    const { fileURLToPath } = await import("node:url");
    const src = readFileSync(fileURLToPath(new URL("./host.mjs", import.meta.url)), "utf8");
    const registered = [...src.matchAll(/^(?:const \w+Tool = defineTool\(\{\s*\n\s*name: "([a-z]+)")/gm)].map((m) => m[1]);
    const listed = src.match(/tools: \[([^\]]+)\]/)?.[1] ?? "";
    const published = new Set([...listed.matchAll(/(\w+)Tool/g)].map((m) => m[1]));
    const defined = new Set(registered);
    assert.deepEqual([...published].sort(), [...defined].sort(), "every defined tool is registered");
    for (const name of ["bash", "edit", "find", "grep", "ls", "read", "write"]) {
      assert.ok(defined.has(name), `Pi's builtin ${name} must exist in the worker`);
    }
    for (const name of ["skill", "screenshot"]) {
      assert.ok(defined.has(name), `${name} closes a gap a builtin does not cover`);
    }
  });
});

describe("worker write policy", () => {
  // A background worker never prompts: nobody is attached, so a question would
  // deadlock it. Codex does the same — no prompting, but an inherited sandbox
  // limit. These pin the default and the escapes that must be refused.
  it("defaults to workspace-write, and refuses unknown modes", async () => {
    // @ts-expect-error -- plain .mjs module
    const { sandboxMode, DEFAULT_SANDBOX } = await import("./config.mjs");
    assert.equal(DEFAULT_SANDBOX, "workspace-write");
    assert.equal(sandboxMode({}), "workspace-write");
    assert.equal(sandboxMode({ VOICE_WORKER_SANDBOX: "read-only" }), "read-only");
    assert.equal(sandboxMode({ VOICE_WORKER_SANDBOX: "unrestricted" }), "unrestricted");
    assert.equal(sandboxMode({ VOICE_WORKER_SANDBOX: "yolo" }), "workspace-write", "an unknown mode is not trusted");
  });

  it("allows the workspace and refuses everything else", async () => {
    // @ts-expect-error -- plain .mjs module
    const { workspaceRoots, writablePath } = await import("./config.mjs");
    const roots = workspaceRoots(process.cwd(), {});
    assert.ok(writablePath("pi-voice/src/index.ts", roots), "a project path is writable");
    assert.ok(writablePath(process.cwd(), roots), "the root itself is writable");
    assert.equal(writablePath("/etc/passwd", roots), false, "outside the workspace is refused");
    assert.equal(writablePath(process.env.HOME + "/x.txt", roots), false, "home is refused");
    assert.equal(writablePath("../../../../etc/passwd", roots), false, ".. cannot escape");
  });

  it("extra roots widen the policy only where named", async () => {
    // @ts-expect-error -- plain .mjs module
    const { workspaceRoots, writablePath } = await import("./config.mjs");
    const roots = workspaceRoots(process.cwd(), { VOICE_WORKER_WRITE_ROOTS: "/tmp" });
    assert.ok(writablePath("/tmp/x.txt", roots), "an explicit extra root is writable");
    assert.equal(writablePath(process.env.HOME + "/x.txt", roots), false, "and nothing else is");
  });

  it("phrases the refusal so a voice answer can say it", async () => {
    // @ts-expect-error -- plain .mjs module
    const { writeRefusal } = await import("./config.mjs");
    const text = writeRefusal("/etc/passwd", ["/Users/me/project"]);
    assert.match(text, /^Refused:/);
    assert.match(text, /outside the workspace/);
    assert.match(text, /Say that plainly/, "the model is told to explain, not retry");
    assert.match(text, /VOICE_WORKER_WRITE_ROOTS/, "the user is told how to widen it");
  });

  it("generates a profile that denies writes outside the roots", async () => {
    // @ts-expect-error -- plain .mjs module
    const { sandboxProfile } = await import("./config.mjs");
    const profile = sandboxProfile(["/Users/me/project"]);
    assert.match(profile, /\(deny file-write\*/);
    assert.match(profile, /\(subpath "\/Users\/me\/project"\)/);
    assert.match(profile, /\(subpath "\/private\/tmp"\)/, "temp dirs stay writable for normal tooling");
  });
});

describe("worker crash resume", () => {
  // A host that dies mid-job used to lose the work silently while the voice call
  // kept talking. These pin the recovery rules; the host's own behaviour is
  // covered by the kill-and-restart check in the handoff.
  it("picks the newest run that still has unfinished jobs, and nothing otherwise", async () => {
    const { mkdtempSync, writeFileSync, mkdirSync } = await import("node:fs");
    const { resumeTarget, runDirectories, readJobs, writeJobs } = await import("./config.mjs");
    const root = mkdtempSync(join(tmpdir(), "workers-"));
    mkdirSync(join(root, "run-a"), { recursive: true });
    mkdirSync(join(root, "run-b"), { recursive: true });
    writeJobs(join(root, "run-a"), [{ id: "old", finished: true }]);
    writeJobs(join(root, "run-b"), [{ id: "live", finished: false }]);

    assert.deepEqual(
      runDirectories(root).map((d: string) => d.split("/").pop()),
      ["run-b", "run-a"],
      "newest first",
    );
    assert.equal(resumeTarget(root, {}), join(root, "run-b"), "the unfinished run wins");
    assert.deepEqual(readJobs(join(root, "run-b")), [{ id: "live", finished: false }]);

    // Everything finished: start fresh.
    writeJobs(join(root, "run-b"), [{ id: "live", finished: true }]);
    assert.equal(resumeTarget(root, {}), undefined);
  });

  it("can be told which run to reopen, or to never resume", async () => {
    const { resumeTarget } = await import("./config.mjs");
    assert.equal(resumeTarget("/state", { VOICE_WORKER_RESUME: "/state/run-x" }), "/state/run-x");
    for (const off of ["none", "off", "0", "NONE"]) {
      assert.equal(resumeTarget("/state", { VOICE_WORKER_RESUME: off }), undefined, off);
    }
  });

  it("survives a missing or corrupt job record", async () => {
    const { mkdtempSync, writeFileSync } = await import("node:fs");
    const { readJobs, resumeTarget } = await import("./config.mjs");
    const root = mkdtempSync(join(tmpdir(), "workers-"));
    assert.deepEqual(readJobs(root), [], "no file is no jobs");
    writeFileSync(join(root, "jobs.json"), "{not json");
    assert.deepEqual(readJobs(root), [], "corrupt is no jobs");
    assert.equal(resumeTarget(root, {}), undefined, "corrupt state must not block a fresh run");
  });

  it("caps resumes, so a job that always crashes is declared dead", async () => {
    const { MAX_RESUMES, MAX_CONCURRENT_RESUMES } = await import("./config.mjs");
    assert.equal(MAX_RESUMES, 2);
    assert.equal(MAX_CONCURRENT_RESUMES, 2);
  });
});

describe("shellEnv", () => {
  it("points pi-browser state at the temp dir so the sandbox allows it", () => {
    const env = shellEnv({ PATH: "/bin" });
    assert.equal(env.PIBROWSE_STATE, join(tmpdir(), "pi-voice-browser"));
    assert.equal(env.PATH, "/bin");
  });

  it("keeps an explicit PIBROWSE_STATE", () => {
    assert.equal(shellEnv({ PIBROWSE_STATE: "/x/y" }).PIBROWSE_STATE, "/x/y");
  });
});
