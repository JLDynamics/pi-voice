// Which conversation a working directory opens. The durable thread is
// history-<id>.sqlite; this file is only the folder → id map. Resume lists
// every thread by id, so a different folder does not split memory.
// Mirrors the repo's secrets convention: ~/.config origin, owner-only.

import { mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { homedir } from "node:os";
import { join } from "node:path";

export type StoredSession = {
  /** Luna's current conversation id per folder; her history database is named after it. */
  conversations?: Record<string, string>;
};

export type VoiceHistoryTurn = { role: "user" | "assistant"; text: string };

/**
 * Safety bound on the history handed to the voice child through an env var.
 * HistoryStore already bounds the replay itself (VOICE_REPLAY_KB, at most 80 turns),
 * so this should never trim; it only guards against a runaway value.
 */
const REPLAY_ENV_BYTES = 64_000;
/** One startup pack must survive; the env byte cap still bounds the payload. */
const REPLAY_TURN_CHARS = 64_000;

export function boundHistory(history: VoiceHistoryTurn[]): VoiceHistoryTurn[] {
  const turns = history.map(turn => ({ ...turn, text: turn.text.slice(0, REPLAY_TURN_CHARS) }));
  let drop = 0;
  const sizeFrom = (start: number) => Buffer.byteLength(JSON.stringify(turns.slice(start)), "utf8");
  // Keep the newest turn and shorten it below, instead of discarding the whole pack.
  while (sizeFrom(drop) > REPLAY_ENV_BYTES && drop < turns.length - 1) drop++;
  while (drop < turns.length && turns[drop].role === "assistant") drop++;
  const kept = turns.slice(drop);
  while (
    kept.length === 1 &&
    Buffer.byteLength(JSON.stringify(kept), "utf8") > REPLAY_ENV_BYTES &&
    kept[0].text.length > 0
  ) {
    kept[0] = { ...kept[0], text: kept[0].text.slice(0, Math.floor(kept[0].text.length * 0.9)) };
  }
  return kept;
}

export function configDir(): string {
  return process.env.PI_VOICE_CONFIG || join(homedir(), ".config", "pi-voice");
}

export function sessionFile(): string {
  return join(configDir(), "session.json");
}

export function atomicWriteJson(filePath: string, data: unknown): void {
  mkdirSync(configDir(), { recursive: true, mode: 0o700 });
  const tmpPath = `${filePath}.tmp.${process.pid}.${Date.now()}`;
  writeFileSync(tmpPath, `${JSON.stringify(data, null, 2)}\n`, { mode: 0o600 });
  try {
    renameSync(tmpPath, filePath);
  } catch {
    try {
      writeFileSync(filePath, `${JSON.stringify(data, null, 2)}\n`, { mode: 0o600 });
    } finally {
      try {
        unlinkSync(tmpPath);
      } catch {}
    }
  }
}

/** A conversation id is a file-name-safe label (a UUID unless someone chose a name). */
export function newConversationId(): string {
  return randomUUID();
}

export function isConversationId(value: unknown): value is string {
  return typeof value === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(value);
}

export function loadConversation(cwd: string): string | null {
  try {
    const parsed = JSON.parse(readFileSync(sessionFile(), "utf8")) as Partial<StoredSession>;
    const id = parsed.conversations?.[cwd];
    if (isConversationId(id)) return id;
  } catch {
    // missing or corrupt: the caller starts a new conversation
  }
  return null;
}

export function storeConversation(cwd: string, conversationId: string): void {
  try {
    let existing: Partial<StoredSession> = {};
    try {
      existing = JSON.parse(readFileSync(sessionFile(), "utf8")) as Partial<StoredSession>;
    } catch {}
    atomicWriteJson(sessionFile(), {
      ...existing,
      conversations: { ...(existing.conversations ?? {}), [cwd]: conversationId },
    });
  } catch {
    // persistence is best-effort; the in-memory conversation still works
  }
}
