// What the store does with git and with the test command, against a real temporary repository: no omp, no model.
//   node --test extensions/omp-cycle/
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { type Exec, type ExecResult, changeReport, headSha, runTests, snapshot } from "./store.ts";

/** One command in the shape omp's exec gives back: real processes, real timeouts. */
const sh: Exec = (cmd, args, opts) => {
	const r = spawnSync(cmd, args, { cwd: opts?.cwd, encoding: "utf8", timeout: opts?.timeout });
	const killed = r.status === null; // a killed run has no exit code
	return Promise.resolve({
		stdout: r.stdout ?? "",
		stderr: r.stderr ?? "",
		code: killed ? 1 : r.status ?? 1,
		killed,
	} satisfies ExecResult);
};

const git = (at: string, ...args: string[]) => {
	const r = spawnSync("git", args, { cwd: at, encoding: "utf8" });
	assert.equal(r.status, 0, `git ${args.join(" ")}: ${r.stderr}`);
};
const commit = (at: string, message: string) =>
	git(at, "-c", "user.email=cycle@test", "-c", "user.name=cycle test", "commit", "-qm", message);
const head = (at: string) => spawnSync("git", ["rev-parse", "HEAD"], { cwd: at, encoding: "utf8" }).stdout.trim();

/** A fresh git repository in a temporary folder, with `setup` run inside it. */
function tempRepo(setup: (at: string) => void): string {
	const at = mkdtempSync(join(tmpdir(), "omp-cycle-store-"));
	git(at, "init", "-q", "--initial-branch=main");
	setup(at);
	return at;
}
const write = (at: string, name: string, text: string) => writeFileSync(join(at, name), text);
const gone = (...dirs: string[]) => dirs.forEach((d) => rmSync(d, { recursive: true, force: true }));
const idle = new AbortController().signal;

// ---- snapshots and reports when the run has no base commit ------------------------------------------------------

test("a fresh repository shows its staged work to a snapshot", async () => {
	const staged = (text: string) => tempRepo((at) => {
		write(at, "f.txt", text);
		git(at, "add", "f.txt");
	});
	const one = staged("one\n");
	const other = staged("another\n");
	const nothing = tempRepo(() => undefined);
	assert.equal(await headSha(sh, one), "none", "no commit yet, so the run has no base");

	const first = await snapshot(sh, one, "none");
	assert.equal(first, await snapshot(sh, one, "none"), "the same tree hashes the same");
	assert.notEqual(first, await snapshot(sh, other, "none"), "different staged content hashes differently");
	assert.notEqual(first, await snapshot(sh, nothing, "none"), "an empty tree hashes differently");
	gone(one, other, nothing);
});

test("a new untracked file changes the snapshot with a base, too", async () => {
	const at = tempRepo((d) => {
		write(d, "f.txt", "one\n");
		git(d, "add", "f.txt");
		commit(d, "start");
	});
	const base = head(at);
	write(at, "new.txt", "first\n");
	const before = await snapshot(sh, at, base);
	assert.notEqual(before, await snapshot(sh, at, "none"), "the untracked file is in both");
	write(at, "new.txt", "second\n");
	assert.notEqual(before, await snapshot(sh, at, base), "its content is part of the tree");
	gone(at);
});

test("the report shows staged work when there is no base commit", async () => {
	const at = tempRepo((d) => {
		write(d, "staged.txt", "staged line\n");
		git(d, "add", "staged.txt");
		write(d, "new.txt", "new file line\n");
	});
	const report = await changeReport(sh, at, "none", 100_000);
	assert.match(report, /git status --short/);
	assert.match(report, /\$ git diff\b/);
	assert.match(report, /git diff --cached/);
	assert.match(report, /staged line/);
	assert.match(report, /new file new\.txt/);
	assert.match(report, /new file line/);
	gone(at);
});

test("the report diffs against the base when there is one", async () => {
	const at = tempRepo((d) => {
		write(d, "f.txt", "one\n");
		git(d, "add", "f.txt");
		commit(d, "start");
		write(d, "f.txt", "two\n");
		git(d, "add", "f.txt"); // staged after the run started
	});
	const base = head(at);
	const report = await changeReport(sh, at, base, 100_000);
	assert.match(report, new RegExp(`git diff ${base.slice(0, 10)}\\b`));
	assert.match(report, /\+two/);
	assert.ok(!report.includes("git diff --cached"), "with a base, one diff covers staged and unstaged work");
	gone(at);
});

