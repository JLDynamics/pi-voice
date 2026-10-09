#!/usr/bin/env node
// Writes a synthetic earlier call into an isolated cabinet with the extension's own
// Conversation code, reopens it like a restart, and prints the startup pack as JSON.
// Needs PI_VOICE_CONFIG (scratch dir), PV_PROJ (fixture folder), PV_SRC (pi-voice/src).
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const src = process.env.PV_SRC;
const proj = process.env.PV_PROJ;
if (!src || !proj || !process.env.PI_VOICE_CONFIG) {
  console.error("seed-history: set PI_VOICE_CONFIG, PV_PROJ and PV_SRC");
  process.exit(2);
}
const load = (name) => import(pathToFileURL(join(src, name)).href);
const { Conversation } = await load("conversation.ts");
const { handoffHistoryTurn, workHistoryTurn } = await load("history.ts");
const warn = (message) => console.error(`warn: ${message}`);

// PV_SEED picks the earlier call: "memory" (default) names the project; "repeat"
// is the shape that made the Agent ignore the first live turn: an instruction-like
// exchange in Previous, then a finished handoff in Latest.
const seed = process.env.PV_SEED || "memory";
const first = new Conversation(proj, warn);
first.replay([]);
if (seed === "repeat") {
  const brief = "List the files in the current working directory and report their names back to the user.";
  first.record({ role: "user", text: "Hi, reply with just the single word hello." }, "voice");
  first.record({ role: "assistant", text: "Hello." }, "voice");
  first.record({ role: "user", text: "Please have Pi list the files in the current working directory and tell me their names." }, "voice");
  first.record(handoffHistoryTurn(brief), "work");
  first.record(workHistoryTurn(brief, "The current working directory contains:\n\n- `README.md`\n- `main.py`\n- `notes.txt`"), "work");
  first.record({ role: "assistant", text: "The current folder has three files: README.md, main.py, and notes.txt." }, "voice");
} else {
  const brief = "List the files in the project folder.";
  first.record({ role: "user", text: "Let's call this project Pelican Harbor." }, "voice");
  first.record({ role: "assistant", text: "Got it, Pelican Harbor it is." }, "voice");
  first.record({ role: "user", text: "Have Pi list the files in the project folder." }, "voice");
  first.record(handoffHistoryTurn(brief), "work");
  first.record(workHistoryTurn(brief, "Three files: README.md, notes.txt and main.py."), "work");
  first.record({ role: "assistant", text: "Pi found README.md, notes.txt and main.py." }, "voice");
}
first.close();

const reopened = new Conversation(proj, warn);
process.stdout.write(JSON.stringify(reopened.replay([])));
reopened.close();
