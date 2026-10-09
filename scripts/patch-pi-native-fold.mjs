#!/usr/bin/env node
/**
 * Scoped renderer repair for a reviewed Pi bundle: while `/voice` is on, Pi's
 * assistant answers and tool calls start folded and toggle open on click.
 *
 * Usage: patch-pi-native-fold.mjs <pi-coding-agent package dir> [--check|--restore]
 *   --check    report what would happen; write nothing
 *   --restore  put the original bundle back from the backup
 *
 * Only bundles listed in REVIEWED (exact version, chunk and sha256) are patched.
 * Pi 1.1.0 still renders answers and tool calls expanded with no click toggle,
 * so it still needs this; a version whose renderer no longer matches is left
 * untouched with a message, never patched blind.
 */
import { createHash } from "node:crypto";
import { copyFileSync, existsSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const PACKAGE = "@earendil-works/pi-coding-agent";
export const MARKER = "globalThis.__piVoiceActive";

/** version -> renderer chunk and the sha256 of the unpatched chunk we reviewed. */
export const REVIEWED = {
  "0.87.1": { chunk: "chunk-OJP47DM6.js", sha256: "81c81a21ec81e84200205f561687408ff5e3738fbbbb3c2a6c186b348d376020" },
  "1.1.0": { chunk: "chunk-OIM2DMFI.js", sha256: "463db439b55a9c580d4c153492dcdd4e215fe4709ee71a876d4edff1074cdb8f" },
};

export const EDITS = [
  {
    name: "assistant answer folds",
    before: 'render(width){let lines=super.render(width);return this.hasToolCalls||lines.length===0||(lines[0]=OSC133_ZONE_START+lines[0],lines[lines.length-1]=OSC133_ZONE_END+OSC133_ZONE_FINAL+lines[lines.length-1]),lines}updateContent(message,isStreaming=this.isStreaming){',
    after: 'render(width){if(globalThis.__piVoiceActive&&this.lastMessage?.content?.some(c=>c.type==="text"&&c.text?.trim())&&!this.piVoiceExpanded)return [`▸ Pi response · ${this.isStreaming?"writing…":"ready"}`.slice(0,width)];let lines=super.render(width);return this.hasToolCalls||lines.length===0||(lines[0]=OSC133_ZONE_START+lines[0],lines[lines.length-1]=OSC133_ZONE_END+OSC133_ZONE_FINAL+lines[lines.length-1]),lines}handleMouse(event){if(globalThis.__piVoiceActive&&event.type==="click"&&event.button==="left"&&this.lastMessage?.content?.some(c=>c.type==="text"&&c.text?.trim())){this.piVoiceExpanded=!this.piVoiceExpanded;return {handled:!0,render:!0}}return super.handleMouse(event)}updateContent(message,isStreaming=this.isStreaming){',
  },
  {
    name: "tool call toggles on click",
    before: 'handleMouse(event){if(!this.hasRendererDefinition()||this.getRenderShell()!=="self")return super.handleMouse(event);',
    after: 'handleMouse(event){if(globalThis.__piVoiceActive&&event.type==="click"&&event.button==="left"){this.setExpanded(!this.expanded);return {handled:!0,render:!0}}if(!this.hasRendererDefinition()||this.getRenderShell()!=="self")return super.handleMouse(event);',
  },
  {
    name: "tool call title folds",
    before: 'formatToolExecution(){let text=theme.fg("toolTitle",theme.bold(this.toolName)),content=JSON.stringify(this.args,null,2);',
    after: 'formatToolExecution(){if(globalThis.__piVoiceActive&&!this.expanded)return theme.fg("toolTitle",`▸ ${this.toolName}`);let text=theme.fg("toolTitle",theme.bold(this.toolName)),content=JSON.stringify(this.args,null,2);',
  },
];

export const sha256 = (text) => createHash("sha256").update(text).digest("hex");

/** Apply every edit; throws unless each anchor matches exactly once. */
export function patchRenderer(text) {
  let out = text;
  for (const edit of EDITS) {
    const n = out.split(edit.before).length - 1;
    if (n !== 1) throw new Error(`Expected exactly one renderer match for "${edit.name}", found ${n}.`);
    out = out.replace(edit.before, () => edit.after);
  }
  return out;
}

/**
 * Decide what to do without writing. Returns { action, message, file?, patched? }
 * where action is "patch", "noop" (already patched) or "refuse" (unknown or changed bundle).
 */
export function plan(root) {
  const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
  if (pkg.name !== PACKAGE) return { action: "refuse", message: `Not the Pi package: found ${pkg.name}.` };
  const reviewed = REVIEWED[pkg.version];
  if (!reviewed) {
    return { action: "refuse", message: `Pi ${pkg.version} has no reviewed renderer patch (reviewed: ${Object.keys(REVIEWED).join(", ")}). No change was made; review that bundle and add it to REVIEWED.` };
  }
  const file = join(root, "dist/bundle/chunks", reviewed.chunk);
  if (!existsSync(file)) return { action: "refuse", message: `Pi ${pkg.version} renderer chunk ${reviewed.chunk} is missing. No change was made.` };
  const text = readFileSync(file, "utf8");
  if (text.includes(MARKER)) return { action: "noop", file, message: `Pi ${pkg.version} is already patched for pi-voice folding; nothing to do.` };
  if (sha256(text) !== reviewed.sha256) {
    return { action: "refuse", file, message: `Pi ${pkg.version} renderer differs from the reviewed bundle. No change was made.` };
  }
  return { action: "patch", file, patched: patchRenderer(text), message: `Pi ${pkg.version} renderer matches the reviewed bundle.` };
}

export function restore(root) {
  const p = plan(root);
  const reviewed = REVIEWED[JSON.parse(readFileSync(join(root, "package.json"), "utf8")).version];
  if (!reviewed) throw new Error(p.message);
  const file = join(root, "dist/bundle/chunks", reviewed.chunk);
  const backup = `${file}.pi-voice-original`;
  if (!existsSync(backup)) throw new Error("No original bundle backup exists.");
  copyFileSync(backup, file);
  unlinkSync(backup);
  return "Restored the original Pi terminal renderer.";
}

export function apply(root) {
  const p = plan(root);
  if (p.action === "refuse") throw new Error(p.message);
  if (p.action === "noop") return p.message;
  const backup = `${p.file}.pi-voice-original`;
  if (existsSync(backup)) throw new Error("A Pi voice backup already exists; restore it before patching again.");
  copyFileSync(p.file, backup);
  const temporary = `${p.file}.pi-voice-new`;
  try {
    writeFileSync(temporary, p.patched);
    renameSync(temporary, p.file);
  } catch (error) {
    copyFileSync(backup, p.file);
    throw error;
  }
  return "Pi voice terminal entries now start folded; click each entry to expand. Original bundle backed up.";
}

function main(argv) {
  const [root, flag] = argv;
  if (!root) throw new Error("Pass the Pi package directory to patch, check or restore.");
  if (flag === "--restore") return restore(root);
  if (flag === "--check") {
    const p = plan(root);
    return `${p.message} ${p.action === "patch" ? "Would patch." : p.action === "noop" ? "Would do nothing." : "Would refuse."}`;
  }
  if (flag) throw new Error(`Unknown option ${flag}.`);
  return apply(root);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    process.stdout.write(`${main(process.argv.slice(2))}\n`);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exit(1);
  }
}