test("a huge new file is described in the report, not read into it", async () => {
	const at = tempRepo((d) => write(d, "big.bin", "x".repeat(3 * 1024 * 1024))); // untracked, over the 1 MB shown
	const report = await changeReport(sh, at, "none", 100_000);
	assert.match(report, /new file big\.bin: 3145728 bytes/);
	assert.ok(!report.includes("xxxxxxxxxx"), "its content stays out of the report");
	assert.equal((await snapshot(sh, at, "none")).length, 64, "a snapshot still hashes it");
	gone(at);
});

test("an untracked link is named, never followed", async () => {
	const at = tempRepo((d) => {
		symlinkSync("/dev/zero", join(d, "bottomless")); // following it would read forever
		symlinkSync("nowhere at all", join(d, "dangling"));
	});
	const report = await changeReport(sh, at, "none", 100_000);
	assert.match(report, /new link bottomless -> \/dev\/zero/);
	assert.match(report, /new link dangling -> nowhere at all/);
	assert.equal((await snapshot(sh, at, "none")).length, 64, "the snapshot still finishes");
	gone(at);
});

// ---- the test command ------------------------------------------------------------------------------------------

test("the tests run in bash; where bash is missing, in that machine's own shell", async () => {
	const tried: string[][] = [];
	const fake = (bashWorks: boolean): Exec => async (cmd, args) => {
		tried.push([cmd, ...args]);
		if (cmd === "bash" && !bashWorks) throw Object.assign(new Error("spawn bash ENOENT"), { code: "ENOENT" });
		return { stdout: "1 passed", stderr: "", code: 0, killed: false } satisfies ExecResult;
	};

	tried.length = 0;
	const here = await runTests(fake(true), "/tmp", "make test", 1, idle);
	assert.equal(here.ok, true);
	assert.deepEqual(tried, [["bash", "-lc", "make test"]], "bash is used when it is there");

	tried.length = 0;
	const elsewhere = await runTests(fake(false), "/tmp", "make test", 1, idle);
	assert.equal(elsewhere.ok, true, "the same command in another shell still passes");
	assert.equal(tried.length, 2);
	assert.deepEqual(tried[1], process.platform === "win32" ? ["cmd", "/c", "make test"] : ["sh", "-lc", "make test"]);
});

test("a shell that reports bash as missing also falls back", async () => {
	const tried: string[] = [];
	const weird: Exec = async (cmd) => {
		tried.push(cmd);
		if (cmd === "bash") {
			return { stdout: "", stderr: "/usr/bin/env: bash: No such file or directory", code: 127, killed: false };
		}
		return { stdout: "ok", stderr: "", code: 0, killed: false } satisfies ExecResult;
	};
	const r = await runTests(weird, "/tmp", "make test", 1, idle);
	assert.equal(r.ok, true);
	assert.equal(tried.length, 2);
	assert.equal(tried[0], "bash");
	assert.notEqual(tried[1], "bash");
});

test("a failing test is a failing test, not a missing shell", async () => {
	const tried: string[] = [];
	const failing: Exec = async (cmd) => {
		tried.push(cmd);
		return { stdout: "", stderr: "FAILED (failures=1)", code: 1, killed: false } satisfies ExecResult;
	};
	const r = await runTests(failing, "/tmp", "make test", 1, idle);
	assert.equal(r.ok, false);
	assert.equal(r.killed, false);
	assert.deepEqual(tried, ["bash"], "no retry: the tests themselves failed");
	assert.match(r.output, /FAILED \(failures=1\)/);
});

test("no shell at all says so instead of pretending the tests passed", async () => {
	const missing: Exec = async (cmd) => {
		throw Object.assign(new Error(`spawn ${cmd} ENOENT`), { code: "ENOENT" });
	};
	const r = await runTests(missing, "/tmp", "make test", 1, idle);
	assert.equal(r.ok, false);
	assert.match(r.output, /neither bash nor/);
});

test("a test run cut off by its timeout is not a pass", async () => {
	const r = await runTests(sh, "/tmp", "sleep 30", 1 / 600, idle); // 100 ms for a 30 s sleep
	assert.equal(r.ok, false);
	assert.equal(r.killed, true);
	assert.match(r.output, /\[killed: timeout or cancelled\]/);
});

test("real commands run in a real shell", async () => {
	const pass = await runTests(sh, "/tmp", "echo 1", 1, idle);
	assert.equal(pass.ok, true);
	assert.match(pass.output, /1$/);
	const fail = await runTests(sh, "/tmp", "exit 3", 1, idle);
	assert.equal(fail.ok, false);
});
