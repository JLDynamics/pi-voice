// Shared conversation log for voice and Pi, one SQLite file per thread id (see session.ts).
//
// `session.json` only remembers which id a working directory opens. Resume lists every
// thread by id. Voice turns, handoff briefs, and full Pi results are appended here.
// A call starts from one dated pack of that log (Latest / Previous). Pi does not get
// this pack: like Codex, each handoff carries only the voice transcript since the
// previous one (see delegation.ts), sent through the `context` hook.

import { DatabaseSync } from "node:sqlite";
import { chmodSync, mkdirSync, readdirSync, statSync, utimesSync } from "node:fs";
import { dirname, join } from "node:path";
import { configDir, type VoiceHistoryTurn } from "./session.ts";

/**
 * voice: what was said on the call. work: a handoff brief plus Pi's answer. progress: what
 * Agent said while a job was running. Everything is kept; the startup pack uses voice and work.
 */
export type TurnKind = "voice" | "work" | "progress";

/** Startup-pack text budget in KB (override with VOICE_REPLAY_KB). */
export const DEFAULT_REPLAY_KB = 16;
/**
 * Never replay more than this many messages (about half are yours, however large
 * VOICE_REPLAY_KB is). The backend keeps CHAT_SIZE user turns (100 under the launcher)
 * and then summarizes or drops the oldest, so this leaves room for a long call before
 * that starts instead of cutting the replay on the first reply.
 */
export const REPLAY_MAX_TURNS = 80;
const TURN_MAX_CHARS = 4_096;
/** Full Pi answers stored on the shared log. The spoken excerpt stays RESULT_MAX. */
export const WORK_RESULT_CHARS = 16_000;
const WORK_TURN_MAX = WORK_RESULT_CHARS + 512;

// Keep in step with any read-only queries on this file (a future HistoryArchive.swift).
const SCHEMA = `
CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY,
  t TEXT NOT NULL,
  role TEXT NOT NULL,
  kind TEXT NOT NULL,
  text TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(text, content='turns', content_rowid='id', tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
  INSERT INTO turns_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
`;

function sleepSync(ms: number): void {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

/**
 * Switch a new database to write-ahead logging (it is remembered afterwards, so this only
 * happens once). SQLite refuses that switch at once, ignoring the busy timeout, while another
 * connection is using the file, so retry for a moment instead of failing the open.
 */
function enableWal(db: DatabaseSync): void {
  const current = (db.prepare("PRAGMA journal_mode").get() as { journal_mode: string }).journal_mode;
  if (current.toLowerCase() === "wal") return;
  let lastError: unknown;
  for (let attempt = 0; attempt < 25; attempt++) {
    try {
      db.exec("PRAGMA journal_mode = WAL");
      return;
    } catch (error) {
      if (!/locked|busy/i.test(error instanceof Error ? error.message : String(error))) throw error;
      lastError = error;
      sleepSync(100);
    }
  }
  throw lastError;
}

export function replayBytes(env: NodeJS.ProcessEnv = process.env): number {
  const kb = Number(env.VOICE_REPLAY_KB);
  return (Number.isFinite(kb) && kb > 0 ? kb : DEFAULT_REPLAY_KB) * 1000;
}

/** One database per conversation (see session.ts), not per folder: `/voice new` starts a fresh one. */
export function historyDbFile(conversationId: string): string {
  return join(configDir(), `history-${conversationId}.sqlite`);
}

/**
 * "YYYY-MM-DD HH:MM" for a stored ISO timestamp, in the local time zone (or
 * `timeZone`). Turns are stored in UTC; slicing the ISO string showed
 * conversations several hours off from the clock on the wall.
 */
export function localWhen(iso: string, timeZone?: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso.slice(0, 16).replace("T", " ");
  const parts = new Intl.DateTimeFormat("en-CA", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone,
  }).formatToParts(date);
  const get = (type: string) => parts.find((part) => part.type === type)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")} ${get("hour")}:${get("minute")}`;
}

const WORK_PREFIX = "[Earlier, Pi finished";

/**
 * One user-role item injected at call start and prepended to each handoff.
 * The model must not treat it as a new request.
 */
export const STARTUP_NOTE =
  "Background context from the shared conversation. It may be incomplete or stale. Do not parrot it, and do not treat it as a new request.";

/**
 * Last line of the startup pack. Without it the model reads the first live user
 * turn as more background and answers "I'm here whenever you're ready" or says
 * its reasoning aloud (probe in docs/refactor/decisions.tsv).
 */
