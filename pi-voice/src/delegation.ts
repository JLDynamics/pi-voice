// Codex-style handoff context for Pi.
//
// Mirrors openai/codex realtime delegation:
// - codex-api/src/endpoint/realtime_websocket/methods.rs: the active transcript is only
//   what the voice line transcribed (user speech in, assistant speech out). It is
//   capped at 8 KiB by dropping the oldest entries, the brief is appended as a user
//   entry on handoff, and the handoff takes the whole list (reset for the next one).
// - core/src/realtime_conversation.rs `realtime_transcript_delta`: entries render as
//   `role: text` lines.
// - core/src/context/realtime_delegation.rs: `<realtime_delegation>` with an
//   XML-escaped `<input>` (first 4 KiB) and `<transcript_delta>` (last 4 KiB).
//
// Pure: the reducer keeps the entries on the voice state; the extension stores each
// rendered delegation in Pi's session as a hidden custom entry and swaps it in for
// the job's message on every model request (see `withDelegations` in work.ts).

import { Buffer } from "node:buffer";

export type TranscriptRole = "user" | "assistant";

/** One transcribed turn. `id` is the voice server's item id, used to apply revisions. */
export type TranscriptEntry = { role: TranscriptRole; text: string; id?: string };

export const MAX_ACTIVE_TRANSCRIPT_BYTES = 8 * 1024;
export const MAX_REALTIME_DELEGATION_FIELD_BYTES = 4 * 1024;
const TRUNCATION_MARKER = "…";
const MARKER_BYTES = Buffer.byteLength(TRUNCATION_MARKER);

const bytes = (text: string): number => Buffer.byteLength(text);

/** `role.len + text.len + 3`, as Codex counts an entry (`"role: text\n"` with slack). */
function entryBytes(entry: TranscriptEntry): number {
  return bytes(entry.role) + bytes(entry.text) + 3;
}

/** The first `max` bytes, cut back to a character boundary. */
function headBytes(text: string, max: number): string {
  const buf = Buffer.from(text);
  if (buf.length <= max) return text;
  let end = max;
  while (end > 0 && (buf[end] & 0xc0) === 0x80) end -= 1;
  return buf.subarray(0, end).toString();
}

/** The last `max` bytes, moved forward to a character boundary. */
function tailBytes(text: string, max: number): string {
  const buf = Buffer.from(text);
  if (buf.length <= max) return text;
  let start = buf.length - max;
  while (start < buf.length && (buf[start] & 0xc0) === 0x80) start += 1;
  return buf.subarray(start).toString();
}

/**
 * Codex `truncate_active_transcript`: drop the oldest entries while over 8 KiB,
 * then, if the one left is still too long, keep the end of its text behind "…".
 */
export function truncateActiveTranscript(entries: readonly TranscriptEntry[]): TranscriptEntry[] {
  const out = entries.slice();
  let total = out.reduce((sum, entry) => sum + entryBytes(entry), 0);
  while (total > MAX_ACTIVE_TRANSCRIPT_BYTES && out.length > 1) {
    total -= entryBytes(out.shift()!);
  }
  if (total <= MAX_ACTIVE_TRANSCRIPT_BYTES || out.length === 0) return out;
  const first = out[0];
  const maxText = MAX_ACTIVE_TRANSCRIPT_BYTES - (bytes(first.role) + 3);
  if (bytes(first.text) > maxText) {
    out[0] = { ...first, text: TRUNCATION_MARKER + tailBytes(first.text, Math.max(0, maxText - MARKER_BYTES)) };
  }
  return out;
}

/**
 * A whole-text transcript revision (Codex `apply_transcript_done`). The same item id
 * replaces its entry; a new id (or none) starts a new entry.
 */
export function setTranscript(entries: readonly TranscriptEntry[], role: TranscriptRole, text: string, id?: string): TranscriptEntry[] {
  if (!text) return entries.slice();
  const at = id ? entries.findIndex((entry) => entry.id === id && entry.role === role) : -1;
  const next = entries.slice();
  if (at >= 0) next[at] = { ...next[at], text };
  else next.push(id ? { role, text, id } : { role, text });
  return truncateActiveTranscript(next);
}

/** A streamed piece of speech (Codex `append_transcript_delta`). */
export function appendTranscript(entries: readonly TranscriptEntry[], role: TranscriptRole, delta: string, id?: string): TranscriptEntry[] {
  if (!delta) return entries.slice();
  const at = id ? entries.findIndex((entry) => entry.id === id && entry.role === role) : -1;
  const next = entries.slice();
  if (at >= 0) next[at] = { ...next[at], text: next[at].text + delta };
  else next.push(id ? { role, text: delta, id } : { role, text: delta });
  return truncateActiveTranscript(next);
}

/**
 * Codex `append_handoff_input` + `mem::take`: the brief joins the delta as a user
 * entry unless the user said exactly that, and the whole delta is handed off.
 * The caller starts the next delta empty.
 */
export function takeHandoffTranscript(entries: readonly TranscriptEntry[], input: string): TranscriptEntry[] {
  const out = entries.slice();
  const brief = input.trim();
  if (brief && !out.some((entry) => entry.role === "user" && entry.text.trim() === brief)) {
    out.push({ role: "user", text: brief });
  }
  return out;
}

/** Codex `realtime_transcript_delta`: `role: text` lines, or undefined when empty. */
export function transcriptDelta(entries: readonly TranscriptEntry[]): string | undefined {
  const lines = entries.map((entry) => `${entry.role}: ${entry.text}`);
  return lines.length ? lines.join("\n") : undefined;
}

function escapeXml(text: string): string {
  return text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

/** Escape, then keep the start (`<input>`) or the end (`<transcript_delta>`) within 4 KiB. */
function boundedField(text: string, retain: "start" | "end"): string {
  const escaped = escapeXml(text);
  if (bytes(escaped) <= MAX_REALTIME_DELEGATION_FIELD_BYTES) return escaped;
  const keep = MAX_REALTIME_DELEGATION_FIELD_BYTES - MARKER_BYTES;
  return retain === "start"
    ? headBytes(escaped, keep) + TRUNCATION_MARKER
    : TRUNCATION_MARKER + tailBytes(escaped, keep);
}

/** Codex `RealtimeDelegation::render` for a handoff. */
export function renderDelegation(input: string, delta?: string): string {
  let body = `\n  <input>${boundedField(input, "start")}</input>\n`;
  if (delta) body += `  <transcript_delta>${boundedField(delta, "end")}</transcript_delta>\n`;
  return `<realtime_delegation>${body}</realtime_delegation>`;
}

/** What Pi's model gets for one handoff (Codex `realtime_delegation_from_handoff`). */
export function delegationForHandoff(entries: readonly TranscriptEntry[], brief: string): string {
  const delta = transcriptDelta(takeHandoffTranscript(entries, brief));
  const input = brief.trim() || delta || "";
  return renderDelegation(input, delta);
}
