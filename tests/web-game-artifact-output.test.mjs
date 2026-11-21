import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import {
  ensureOutputDirectory,
  writeArtifactFile,
} from "../skills/.curated/develop-web-game/scripts/artifact_output.mjs";

function makeTempDirectory(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "web-game-artifacts-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return directory;
}

test("creates the default workspace output and atomically overwrites artifacts", (t) => {
  const root = makeTempDirectory(t);
  const output = ensureOutputDirectory("output/web-game", { cwd: root });
  const artifact = path.join(output, "state-0.json");

  assert.equal(output, path.join(root, "output", "web-game"));
  writeArtifactFile(artifact, "first", { cwd: root });
  writeArtifactFile(artifact, "second", { cwd: root });
  assert.equal(fs.readFileSync(artifact, "utf8"), "second");
});

test("rejects a symlink or junction in the workspace output path", (t) => {
  const root = makeTempDirectory(t);
  const target = path.join(root, "outside-target");
  const linkedOutput = path.join(root, "output");
  fs.mkdirSync(target);
  fs.writeFileSync(path.join(target, "sentinel.txt"), "unchanged");
  fs.symlinkSync(target, linkedOutput, process.platform === "win32" ? "junction" : "dir");

  assert.throws(() => ensureOutputDirectory("output/web-game", { cwd: root }), /symbolic link/);
  assert.equal(fs.readFileSync(path.join(target, "sentinel.txt"), "utf8"), "unchanged");
});

test("replaces a leaf file symlink without writing through it", (t) => {
  const root = makeTempDirectory(t);
  const victim = path.join(root, "victim.txt");
  const artifact = path.join(root, "shot-0.png");
  fs.writeFileSync(victim, "unchanged");
  try {
    fs.symlinkSync(victim, artifact, "file");
  } catch (error) {
    if (["EPERM", "EACCES", "ENOTSUP"].includes(error.code)) {
      t.skip(`file symlinks are unavailable: ${error.code}`);
      return;
    }
    throw error;
  }

  writeArtifactFile(artifact, Buffer.from("replacement"), { cwd: root });
  assert.equal(fs.readFileSync(victim, "utf8"), "unchanged");
  assert.equal(fs.readFileSync(artifact, "utf8"), "replacement");
  assert.equal(fs.lstatSync(artifact).isSymbolicLink(), false);
});

test("replaces a hardlinked artifact without changing its other link", (t) => {
  const root = makeTempDirectory(t);
  const linkedName = path.join(root, "linked-artifact");
  const artifact = path.join(root, "shot-0.png");
  fs.writeFileSync(linkedName, "unchanged");
  fs.linkSync(linkedName, artifact);

  writeArtifactFile(artifact, "replacement", { cwd: root });
  assert.equal(fs.readFileSync(linkedName, "utf8"), "unchanged");
  assert.equal(fs.readFileSync(artifact, "utf8"), "replacement");
});

test("requires an explicit option for external output and preserves that option", (t) => {
  const root = makeTempDirectory(t);
  const workspace = path.join(root, "workspace");
  const external = path.join(root, "external-output");
  fs.mkdirSync(workspace);

  assert.throws(() => ensureOutputDirectory("../external-output", { cwd: workspace }), /explicit/);
  assert.equal(fs.existsSync(external), false);
  assert.equal(
    ensureOutputDirectory("../external-output", { cwd: workspace, allowExternal: true }),
    external
  );
});

test("an explicit path inside the workspace still rejects symlink components", (t) => {
  const root = makeTempDirectory(t);
  const target = path.join(root, "target");
  const linkedOutput = path.join(root, "chosen-output");
  fs.mkdirSync(target);
  fs.symlinkSync(target, linkedOutput, process.platform === "win32" ? "junction" : "dir");

  assert.throws(
    () => ensureOutputDirectory("chosen-output", { cwd: root, allowExternal: true }),
    /symbolic link/
  );
});

test("rechecks the workspace directory before writing each artifact", (t) => {
  const root = makeTempDirectory(t);
  const output = ensureOutputDirectory("output/web-game", { cwd: root });
  const target = path.join(root, "replacement-target");
  fs.mkdirSync(target);
  fs.writeFileSync(path.join(target, "sentinel.txt"), "unchanged");
  fs.rmdirSync(output);
  fs.symlinkSync(target, output, process.platform === "win32" ? "junction" : "dir");

  assert.throws(
    () => writeArtifactFile(path.join(output, "state-0.json"), "payload", { cwd: root }),
    /symbolic link/
  );
  assert.equal(fs.readFileSync(path.join(target, "sentinel.txt"), "utf8"), "unchanged");
});