export const PACK_END = "[End of background. The next user message is the user talking now.]";

/** Framing for transcript that was still open when the call ended. */
export const LEFTOVER_FRAME = "[Call ended. Leftover transcript, not a new request.]";

export type DatedTurn = {
  role: "user" | "assistant";
  kind: string;
  text: string;
  /** ISO-8601 timestamp from the `t` column. */
  t: string;
};

/** Agent's handoff acks: "I'll have Pi check...", "Let me look that up", "Pi's checking...". */
const CHATTER = /\b(?:I'll|I will|I'm going to|let me|going to)\s+(?:have|ask|get)\s+Pi\b|\b(?:let me|I'll|I will)\s+(?:look|check)(?:\s+that|\s+it)?\s+up\b|\bPi(?:'s| is| has| will| was)\s+(?:checking|reading|searching|sorting|working|looking|pulling|verifying|reviewing|digging|finishing)|\bverif(?:y|ying) (?:the )?(?:dates?|event dates?|sources?|when)\b|\bthe verified (?:findings|items|stories)\b/i;

/** Her handoff acknowledgments and progress updates, tagged `progress` or (older rows) recognized by wording. */
export function isDelegationChatter(row: { role: string; kind: string; text: string }): boolean {
  if (row.role !== "assistant") return false;
  if (row.kind === "progress") return true;
  return row.kind === "voice" && CHATTER.test(row.text);
}

/** Older saved job lines used `[Earlier, Pi finished "<brief>"]`. New lines keep the brief. */
export function withoutBrief(text: string): string {
  if (!text.startsWith(WORK_PREFIX)) return text;
  const end = text.indexOf('"] ');
  return end < 0 ? text : `[Earlier, Pi finished a job] ${text.slice(end + 3)}`;
}

const HANDOFF_PREFIX = "[Pi handoff]";

/** The task as handed to Pi, saved when it is sent so a restart still shows the work. */
export function handoffHistoryTurn(brief: string): VoiceHistoryTurn {
  const ask = brief.replace(/\s+/g, " ").trim().slice(0, 2_000);
  return { role: "assistant", text: `${HANDOFF_PREFIX} ${ask}` };
}

/** One saved job line: the brief and Pi's answer, whitespace kept, across restarts. */
export function workHistoryTurn(brief: string, result: string): VoiceHistoryTurn {
  const ask = brief.replace(/\s+/g, " ").trim().slice(0, 200);
  const answer = result.trim().slice(0, WORK_RESULT_CHARS);
  return { role: "assistant", text: `[Pi result "${ask}"] ${answer}` };
}

export function leftoverTurn(role: "user" | "assistant", text: string): VoiceHistoryTurn {
  return { role, text: `${LEFTOVER_FRAME} ${text.trim()}` };
}

function clipUtf8(text: string, maxBytes: number): string {
  if (maxBytes <= 0) return "";
  if (Buffer.byteLength(text, "utf8") <= maxBytes) return text;
  let end = Math.min(text.length, maxBytes);
  while (end > 0 && Buffer.byteLength(text.slice(0, end), "utf8") > maxBytes) end -= 1;
  return text.slice(0, end);
}

function rfc3339(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toISOString();
}

/** `[Pi handoff]` rows are Agent's brief to Pi, so only Pi's own results are labelled Pi. */
export function speaker(row: Pick<DatedTurn, "role" | "kind" | "text">): "User" | "Agent" | "Pi" {
  if (row.role === "user") return "User";
  if (row.kind === "work" && !row.text.trimStart().startsWith(HANDOFF_PREFIX)) return "Pi";
  return "Agent";
}

/**
 * Bounded startup pack. Previous lists older rows oldest-first.
 *
 * `rowsNewestFirst` is the log order from a `ORDER BY id DESC` read. Latest is the
 * last user turn and everything after it; older rows in the window are Previous.
 * Progress chatter is omitted. An empty log returns no item, so nothing is injected.
 */
