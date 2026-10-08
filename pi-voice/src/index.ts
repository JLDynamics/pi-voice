import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { AGENT_LABEL, Voice, detectMuteChord } from "./voice.ts";
import { jobIdFromText, toolProgress } from "./work.ts";

export default function (pi: ExtensionAPI): void {
  const voice = Voice.attach(pi);
  let announcedAnswer = false;
  const partialTools = new Set<string>();

  pi.registerCommand("voice", {
    description: `Talk in this session. ${AGENT_LABEL} hears and speaks. Second /voice stops.`,
    getArgumentCompletions: (prefix) => {
      const items = voice.completions(prefix);
      return items.length > 0 ? items : null;
    },
    handler: async (args, ctx) => {
      // MUST return. Awaiting Voice ready freezes Enter.
      voice.slash(args, ctx);
    },
  });

  // Never ctrl+m (ASCII 13 / Enter). ctrl+shift+m only if Kitty or
  // modifyOtherKeys is already active. Else alt+m (legacy ESC m).
  pi.registerShortcut(detectMuteChord(), {
    description: `Mute or unmute ${AGENT_LABEL}'s mic`,
    handler: (ctx) => voice.slash("mute", ctx),
  });

  pi.on("input", (event, ctx) => voice.onInput(event, ctx));
  pi.on("before_agent_start", (event, ctx) => voice.onBeforeAgentStart(event, ctx));
  // Each voice job reaches the model as its <realtime_delegation>, not the chat.
  pi.on("context", (event, ctx) => voice.onContext(event, ctx));
  pi.on("message_start", (event, ctx) => {
    if (event.message.role === "assistant") {
      announcedAnswer = false;
      return;
    }
    if (event.message.role !== "user") return;
    const content = event.message.content;
    const text = typeof content === "string" ? content : content
      .filter((part) => part.type === "text")
      .map((part) => part.text).join("");
    const id = jobIdFromText(text);
    if (id) voice.feed({ tag: "jobMessage", id }, ctx);
  });
  pi.on("agent_settled", (_event, ctx) => voice.feed({ tag: "agentSettled" }, ctx));
  pi.on("tool_execution_start", (event, ctx) => {
    partialTools.delete(event.toolCallId);
    voice.feed({ tag: "jobProgress", note: toolProgress("start", event.toolName) }, ctx);
  });
  pi.on("tool_execution_update", (event, ctx) => {
    if (partialTools.has(event.toolCallId)) return;
    partialTools.add(event.toolCallId);
    voice.feed({ tag: "jobProgress", note: toolProgress("update", event.toolName) }, ctx);
  });
  pi.on("tool_execution_end", (event, ctx) => {
    partialTools.delete(event.toolCallId);
    voice.feed({ tag: "jobProgress", note: toolProgress(event.isError ? "error" : "end", event.toolName) }, ctx);
  });
  pi.on("message_update", (event, ctx) => {
    if (event.message.role !== "assistant" || !Array.isArray(event.message.content)) return;
    const answer = event.message.content.filter((part) => part.type === "text").map((part) => part.text).join("").trim();
    if (!answer) return;
    if (!announcedAnswer) {
      announcedAnswer = true;
      voice.feed({ tag: "jobProgress", note: "Pi is writing the answer" }, ctx);
    }
  });
  pi.on("session_shutdown", () => voice.feed({ tag: "shutdown" }));
}
