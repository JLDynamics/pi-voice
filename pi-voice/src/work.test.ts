import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { asUser, asWork, fullResult, RESULT_MAX } from "./voice.ts";
import { DELEGATION_TYPE, delegationsFromBranch, jobIdFromText, jobPrompt, withDelegations } from "./work.ts";

describe("jobPrompt", () => {
  it("is only the brief and the job id, with the id last", () => {
    const prompt = jobPrompt(asWork("w1"), asUser("read the file"));
    assert.equal(prompt, "read the file\n\n[Pi voice job id: w1]");
    assert.equal(jobIdFromText(prompt), asWork("w1"));
    assert.doesNotMatch(prompt, /Background context|Latest:|Previous:|realtime_delegation/);
  });
});

describe("withDelegations", () => {
  const user = (text: string) => ({ role: "user", content: [{ type: "text", text }], timestamp: 1 });
  const first = "<realtime_delegation>\n  <input>older</input>\n  <transcript_delta>user: older</transcript_delta>\n</realtime_delegation>";
  const second = "<realtime_delegation>\n  <input>search the latest AI news</input>\n  <transcript_delta>user: what is new\nuser: search the latest AI news</transcript_delta>\n</realtime_delegation>";

  it("swaps every voice job message for its delegation, job id last, on a copy", () => {
    const visible = jobPrompt(asWork("w2"), asUser("search the latest AI news"));
    const messages = [user(jobPrompt(asWork("w1"), asUser("older"))), { role: "assistant", content: [] }, user(visible), user("typed by hand")];
    const out = withDelegations(messages, new Map([[asWork("w1"), first], [asWork("w2"), second]]));
    assert.ok(out);
    const text = (at: number) => (out[at].content as { text: string }[]).map((part) => part.text).join("");
    // Earlier handoffs keep their delegation in later requests, as in Codex.
    assert.equal(text(0), `${first}\n\n[Pi voice job id: w1]`);
    assert.equal(text(2), `${second}\n\n[Pi voice job id: w2]`);
    assert.equal(jobIdFromText(text(2)), asWork("w2"));
    assert.equal(out[1], messages[1]);
    assert.equal(out[3], messages[3]);
    // The session's own messages are untouched.
    assert.equal(messages[2].content[0].text, visible);
  });

  it("handles string content and leaves unrelated cases alone", () => {
    const visible = jobPrompt(asWork("w1"), asUser("check"));
    const map = new Map([[asWork("w1"), first]]);
    const textParts = (content: unknown) => content as { text: string }[];
    assert.equal(textParts(withDelegations([{ role: "user", content: visible }], map)?.[0].content)[0].text, `${first}\n\n[Pi voice job id: w1]`);
    assert.equal(withDelegations([user(visible)], new Map([[asWork("other"), first]])), undefined);
    assert.equal(withDelegations([user(visible)], new Map()), undefined);
    // An assistant message quoting the id is not the job message.
    assert.equal(withDelegations([{ role: "assistant", content: [{ type: "text", text: visible }] }], map), undefined);
  });

  it("reads stored delegations from hidden session entries", () => {
    const branch = [
      { type: "custom", customType: DELEGATION_TYPE, data: { id: "w1", text: first } },
      { type: "custom", customType: "pi-voice-face", data: { kind: "heard", text: "hi" } },
      { type: "custom", customType: DELEGATION_TYPE, data: { id: "w2", text: second } },
    ];
    assert.deepEqual([...delegationsFromBranch(branch as never)], [["w1", first], ["w2", second]]);
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