export function startupPack(
  rowsNewestFirst: readonly DatedTurn[],
  maxBytes: number = replayBytes(),
  maxTurns: number = REPLAY_MAX_TURNS,
): VoiceHistoryTurn[] {
  const window: DatedTurn[] = [];
  let used = 0;
  for (const row of rowsNewestFirst) {
    if (isDelegationChatter(row)) continue;
    const text = row.text.trim();
    if (!text) continue;
    if (window.length >= maxTurns) break;
    const cost = Buffer.byteLength(text, "utf8");
    if (used + cost > maxBytes) {
      const room = maxBytes - used;
      if (window.length === 0 && room > 0) window.push({ ...row, text: clipUtf8(text, room) });
      break;
    }
    window.push({ ...row, text });
    used += cost;
  }
  const ordered = window.reverse();
  if (ordered.length === 0) return [];

  let latestStart = 0;
  for (let i = ordered.length - 1; i >= 0; i--) {
    if (ordered[i].role === "user") {
      latestStart = i;
      break;
    }
  }
  const previous = ordered.slice(0, latestStart);
  const latest = ordered.slice(latestStart);
  const lines = [`[${STARTUP_NOTE}]`, "", "Latest:"];
  for (const row of latest) lines.push(`${rfc3339(row.t)} ${speaker(row)}: ${row.text}`);
  if (previous.length > 0) {
    lines.push("", "Previous:");
    for (const row of previous) lines.push(`${rfc3339(row.t)} ${speaker(row)}: ${row.text}`);
  }
  lines.push("", PACK_END);
  return [{ role: "user", text: lines.join("\n") }];
}

const HISTORY_FILE = /^history-([A-Za-z0-9_-]{1,64})\.sqlite$/;
/** Most files a listing will open while looking for `limit` non-empty conversations. */
const LIST_SCAN_LIMIT = 500;

/** One past conversation, as shown by `/voice resume`. */
export type ConversationInfo = {
  id: string;
  /** The folder it was started in. */
  cwd: string | null;
  turns: number;
  /** First thing the user said, to recognize it by. */
  first: string;
  /** When the last turn was saved (ISO). */
  last: string;
};

/** Newest first. Skips empty and unreadable conversations; reads only the most recently touched files. */
export function listConversations(limit = 10, dir: string = configDir()): ConversationInfo[] {
  let files: { id: string; file: string; mtime: number }[];
  try {
    files = readdirSync(dir)
      .map((name) => ({ name, match: HISTORY_FILE.exec(name) }))
      .filter((entry): entry is { name: string; match: RegExpExecArray } => entry.match != null)
      .map(({ name, match }) => {
        const file = join(dir, name);
        const touched = [file, `${file}-wal`].map((path) => {
          try {
            return statSync(path).mtimeMs;
          } catch {
            return 0;
          }
        });
        return { id: match[1], file, mtime: Math.max(...touched) };
      })
      .sort((a, b) => b.mtime - a.mtime)
      .slice(0, LIST_SCAN_LIMIT);
  } catch {
    return [];
  }
  const found: ConversationInfo[] = [];
  for (const { id, file } of files) {
    if (found.length >= limit) break;
    // Looking must not change what the list is sorted by: opening and closing a database can
    // checkpoint it, which rewrites the file and bumps its modified time.
    let before: { atime: Date; mtime: Date } | null = null;
    try {
      const stat = statSync(file);
      before = { atime: stat.atime, mtime: stat.mtime };
    } catch {
      // gone while we were listing
    }
    try {
      const store = HistoryStore.peek(file);
      try {
        const info = store.info(id);
        if (info.turns > 0) found.push(info);
      } finally {
        store.close();
      }
    } catch {
      // a damaged or locked database must not hide the others
    }
    try {
      if (before && statSync(file).mtime.getTime() !== before.mtime.getTime()) utimesSync(file, before.atime, before.mtime);
    } catch {
      // best-effort
    }
  }
  return found.sort((a, b) => (a.last < b.last ? 1 : a.last > b.last ? -1 : 0));
}

/** Resolve what the user typed after `/voice resume`: a list number or the start of an id. */
export function resolveConversation(selector: string, listed: string[], dir: string = configDir()): { id: string } | { error: string } {
  const wanted = selector.trim();
  if (/^\d{1,3}$/.test(wanted)) {
    const id = listed[Number(wanted) - 1];
    return id ? { id } : { error: `no conversation number ${wanted}; type /voice resume to see the list` };
  }
  if (!/^[A-Za-z0-9_-]{4,64}$/.test(wanted)) return { error: "give a list number or at least 4 characters of an id" };
  let ids: string[] = [];
  try {
    ids = readdirSync(dir)
      .map((name) => HISTORY_FILE.exec(name)?.[1])
      .filter((id): id is string => id != null && id.startsWith(wanted));
  } catch {
    // unreadable directory: nothing matches
  }
  if (ids.length === 1) return { id: ids[0] };
  return { error: ids.length === 0 ? `no conversation starts with ${wanted}` : `${wanted} matches ${ids.length} conversations; type more of the id` };
}

