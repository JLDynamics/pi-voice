import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

type Manifest = { peerDependencies: Record<string, string>; devDependencies: Record<string, string> };
const manifest = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")) as Manifest;

// The peer range is the Pi this extension claims to run on; the dev pin is the
// Pi that tsc checks it against. They must name the same version, or the claim
// drifts from what is tested. (`^0.86.0` once excluded the installed 1.1.0:
// a caret on 0.x pins the minor.)
test("Pi peer ranges match the pinned dev types", () => {
  for (const name of ["@earendil-works/pi-coding-agent", "@earendil-works/pi-tui"]) {
    const pinned = manifest.devDependencies[name];
    assert.match(pinned, /^\d+\.\d+\.\d+$/, `${name} dev pin is exact`);
    assert.equal(manifest.peerDependencies[name], `^${pinned}`, `${name} peer range`);
    assert.ok(Number(pinned.split(".")[0]) >= 1, `${name}: a 0.x caret range would pin the minor`);
  }
});
