// Persistent voice conversations: one conversation id per working directory,
// surviving voice calls (and reboots) until /voice new resets it.
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

export function boundHistory(history: VoiceHistoryTurn[]): VoiceHistoryTurn[] {
  const turns = history.map(turn => ({ ...turn, text: turn.text.slice(0, 4096) }));
  let size = Buffer.byteLength(JSON.stringify(turns), "utf8");
  let drop = 0;
  while (size > REPLAY_ENV_BYTES && drop < turns.length) {
    size -= Buffer.byteLength(JSON.stringify(turns[drop]), "utf8") + 1;
    drop++;
  }
  while (drop < turns.length && turns[drop].role === "assistant") drop++;
  return turns.slice(drop);
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
