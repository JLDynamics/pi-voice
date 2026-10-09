import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import {
  headlessChildEnv,
  JOB_UPDATE_STATUSES,
  parseLine,
  toVoice,
  type HeadlessIn,
  type JobUpdateStatus,
  type VoiceHistoryTurn,
} from "./child.ts";

const CONTRACT_PATH = join(dirname(fileURLToPath(import.meta.url)), "../../contracts/pi-voice.json");
const contract = JSON.parse(readFileSync(CONTRACT_PATH, "utf8")) as {
  enums: {
    jobStatus: string[];
    historyRole: string[];
    [key: string]: string[];
  };
  stdio: {
    toVoice: Record<string, Record<string, string>>;
    fromVoice: Record<string, Record<string, string>>;
  };
  voiceHistory: {
    env: string;
    turn: Record<string, string>;
  };
  tools: Record<string, Record<string, string>>;
  channels: Record<string, string>;
};

function sampleValuesForType(fieldName: string, typeDef: string, enums: Record<string, string[]>): unknown[] {
  const isOptional = typeDef.endsWith("?");
  const baseType = isOptional ? typeDef.slice(0, -1) : typeDef;
  if (baseType === "string") {
    return [`sample-${fieldName}`];
  }
  if (baseType === "boolean") {
    return [true, false];
  }
  if (baseType in enums) {
    return [...enums[baseType]!];
  }
  throw new Error(`Unknown type '${typeDef}' for field '${fieldName}'`);
}

function generateSamples(
  schema: Record<string, string>,
  enums: Record<string, string[]>,
): Record<string, unknown>[] {
  const entries = Object.entries(schema);
  if (entries.length === 0) {
    return [{}];
  }
  let results: Record<string, unknown>[] = [{}];
  for (const [key, typeDef] of entries) {
    const isOptional = typeDef.endsWith("?");
    const values = sampleValuesForType(key, typeDef, enums);
    const next: Record<string, unknown>[] = [];
    for (const obj of results) {
      if (isOptional) {
        next.push({ ...obj });
      }
      for (const val of values) {
        next.push({ ...obj, [key]: val });
      }
    }
    results = next;
  }
  return results;
}

