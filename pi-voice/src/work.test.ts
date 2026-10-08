import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { asUser, asWork, fullResult, RESULT_MAX } from "./voice.ts";
import { jobIdFromText, jobPrompt, withVoiceContext } from "./work.ts";

describe("jobPrompt", () => {
  it("is only the brief and the job id, with the id last", () => {
    const prompt = jobPrompt(asWork("w1"), asUser("read the file"));
    assert.equal(prompt, "read the file\n\n[Pi voice job id: w1]");
    assert.equal(jobIdFromText(prompt), asWork("w1"));
    assert.doesNotMatch(prompt, /Background context|Latest:|Previous:/);
  });
});

describe("withVoiceContext", () => {
  const pack = [
    "[Background context from the shared conversation. It may be incomplete or stale.]",
    "",
    "Latest:",
    "2026-10-08T18:23:59.000Z User: search the latest AI news",
    "",
    "Previous:",
    `2026-10-08T17:20:00.000Z Pi: [Pi result "AI news"] ${"long finding. ".repeat(400).trim()}`,
  ].join("\n");
  const user = (text: string) => ({ role: "user", content: [{ type: "text", text }], timestamp: 1 });

  it("puts the whole pack in front of the brief for the model request only", () => {
    const visible = jobPrompt(asWork("w2"), asUser("search the latest AI news"));
    const messages = [user(jobPrompt(asWork("w1"), asUser("older"))), { role: "assistant", content: [] }, user(visible)];
    const out = withVoiceContext(messages, asWork("w2"), pack);
    assert.ok(out);
    const text = out[2].content.map((part: { text: string }) => part.text).join("");
    // Nothing is trimmed: the model gets the same pack the old visible message carried.
    assert.equal(text, `${pack}\n\n${visible}`);
    assert.equal(jobIdFromText(text), asWork("w2"));
    // The other messages and the original array are untouched.
    assert.equal(out[0], messages[0]);
    assert.equal(out[1], messages[1]);
    assert.equal(messages[2].content.length, 1);
    assert.equal(messages[2].content[0].text, visible);
  });

  it("handles string content and leaves unrelated or empty cases alone", () => {
    const visible = jobPrompt(asWork("w1"), asUser("check"));
    const out = withVoiceContext([{ role: "user", content: visible }], asWork("w1"), pack);
    assert.equal(out?.[0].content[0].text, `${pack}\n\n${visible}`);
    assert.equal(withVoiceContext([user(visible)], asWork("other"), pack), undefined);
    assert.equal(withVoiceContext([user(visible)], asWork("w1"), "  "), undefined);
    // An assistant message quoting the id is not the job message.
    assert.equal(withVoiceContext([{ role: "assistant", content: [{ type: "text", text: visible }] }], asWork("w1"), pack), undefined);
  });
});

describe("fullResult", () => {
  it("passes Pi's answer through whole, so Luna decides what matters", () => {
    const answer = "Tesla is around $420.\n---\nmaps and URLs";
    assert.equal(fullResult(answer), answer);
  });

  it("keeps the detail that used to be cut after two sentences", () => {
    const answer = "I checked three files. A couple of things stood out. The timeout is 1.2 seconds.";
    const speak = fullResult(answer);
    assert.ok(speak?.includes("1.2 seconds"), "the finding must survive");
  });

  it("keeps code fences, which used to be deleted outright", () => {
    const speak = fullResult("The value is here:\n```\nVAD_MIN_SILENCE_MS=1200\n```");
    assert.ok(speak?.includes("VAD_MIN_SILENCE_MS=1200"));
  });

  it("returns nothing for an empty answer", () => {
    assert.equal(fullResult("   \n  "), undefined);
  });

  it("guards against a runaway dump at RESULT_MAX", () => {
    const dump = `${"word ".repeat(4000)}end.`;
    const speak = fullResult(dump);
    assert.ok(typeof speak === "string");
    assert.ok([...speak].length <= RESULT_MAX);
  });
});
