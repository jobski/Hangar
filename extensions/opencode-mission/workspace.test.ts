import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdir, mkdtemp, truncate, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { changes, initialiseRepository, snapshot } from "./workspace.ts";

const MIB = 1024 * 1024;

async function fixture() {
  const directory = await mkdtemp(join(tmpdir(), "hangar-workspace-"));
  await initialiseRepository(directory);
  return directory;
}

test("a media-sized new file neither breaks the snapshot nor the change report", async () => {
  const directory = await fixture();
  await mkdir(join(directory, "build"));
  const master = join(directory, "build", "master.wav");
  await writeFile(master, "");
  await truncate(master, 120 * MIB); // sparse, like a rendered audio master

  const before = await snapshot(directory, "none");
  const report = await changes(directory, "none");
  assert.match(report, /build\/master\.wav/);
  assert.match(report, /too large to show here/);

  await truncate(master, 130 * MIB); // a later render replaces the artefact
  assert.notEqual(await snapshot(directory, "none"), before);
});

test("re-writing an oversized file at the same size still changes the snapshot", async () => {
  const directory = await fixture();
  const media = join(directory, "clip.mp4");
  await writeFile(media, Buffer.alloc(3 * MIB, 0x61));
  const before = await snapshot(directory, "none");
  await writeFile(media, Buffer.alloc(3 * MIB, 0x62));
  assert.notEqual(await snapshot(directory, "none"), before);
});

test("small new files are still reported inline, binaries described as binary", async () => {
  const directory = await fixture();
  await writeFile(join(directory, "frame.png"), Buffer.from([0x89, 0x50, 0x00, 0x01]));
  await writeFile(join(directory, "script.py"), "print('hi')\n");
  const report = await changes(directory, "none");
  assert.match(report, /binary: 4 bytes skipped/);
  assert.match(report, /print\('hi'\)/);
  assert.match(await snapshot(directory, "none"), /^[0-9a-f]{64}$/);
});
