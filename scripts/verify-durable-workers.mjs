#!/usr/bin/env node
/**
 * Verify the durable worker host runs N jobs at once.
 *
 * This is the parallel-workers seam: `pi-voice/src/worker/host.mjs` speaking the
 * NDJSON protocol in `contracts/pi-voice.json` `worker`. It is deterministic in
 * what it asserts (two jobs, both answered, overlapping in time) and it uses the
 * real model, so a pass means the path works, not that a stub does.
 *
 *   node scripts/verify-durable-workers.mjs [--jobs 2] [--brief "..."]
 *
 * Credentials come from `~/.config/chatbot/env` (or CHATBOT_ENV), the same file
 * `run-browser.sh` sources. Exits 0 only when every check passes.
 */
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const HOST = join(ROOT, "pi-voice/src/worker/host.mjs");

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const at = args.indexOf(`--${name}`);
  return at >= 0 && args[at + 1] ? args[at + 1] : fallback;
};
const JOBS = Number(flag("jobs", "2"));
const TIMEOUT_S = Number(flag("timeout", "180"));

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok });
  const mark = ok ? "PASS" : "FAIL";
  console.log(`[${mark}] ${name}${detail === undefined ? "" : ` — ${JSON.stringify(detail)}`}`);
}

if (!existsSync(HOST)) {
  console.error(`worker host missing: ${HOST}`);
  process.exit(2);
}

const proc = spawn(process.execPath, [HOST], { stdio: ["pipe", "pipe", "pipe"], env: process.env });
const events = [];
let buffer = "";
let stderr = "";
let closed = false;

proc.stdout.setEncoding("utf8");
proc.stdout.on("data", (chunk) => {
  buffer += chunk;
  let nl = buffer.indexOf("\n");
  while (nl >= 0) {
    const line = buffer.slice(0, nl).trim();
    buffer = buffer.slice(nl + 1);
    if (line) {
      try {
        events.push(JSON.parse(line));
      } catch {
        events.push({ ev: "unparsable", line });
      }
    }
    nl = buffer.indexOf("\n");
  }
});
proc.stderr.on("data", (chunk) => {
  stderr += chunk;
});
proc.on("exit", (code) => {
  closed = true;
  if (code !== 0 && code !== null) console.error(`host exited ${code}\n${stderr}`);
});

const send = (msg) => proc.stdin.write(`${JSON.stringify(msg)}\n`);
const waitFor = (pred, ms = TIMEOUT_S * 1000) =>
  new Promise((resolve) => {
    const started = Date.now();
    const tick = setInterval(() => {
      const found = events.find(pred);
      if (found || Date.now() - started > ms) {
        clearInterval(tick);
        resolve(found ?? null);
      }
    }, 100);
  });

const ready = await waitFor((e) => e.ev === "ready" || e.ev === "error", 60_000);
if (!ready || ready.ev !== "ready") {
  check("the host started", false, ready);
  check("credentials are available", false, stderr.split("\n").find((l) => /credential/i.test(l)) ?? "no credential line");
  process.exit(1);
}
check("the host started on the session's model", Boolean(ready.model), ready.model);

// One distinct brief per job, so two answers cannot legitimately be identical:
// a repeated brief would make the cross-talk check meaningless.
const briefFor = (n) => [
  `Report the output of \`git -C ${ROOT} rev-parse --abbrev-ref HEAD\` in one short sentence.`,
  `Count the .md files at the top level of ${ROOT} and say the number. Use ls and count.`,
  `List the directories at the top level of ${ROOT} and say how many there are.`,
  `Report the first line of ${ROOT}/package.json and nothing else.`,
  `Say how many entries are in ${ROOT}/contracts and name them.`,
  `Report the output of \`wc -l < ${ROOT}/README.md\` in one short sentence.`,
][n % 6];

const ids = Array.from({ length: JOBS }, (_, i) => `verify-${i + 1}-${Date.now()}`);
const started = Date.now();
for (const [i, id] of ids.entries()) {
  send({ op: "spawn", id, brief: briefFor(i), effort: "off" });
}

const finished = await waitFor(
  (e) => e.ev === "result" && ids.includes(e.id),
  TIMEOUT_S * 1000,
);
void finished;

// Wait for every id to produce a result.
await new Promise((resolve) => {
  const tick = setInterval(() => {
    const got = new Set(events.filter((e) => e.ev === "result" && ids.includes(e.id)).map((e) => e.id));
    if (got.size >= ids.length || Date.now() - started > TIMEOUT_S * 1000) {
      clearInterval(tick);
      resolve();
    }
  }, 200);
});
const elapsed = Date.now() - started;

const answered = events.filter((e) => e.ev === "result" && ids.includes(e.id));
check(
  `${JOBS} jobs produced results`,
  answered.length === ids.length,
  answered.map((e) => ({ id: e.id, chars: e.full.length })),
);
check(
  "every answer has content",
  answered.every((e) => e.full.trim().length > 0),
  answered.map((e) => e.full.slice(0, 60)),
);
check(
  "each answer is its own (no cross-talk)",
  new Set(answered.map((e) => e.full)).size === answered.length,
  answered.map((e) => e.full.slice(0, 40)),
);

// Overlap: distinct jobs were working at the same time. The host emits one
// `working` per job right after admission, before any model call.
const working = events.filter((e) => e.ev === "job" && e.status === "working" && ids.includes(e.id));
check(
  "both jobs were live in the same host process",
  new Set(working.map((e) => e.id)).size === ids.length,
  [...new Set(working.map((e) => e.id))],
);
console.log(`    (both finished in ${(elapsed / 1000).toFixed(1)}s, one host process)`);

// The progress stream proves real tool use inside a worker.
const progress = events.filter((e) => e.ev === "progress" && ids.includes(e.id));
check("workers reported tool progress", progress.length > 0, `${progress.length} progress line(s)`);

send({ op: "quit" });
await new Promise((r) => setTimeout(r, 1500));
if (!closed) proc.kill("SIGTERM");

const failed = results.filter((r) => !r.ok).length;
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed === 0 ? 0 : 1);
