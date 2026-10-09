import { AGENT_LABEL, type Strip, type VoiceState } from "./voice-core.ts";

export function detectMuteChord(): "alt+m" | "ctrl+shift+m" {
  const term = `${process.env.TERM ?? ""}\n${process.env.TERM_PROGRAM ?? ""}`.toLowerCase();
  if (term.includes("kitty") || Boolean(process.env.KITTY_WINDOW_ID)) return "ctrl+shift+m";
  return "alt+m";
}

export function muteHint(): string {
  return `/voice mute   ${detectMuteChord()}   /voice stop`;
}

/** Pi crashes if any custom render line is wider than the terminal. */
export function faceLines(who: string, text: string, width: number): string[] {
  const w = Math.max(1, Math.floor(width));
  const body = String(text).replace(/\s+/g, " ").trim();
  const full = body ? `${who}  ${body}` : who;
  return wrapToWidth(full, w);
}

/** Show saved voice entries in Pi's ordinary transcript, including during a call. */
export function sessionFaceLines(who: string, text: string, width: number, voiceActive: boolean): string[] {
  void voiceActive;
  return faceLines(who, text, width);
}

/** Keep the live reply in the transcript, with paragraphs and a stable label. */
export function spokenFaceLines(text: string, width: number): string[] {
  return text.split(/\r?\n/).flatMap((paragraph, index) =>
    wrapToWidth(`${index === 0 ? `${AGENT_LABEL}  ` : " ".repeat(AGENT_LABEL.length + 2)}${paragraph}`, Math.max(1, Math.floor(width))));
}

export function wrapToWidth(text: string, width: number): string[] {
  const words = text.split(/(\s+)/);
  const lines: string[] = [];
  let current = "";
  for (const word of words) {
    if (!word) continue;
    if (current.length + word.length <= width) {
      current += word;
      continue;
    }
    if (current.trim()) lines.push(current.trimEnd());
    let rest = word.trimStart();
    while (rest.length > width) {
      lines.push(rest.slice(0, width));
      rest = rest.slice(width);
    }
    current = rest;
  }
  if (current) lines.push(current);
  return lines.length > 0 ? lines : [""];
}

export function strip(state: VoiceState, now: number = Date.now()): Strip | null {
  if (state.tag === "off") return null;
  if (state.tag === "connecting") {
    return { line1: "voice  …  connecting", line2: muteHint() };
  }
  if (state.mic.tag === "closed") {
    return {
      line1: "voice  ◌  muted",
      line2: `/voice unmute   ${detectMuteChord()}   /voice stop`,
    };
  }
  // Speech recognition still waits for an audio segment before it supplies text.
  const hearing = state.heardPending ? "  ·  hearing you" : "";
  if (state.job.tag === "running") {
    return { line1: `voice  ●  talking · pi working · ${elapsedShort(now - state.job.startedAt)}${hearing}`, line2: muteHint() };
  }
  return { line1: `voice  ●  talking${hearing}`, line2: muteHint() };
}

export function elapsedShort(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m${s % 60}s`;
}
