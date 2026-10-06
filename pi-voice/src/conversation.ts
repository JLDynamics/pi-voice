import { HistoryStore, historyDbFile, listConversations, resolveConversation, type TurnKind } from "./history.ts";
import { boundHistory, loadConversation, newConversationId, storeConversation, type VoiceHistoryTurn } from "./session.ts";

/** Owns selection, migration and replay; callers never interpret an empty cabinet. */
export class Conversation {
  private store: HistoryStore | null = null;
  private id: string | null = null;
  private listed: string[] = [];
  private warned = false;

  private readonly cwd: string;
  private readonly warn: (message: string) => void;

  constructor(cwd: string, warn: (message: string) => void) {
    this.cwd = cwd;
    this.warn = warn;
  }

  private notice(message: string): void {
    if (this.warned) return;
    this.warned = true;
    this.warn(message);
  }

  replay(legacy: VoiceHistoryTurn[]): VoiceHistoryTurn[] {
    try {
      if (!this.store) {
        const saved = loadConversation(this.cwd);
        const id = saved ?? newConversationId();
        const store = HistoryStore.open(historyDbFile(id));
        this.store = store;
        this.id = id;
        store.setMetaOnce("cwd", this.cwd);
        // Only first adoption imports Pi's branch, never an explicitly new/resumed cabinet.
        if (!saved && !store.getMeta("initialized")) {
          for (const turn of legacy) store.append(turn);
        }
        store.setMetaOnce("initialized", "true");
        storeConversation(this.cwd, id);
      }
      return boundHistory(this.store.loadReplay());
    } catch (error) {
      this.close();
      this.notice(`Voice history unavailable, using session only: ${String(error)}`);
      return boundHistory(legacy);
    }
  }

  record(turn: VoiceHistoryTurn, kind: TurnKind): void {
    try { this.store?.append(turn, kind); }
    catch (error) { this.notice(`Voice history is not being saved: ${String(error)}`); }
  }

  fresh(): void { this.select(newConversationId()); }

  list() {
    const items = listConversations(10);
    this.listed = items.map(item => item.id);
    return items;
  }

  resume(selector: string): string | null {
    const resolved = resolveConversation(selector, this.listed);
    if ("error" in resolved) return resolved.error;
    // Validate before discarding the current cabinet.
    const check = HistoryStore.peek(historyDbFile(resolved.id));
    check.close();
    this.select(resolved.id);
    return null;
  }

  private select(id: string): void {
    const next = HistoryStore.open(historyDbFile(id));
    next.setMetaOnce("cwd", this.cwd);
    next.setMetaOnce("initialized", "true");
    this.close();
    this.store = next;
    this.id = id;
    this.warned = false;
    storeConversation(this.cwd, id);
  }

  close(): void {
    this.store?.close();
    this.store = null;
    this.id = null;
  }
}
