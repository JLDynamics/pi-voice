#!/usr/bin/env node
/** Scoped renderer repair for the installed Pi 0.87.1 bundle. */
import { createHash } from "node:crypto";
import { copyFileSync, existsSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const root = process.argv[2];
const restoring = process.argv[3] === "--restore";
if (!root) throw new Error("Pass the Pi package directory to patch or restore.");
const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
if (pkg.name !== "@earendil-works/pi-coding-agent" || pkg.version !== "0.87.1") {
  throw new Error(`Expected Pi 0.87.1; found ${pkg.name} ${pkg.version}`);
}
const file = join(root, "dist/bundle/chunks/chunk-OJP47DM6.js");
const backup = `${file}.pi-voice-original`;
if (restoring) {
  if (!existsSync(backup)) throw new Error("No original bundle backup exists.");
  copyFileSync(backup, file);
  unlinkSync(backup);
  process.stdout.write("Restored the original Pi terminal renderer.\n");
  process.exit(0);
}

const original = readFileSync(file, "utf8");
const hash = createHash("sha256").update(original).digest("hex");
if (hash !== "81c81a21ec81e84200205f561687408ff5e3738fbbbb3c2a6c186b348d376020") {
  throw new Error("Pi's renderer differs from the reviewed 0.87.1 bundle; no change was made.");
}
if (existsSync(backup)) throw new Error("A Pi voice backup already exists; restore it before patching again.");

function replaceOnce(text, before, after) {
  if (text.split(before).length !== 2) throw new Error(`Expected exactly one renderer match: ${before.slice(0, 45)}`);
  return text.replace(before, after);
}

let patched = original;
const assistant = 'render(width){let lines=super.render(width);return this.hasToolCalls||lines.length===0||(lines[0]=OSC133_ZONE_START+lines[0],lines[lines.length-1]=OSC133_ZONE_END+OSC133_ZONE_FINAL+lines[lines.length-1]),lines}updateContent(message,isStreaming=this.isStreaming){';
const assistantFold = 'render(width){if(globalThis.__piVoiceActive&&this.lastMessage?.content?.some(c=>c.type==="text"&&c.text?.trim())&&!this.piVoiceExpanded)return [`▸ Pi response · ${this.isStreaming?"writing…":"ready"}`.slice(0,width)];let lines=super.render(width);return this.hasToolCalls||lines.length===0||(lines[0]=OSC133_ZONE_START+lines[0],lines[lines.length-1]=OSC133_ZONE_END+OSC133_ZONE_FINAL+lines[lines.length-1]),lines}handleMouse(event){if(globalThis.__piVoiceActive&&event.type==="click"&&event.button==="left"&&this.lastMessage?.content?.some(c=>c.type==="text"&&c.text?.trim())){this.piVoiceExpanded=!this.piVoiceExpanded;return {handled:!0,render:!0}}return super.handleMouse(event)}updateContent(message,isStreaming=this.isStreaming){';
patched = replaceOnce(patched, assistant, assistantFold);

const toolMouse = 'handleMouse(event){if(!this.hasRendererDefinition()||this.getRenderShell()!=="self")return super.handleMouse(event);';
const toolMouseFold = 'handleMouse(event){if(globalThis.__piVoiceActive&&event.type==="click"&&event.button==="left"){this.setExpanded(!this.expanded);return {handled:!0,render:!0}}if(!this.hasRendererDefinition()||this.getRenderShell()!=="self")return super.handleMouse(event);';
patched = replaceOnce(patched, toolMouse, toolMouseFold);

const toolText = 'formatToolExecution(){let text=theme.fg("toolTitle",theme.bold(this.toolName)),content=JSON.stringify(this.args,null,2);';
const toolTextFold = 'formatToolExecution(){if(globalThis.__piVoiceActive&&!this.expanded)return theme.fg("toolTitle",`▸ ${this.toolName}`);let text=theme.fg("toolTitle",theme.bold(this.toolName)),content=JSON.stringify(this.args,null,2);';
patched = replaceOnce(patched, toolText, toolTextFold);

copyFileSync(file, backup);
const temporary = `${file}.pi-voice-new`;
try {
  writeFileSync(temporary, patched);
  renameSync(temporary, file);
} catch (error) {
  copyFileSync(backup, file);
  throw error;
}
process.stdout.write("Pi voice terminal entries now start folded; click each entry to expand. Original bundle backed up.\n");
