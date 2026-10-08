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

const first = new Conversation(proj, warn);
first.replay([]);
const brief = "List the files in the project folder.";
first.record({ role: "user", text: "Let's call this project Pelican Harbor." }, "voice");
first.record({ role: "assistant", text: "Got it, Pelican Harbor it is." }, "voice");
first.record({ role: "user", text: "Have Pi list the files in the project folder." }, "voice");
first.record(handoffHistoryTurn(brief), "work");
first.record(workHistoryTurn(brief, "Three files: README.md, notes.txt and main.py."), "work");
first.record({ role: "assistant", text: "Pi found README.md, notes.txt and main.py." }, "voice");
first.close();

const reopened = new Conversation(proj, warn);
process.stdout.write(JSON.stringify(reopened.replay([])));
reopened.close();