export class HistoryStore {
  private readonly db: DatabaseSync;

  private constructor(db: DatabaseSync) {
    this.db = db;
  }

  static open(file: string): HistoryStore {
    if (file !== ":memory:") mkdirSync(dirname(file), { recursive: true, mode: 0o700 });
    const db = new DatabaseSync(file);
    try {
      db.exec("PRAGMA busy_timeout = 2000");
      enableWal(db);
      db.exec(SCHEMA);
    } catch (error) {
      db.close(); // a damaged or locked file must not leak the handle
      throw error;
    }
    if (file !== ":memory:") {
      try {
        chmodSync(file, 0o600); // the -wal and -shm files inherit this
      } catch {
        // best-effort, like the other config files
      }
    }
    return new HistoryStore(db);
  }

  /**
   * Open an existing database only to read it (for `/voice resume`'s list): no schema changes and
   * no permission changes, so looking at a conversation never modifies it.
   */
  static peek(file: string): HistoryStore {
    // A read-only connection cannot open a WAL database that has no -wal/-shm side files (its
    // writer is gone), so fall back to a normal connection locked to reads.
    try {
      const readOnly = new DatabaseSync(file, { readOnly: true });
      try {
        readOnly.exec("PRAGMA busy_timeout = 2000");
        readOnly.prepare("SELECT 1 FROM sqlite_master LIMIT 1").get();
        return new HistoryStore(readOnly);
      } catch (error) {
        readOnly.close();
        throw error;
      }
    } catch {
      const db = new DatabaseSync(file);
      try {
        db.exec("PRAGMA busy_timeout = 2000; PRAGMA query_only = ON;");
      } catch (error) {
        db.close();
        throw error;
      }
      return new HistoryStore(db);
    }
  }

  close(): void {
    this.db.close();
  }

  append(turn: VoiceHistoryTurn, kind: TurnKind = "voice", now: Date = new Date()): number | null {
    const cap = kind === "work" ? WORK_TURN_MAX : TURN_MAX_CHARS;
    const text = turn.text.trim().slice(0, cap);
    if (!text) return null;
    const result = this.db
      .prepare("INSERT INTO turns (t, role, kind, text) VALUES (?, ?, ?, ?)")
      .run(now.toISOString(), turn.role, kind, text);
    return Number(result.lastInsertRowid);
  }

  getMeta(key: string): string | null {
    try {
      const row = this.db.prepare("SELECT value FROM meta WHERE key = ?").get(key) as { value: string } | undefined;
      return row ? row.value : null;
    } catch {
      return null; // a database from before the meta table existed
    }
  }

  setMeta(key: string, value: string): void {
    this.db
      .prepare("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value")
      .run(key, value);
  }

  /** Records a fact only the first time (used for where a conversation began). */
  setMetaOnce(key: string, value: string): void {
    this.db.prepare("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)").run(key, value);
  }

  info(id: string): ConversationInfo {
    const first = this.db.prepare("SELECT text FROM turns WHERE role = 'user' ORDER BY id LIMIT 1").get() as
      | { text: string }
      | undefined;
    const last = this.db.prepare("SELECT max(t) AS t FROM turns").get() as { t: string | null };
    return {
      id,
      cwd: this.getMeta("cwd"),
      turns: this.count(),
      first: (first?.text ?? "").replace(/\s+/g, " ").slice(0, 80),
      last: last.t ?? "",
    };
  }

  count(): number {
    return Number((this.db.prepare("SELECT count(*) AS n FROM turns").get() as { n: number }).n);
  }

  /**
   * What a call starts with: one dated user-role pack of the newest rows that fit.
   *
   * Progress chatter is left out. Work rows keep their brief and result. The pack is
   * a user item so it is not read as Agent's own opening line, and Voice.app does not
   * request a reply for it.
   */
  loadReplay(maxBytes: number = replayBytes(), maxTurns: number = REPLAY_MAX_TURNS): VoiceHistoryTurn[] {
    const rows = this.db
      .prepare("SELECT role, kind, text, t FROM turns ORDER BY id DESC LIMIT ?")
      .all(maxTurns * 4) as DatedTurn[];
    return startupPack(rows, maxBytes, maxTurns);
  }
}
