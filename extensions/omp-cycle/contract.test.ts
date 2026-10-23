// The cycle's rules, tested on their own: no omp, no model, no repository - just the decisions.
//   node --test extensions/omp-cycle/
import assert from "node:assert/strict";
import { test } from "node:test";
import {
	afterReview, applyOverrides, codePrompt, DEFAULTS, fingerprint, type Finding, lastAnswer, listFindings, newRun,
	parseRun, planPrompt, reviewPrompt, slug, stamp, truncateMid, verdictFromText, type Verdict, words,
} from "./contract.ts";

const run = (over = {}) => newRun("r1", "/tmp/here", "add retries", { ...DEFAULTS, ...over }, "abc123def456");
const verdictOf = (verdict: "approve" | "changes", problem = "the retry loop never stops"): Verdict => ({
	verdict, source: "tool", summary: "looked at it", findings: verdict === "changes" ? [{ file: "a.py", problem, fix: "count" }] : [],
});

// ---- arguments -------------------------------------------------------------------------------------------------

test("parseRun takes flags and leaves the task", () => {
	const p = parseRun('--rounds 2 --test "python -m unittest -q" --plan hangar/glm --fix hangar/qwen add retries to the uploader');
	assert.equal(p.error, undefined);
	assert.equal(p.task, "add retries to the uploader");
	assert.equal(p.overrides.rounds, 2);
	assert.equal(p.overrides.testCommand, "python -m unittest -q");
	assert.deepEqual(p.overrides.models, { plan: "hangar/glm", fix: "hangar/qwen" });
});

test("parseRun keeps a quoted task and single quotes", () => {
	const p = parseRun("make /cycle work 'on windows too'");
	assert.equal(p.error, undefined);
	assert.equal(p.task, "make /cycle work on windows too");
	assert.deepEqual(words('a "b c" \'d e\' f'), ["a", "b c", "d e", "f"]);
	assert.deepEqual(words('"unclosed x'), ["\"unclosed", "x"]);
});

test("parseRun reports a bad flag instead of starting a run", () => {
	for (const line of ["--rounds 0 fix it", "--rounds two fix it", "--rounds", "--test", "--nope fix it"]) {
		const p = parseRun(line);
		assert.ok(p.error, `${line} should be an error`);
		assert.equal(p.task, "");
	}
});

test("parseRun needs no flags at all, and --allow-fallback takes no value", () => {
	const p = parseRun("--allow-fallback fix the flaky test");
	assert.equal(p.error, undefined);
	assert.equal(p.overrides.allowFallback, true);
	assert.equal(p.overrides.rounds, undefined);
	assert.equal(p.task, "fix the flaky test");
});

test("applyOverrides keeps the settings it was not given", () => {
	const s = applyOverrides(DEFAULTS, { rounds: 2, models: { code: "x/y" }, thinking: { plan: "high" } });
	assert.equal(s.rounds, 2);
	assert.equal(s.models.code, "x/y");
	assert.equal(s.models.plan, DEFAULTS.models.plan);
	assert.equal(s.models.review, DEFAULTS.models.review);
	assert.equal(s.thinking.plan, "high");
	assert.equal(s.testCommand, "");
	assert.equal(applyOverrides(s, { models: { fix: "z" } }).thinking.plan, "high");
});

// ---- verdicts --------------------------------------------------------------------------------------------------

test("verdictFromText reads the last VERDICT line, not the first", () => {
	const v = verdictFromText("VERDICT: CHANGES\nstill broken\n**VERDICT: APPROVE**");
	assert.equal(v?.verdict, "approve");
	assert.deepEqual(v?.findings, []);
	assert.equal(v?.source, "text");
});

test("verdictFromText takes the findings after the line, and before it when nothing follows", () => {
	const after = verdictFromText("review:\nVERDICT: CHANGES\n1. a.py: off by one\n2. b.py: no test");
	assert.equal(after?.findings.length, 1);
	assert.match(after!.findings[0].problem, /^1\. a\.py: off by one/);

	const before = verdictFromText("1. a.py: off by one\n2. b.py: no test\nVERDICT: CHANGES");
	assert.match(before!.findings[0].problem, /^1\. a\.py: off by one/);
	assert.ok(!before!.findings[0].problem.includes("VERDICT"));
});

test("verdictFromText says nothing when there is no VERDICT line", () => {
	assert.equal(verdictFromText("looks fine to me, ship it"), undefined);
	assert.equal(verdictFromText(""), undefined);
});

test("findings are listed and described for the coder", () => {
	const findings: Finding[] = [{ file: "a.py", problem: "no retry", fix: "loop 3x" }, { problem: "typo" }];
	assert.equal(listFindings(findings), "1. a.py: no retry Fix: loop 3x\n2. typo");
});

