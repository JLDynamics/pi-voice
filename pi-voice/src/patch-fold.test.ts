import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
// @ts-expect-error: plain .mjs script without type declarations
import { EDITS, MARKER, PACKAGE, REVIEWED, patchRenderer, plan, sha256 } from "../../scripts/patch-pi-native-fold.mjs";

type Edit = { name: string; before: string; after: string };
const edits = EDITS as Edit[];
const renderer = edits.map((e) => `/*x*/${e.before}/*y*/`).join("\n");

function fakePackage(version: string, chunkText?: string): string {
  const root = mkdtempSync(join(tmpdir(), "pv-fold-"));
  writeFileSync(join(root, "package.json"), JSON.stringify({ name: PACKAGE, version }));
  const reviewed = (REVIEWED as Record<string, { chunk: string }>)[version];
  if (reviewed && chunkText !== undefined) {
    mkdirSync(join(root, "dist/bundle/chunks"), { recursive: true });
    writeFileSync(join(root, "dist/bundle/chunks", reviewed.chunk), chunkText);
  }
  return root;
}

test("patchRenderer applies every edit once and marks the bundle", () => {
  const out = patchRenderer(renderer);
  for (const e of edits) assert.ok(out.includes(e.after), e.name);
  assert.ok(out.includes(MARKER));
});

test("patchRenderer refuses when an anchor is missing or repeated", () => {
  assert.throws(() => patchRenderer(renderer.replace(edits[0].before, "")), /exactly one/);
  assert.throws(() => patchRenderer(renderer + edits[1].before), /exactly one/);
});

test("Pi 1.1.0 is a reviewed target", () => {
  assert.ok((REVIEWED as Record<string, unknown>)["1.1.0"]);
});

test("plan refuses an unreviewed Pi version without writing", () => {
  const p = plan(fakePackage("9.9.9"));
  assert.equal(p.action, "refuse");
  assert.match(p.message, /no reviewed renderer patch/);
});

test("plan is a no-op on an already patched bundle", () => {
  const p = plan(fakePackage("1.1.0", patchRenderer(renderer)));
  assert.equal(p.action, "noop");
  assert.match(p.message, /already patched/);
});

test("plan refuses a 1.1.0 bundle whose hash differs from the reviewed one", () => {
  const p = plan(fakePackage("1.1.0", renderer));
  assert.notEqual(sha256(renderer), (REVIEWED as Record<string, { sha256: string }>)["1.1.0"].sha256);
  assert.equal(p.action, "refuse");
  assert.match(p.message, /differs from the reviewed bundle/);
});