describe("contracts/pi-voice.json conformance (TypeScript)", () => {
  it("JOB_UPDATE_STATUSES equals enums.jobStatus", () => {
    assert.deepEqual([...JOB_UPDATE_STATUSES], contract.enums.jobStatus);
  });

  it("headlessChildEnv writes voiceHistory.env as a JSON array with exactly the turn keys", () => {
    const turnSchema = contract.voiceHistory.turn;
    const turnSamples = generateSamples(turnSchema, contract.enums) as VoiceHistoryTurn[];
    assert.ok(turnSamples.length > 0, "No turn samples generated");

    const envVar = contract.voiceHistory.env;
    const env = headlessChildEnv({}, turnSamples);
    assert.ok(envVar in env, `Environment variable ${envVar} missing from headlessChildEnv`);

    const raw = env[envVar];
    assert.ok(typeof raw === "string", `${envVar} must be a string`);
    const parsed = JSON.parse(raw);
    assert.ok(Array.isArray(parsed), `${envVar} must parse to an array`);
    assert.equal(parsed.length, turnSamples.length);

    const expectedKeys = Object.keys(turnSchema).sort();
    for (let i = 0; i < parsed.length; i++) {
      const item = parsed[i] as Record<string, unknown>;
      assert.ok(item && typeof item === "object", `Item ${i} in ${envVar} is not an object`);
      assert.deepEqual(
        Object.keys(item).sort(),
        expectedKeys,
        `Keys in ${envVar}[${i}] do not match contract turn schema`,
      );
      assert.deepEqual(item, turnSamples[i]);
    }
  });

  describe("fromVoice -> parseLine", () => {
    type FromVoiceMapping = {
      tag: string;
      fields: Record<string, string>; // contractField -> eventProperty
    };

    const FROM_VOICE_MAPPING: Record<string, FromVoiceMapping> = {
      ready: { tag: "ready", fields: {} },
      error: { tag: "error", fields: { message: "message" } },
      request_error: { tag: "requestError", fields: { message: "message" } },
      speech_started: { tag: "speechStarted", fields: {} },
      heard: { tag: "heard", fields: { text: "text", item_id: "itemId" } },
      spoken_delta: { tag: "spokenDelta", fields: { text: "text", item_id: "itemId" } },
      spoken: { tag: "spoken", fields: { text: "text", item_id: "itemId" } },
      work: { tag: "work", fields: { id: "id", brief: "brief" } },
      stop_work: { tag: "stopWork", fields: {} },
    };

    it("every contract fromVoice type has an entry in the mapping table", () => {
      const contractTypes = Object.keys(contract.stdio.fromVoice).sort();
      const mappedTypes = Object.keys(FROM_VOICE_MAPPING).sort();
      assert.deepEqual(
        mappedTypes,
        contractTypes,
        "FROM_VOICE_MAPPING must map exactly every contract fromVoice type",
      );
      for (const [type, schema] of Object.entries(contract.stdio.fromVoice)) {
        const mapping = FROM_VOICE_MAPPING[type]!;
        assert.deepEqual(
          Object.keys(mapping.fields).sort(),
          Object.keys(schema).sort(),
          `Field mapping mismatch for ${type}`,
        );
      }
    });

    it("parseLine parses every fromVoice sample with all field values", () => {
      for (const [type, schema] of Object.entries(contract.stdio.fromVoice)) {
        const mapping = FROM_VOICE_MAPPING[type]!;
        const samples = generateSamples(schema, contract.enums);
        assert.ok(samples.length > 0, `No samples generated for ${type}`);

        for (const sample of samples) {
          const wireObj = { type, ...sample };
          const line = JSON.stringify(wireObj);
          const event = parseLine(line);
          assert.ok(event, `parseLine returned undefined for fromVoice message: ${line}`);
          assert.equal(event.tag, mapping.tag, `Tag mismatch for ${type}`);

          for (const [contractField, eventProp] of Object.entries(mapping.fields)) {
            const expectedVal = sample[contractField];
            const actualVal: unknown = (event as Record<string, unknown>)[eventProp];
            assert.equal(
              actualVal,
              expectedVal,
              `Value mismatch for ${type}.${contractField} (mapped to ${eventProp}): expected ${String(expectedVal)}, got ${String(actualVal)}`,
            );
          }
        }
      }
    });
  });

  describe("toVoice builders", () => {
    type ToVoiceBuilder = (sample: Record<string, unknown>) => HeadlessIn;

    const TO_VOICE_BUILDERS: Record<string, { builderName: keyof typeof toVoice; build: ToVoiceBuilder }> = {
      quit: {
        builderName: "quit",
        build: () => toVoice.quit(),
      },
      mute: {
        builderName: "mute",
        build: (s) => toVoice.mute(s.muted as boolean),
      },
      interrupt: {
        builderName: "interrupt",
        build: () => toVoice.interrupt(),
      },
      user: {
        builderName: "user",
        build: (s) => toVoice.user(s.text as string),
      },
      result: {
        builderName: "result",
        build: (s) => toVoice.result(s.id as string, s.speak as string, s.full as string),
      },
      job_update: {
        builderName: "jobUpdate",
        build: (s) =>
          s.note !== undefined
            ? toVoice.jobUpdate(s.id as string, s.status as JobUpdateStatus, s.note as string)
            : toVoice.jobUpdate(s.id as string, s.status as JobUpdateStatus),
      },
    };

    it("every toVoice contract message has a builder", () => {
      const contractTypes = Object.keys(contract.stdio.toVoice).sort();
      const mappedTypes = Object.keys(TO_VOICE_BUILDERS).sort();
      assert.deepEqual(mappedTypes, contractTypes);

      const actualBuilderNames = Object.keys(toVoice).sort();
      const expectedBuilderNames = Object.values(TO_VOICE_BUILDERS)
        .map((b) => b.builderName)
        .sort();
      assert.deepEqual(
        actualBuilderNames,
        expectedBuilderNames,
        "Every function in toVoice must correspond to a contract message",
      );
    });

    it("every toVoice builder produces a contract type with exactly the contract keys and sample values", () => {
      for (const [type, schema] of Object.entries(contract.stdio.toVoice)) {
        const { build } = TO_VOICE_BUILDERS[type]!;
        const samples = generateSamples(schema, contract.enums);
        assert.ok(samples.length > 0, `No samples generated for ${type}`);

        for (const sample of samples) {
          const msg = build(sample);
          assert.equal(msg.type, type, `Message type mismatch for ${type}`);

          const expectedKeys = ["type", ...Object.keys(sample)].sort();
          const actualKeys = Object.keys(msg).sort();
          assert.deepEqual(
            actualKeys,
            expectedKeys,
            `Keys mismatch in builder output for ${type}: expected ${JSON.stringify(expectedKeys)}, got ${JSON.stringify(actualKeys)}`,
          );

          for (const [key, val] of Object.entries(sample)) {
            assert.equal(
              (msg as Record<string, unknown>)[key],
              val,
              `Value mismatch for ${type}.${key}: expected ${String(val)}, got ${String((msg as Record<string, unknown>)[key])}`,
            );
          }
        }
      }
    });
  });
});