test("a fingerprint ignores order, case and spacing but not content", () => {
	const a = fingerprint([{ file: "A.py", problem: "Off   by ONE" }, { file: "b.py", problem: "no test" }]);
	const b = fingerprint([{ file: "a.py", problem: "off by one" }, { file: "b.py", problem: "no test." }]);
	assert.equal(a, b);
	assert.notEqual(a, fingerprint([{ file: "a.py", problem: "off by two" }, { file: "b.py", problem: "no test" }]));
	assert.notEqual(a, fingerprint([]));
});

// ---- decisions -------------------------------------------------------------------------------------------------

test("afterReview only approves a clean unconflicted approval of an untouched tree", () => {
	assert.deepEqual(afterReview(run(), { verdict: verdictOf("approve"), conflict: false, treeUnchanged: true }), { kind: "approve" });
	const changes = { verdict: verdictOf("changes"), conflict: false, treeUnchanged: true };
	assert.equal(afterReview(run(), changes).kind, "fix");
});

test("afterReview stops rather than spin", () => {
	const changes = { verdict: verdictOf("changes"), conflict: false, treeUnchanged: true };
	const clean = { verdict: verdictOf("approve"), conflict: false, treeUnchanged: true };
	const kind = (f: Parameters<typeof afterReview>[1], settings = {}, round = 0) => {
		const r = run(settings);
		r.round = round;
		return afterReview(r, f).kind;
	};
	assert.equal(kind({ ...clean, treeUnchanged: false }), "stop"); // the reviewer edited: its word is void
	assert.equal(kind({ ...clean, conflict: true }), "stop");
	assert.equal(kind({ ...clean, verdict: undefined }), "stop");
	assert.equal(kind({ ...changes, verdict: { ...changes.verdict, findings: [] } }), "stop");
	assert.equal(kind(changes), "fix");
	assert.equal(kind(changes, { rounds: 1 }, 1), "stop"); // the review of the last allowed round ends the run
});

test("afterReview stops when the same findings come back", () => {
	const r = run({ rounds: 9 });
	const v = verdictOf("changes");
	r.fingerprints.push(fingerprint(v.findings));
	const d = afterReview(r, { verdict: v, conflict: false, treeUnchanged: true });
	assert.equal(d.kind, "stop");
	assert.match(d.kind === "stop" ? d.reason : "", /same findings/);
});

test("afterReview counts rounds the way the run does", () => {
	const v = verdictOf("changes", "still wrong");
	const r = run({ rounds: 2 });
	r.round = 1;
	assert.equal(afterReview(r, { verdict: v, conflict: false, treeUnchanged: true }).kind, "fix");
	r.round = 2;
	assert.equal(afterReview(r, { verdict: v, conflict: false, treeUnchanged: true }).kind, "stop");
});

// ---- prompts and text ------------------------------------------------------------------------------------------

test("the prompts carry the task, the plan and the test command", () => {
	assert.match(planPrompt("add retries"), /Task: add retries$/);
	assert.match(codePrompt("t", "1. do it", "make test"), /`make test`/);
	assert.ok(!codePrompt("t", "1. do it", "").includes("The tests run with"));
	assert.match(codePrompt("t", "1. do it", "make test"), /Plan:\n1\. do it$/);
	assert.match(reviewPrompt("t", "the plan", "THE DIFF", "Tests pass (`make test`).\n\n"), /Tests pass/);
});

test("truncateMid keeps both ends and says what is missing", () => {
	const text = "x".repeat(1000);
	assert.equal(truncateMid(text, 2000), text);
	const cut = truncateMid(text, 100);
	assert.ok(cut.startsWith("xxxx") && cut.endsWith("xxxx")); // both ends survive
	assert.match(cut, /\.\.\. \[900 characters omitted\] \.\.\./);
	const head = truncateMid("abcdefghij", 6);
	assert.ok(head.startsWith("abc"));
	assert.ok(head.endsWith("hij"));
});

test("lastAnswer is the last assistant turn of the branch", () => {
	const entries = [
		{ type: "message", message: { role: "user", content: "hi" } },
		{ type: "message", message: { role: "assistant", content: [{ type: "text", text: "first" }] } },
		{ type: "something else" },
		{ type: "message", message: { role: "assistant", content: [
			{ type: "toolCall", name: "read" }, { type: "text", text: "second" }, { type: "text", text: "line" },
		], stopReason: "stop" } },
	];
	const a = lastAnswer(entries)!;
	assert.equal(a.text, "second\nline");
	assert.equal(a.toolCalls, 1);
	assert.equal(a.stopReason, "stop");
	assert.equal(lastAnswer([]), undefined);
	assert.equal(lastAnswer([{ type: "message", message: { role: "assistant", content: "text as a string" } }])!.text, "");
});

test("ids are readable: slug falls back, stamp sorts by time", () => {
	assert.equal(slug("Add retries, please!"), "add-retries-please");
	assert.equal(slug("###"), "task");
	assert.equal(slug("x".repeat(40)).length, 32);
	assert.match(stamp(new Date(2026, 9, 23, 7, 5, 9)), /^20261023-070509$/);
});
