import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { fullResult, RESULT_MAX } from "./voice.ts";

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
