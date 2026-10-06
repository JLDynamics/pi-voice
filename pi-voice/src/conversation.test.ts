import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { it } from "node:test";
import { Conversation } from "./conversation.ts";

it("imports once, starts genuinely fresh, resumes the displayed selection, and survives restart", () => {
  const dir = mkdtempSync(join(tmpdir(), "voice-conversation-"));
  const previous = process.env.PI_VOICE_CONFIG;
  process.env.PI_VOICE_CONFIG = dir;
  const warnings: string[] = [];
  let conversation = new Conversation("/example", message => warnings.push(message));
  const legacy = [{ role: "user" as const, text: "old question" }, { role: "assistant" as const, text: "old answer" }];
  try {
    assert.deepEqual(conversation.replay(legacy), legacy);
    conversation.close();
    conversation = new Conversation("/example", message => warnings.push(message));
    assert.deepEqual(conversation.replay([]), legacy);
    const listed = conversation.list();
    assert.equal(listed.length, 1);
    conversation.fresh();
    assert.deepEqual(conversation.replay(legacy), []);
    conversation.record({ role: "user", text: "new question" }, "voice");
    conversation.record({ role: "assistant", text: "new answer" }, "voice");
    // Changing recency after displaying a list must not change what number 1 means.
    assert.equal(conversation.resume("1"), null);
    assert.deepEqual(conversation.replay([]), legacy);
    assert.deepEqual(warnings, []);
  } finally {
    conversation.close();
    if (previous === undefined) delete process.env.PI_VOICE_CONFIG;
    else process.env.PI_VOICE_CONFIG = previous;
    rmSync(dir, { recursive: true, force: true });
  }
});
