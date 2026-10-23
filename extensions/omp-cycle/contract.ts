// The cycle's rules, free of omp APIs: phases, settings, verdicts, prompts, stop rules. Pure so a host can reuse it.
import { createHash } from "node:crypto";

export type Phase = "plan" | "code" | "review" | "fix";
export type Step = Phase | "test" | "done";
export type Status = "running" | "approved" | "stopped" | "interrupted" | "cancelled";

export interface Settings {
	models: Record<Phase, string>;
	thinking: Partial<Record<Phase, string>>;
	rounds: number;
	stepTimeoutMin: number;
	testCommand: string;
	testTimeoutMin: number;
	diffLimitKb: number;
	allowFallback: boolean;
}

export const DEFAULTS: Settings = {
	models: { plan: "hangar/glm", code: "hangar/qwen", review: "hangar/glm", fix: "hangar/qwen" },
	thinking: {},
	rounds: 4,
	stepTimeoutMin: 90,
	testCommand: "",
	testTimeoutMin: 20,
	diffLimitKb: 60,
	allowFallback: false,
};

export interface Finding {
	file?: string;
	problem: string;
	fix?: string;
}

export interface Verdict {
	verdict: "approve" | "changes";
	summary: string;
	findings: Finding[];
	source: "tool" | "text";
}

export interface RunState {
	version: 1;
	id: string;
	cwd: string;
	task: string;
	status: Status;
	stopReason?: string;
	step: Step;
	round: number;
	testFails: number;
	settings: Settings;
	baseSha: string;
	plan?: string;
	findings: Finding[];
	fingerprints: string[];
	sessions: { orig?: string; plan?: string; code?: string };
	reviewedSnapshot?: string;
	served: string[];
	log: string[];
}

export const READ_ONLY = new Set<Phase>(["plan", "review"]);
export const MAX_TEST_FAILS = 3;

export function newRun(id: string, cwd: string, task: string, settings: Settings, baseSha: string): RunState {
	return {
		version: 1, id, cwd, task, status: "running", step: "plan", round: 0, testFails: 0, settings, baseSha,
		findings: [], fingerprints: [], sessions: {}, served: [], log: [],
	};
}

// ---- arguments -------------------------------------------------------------------------------------------------

/** Split a command line into words, honouring single and double quotes. */
export function words(line: string): string[] {
	const out: string[] = [];
	const re = /"([^"]*)"|'([^']*)'|(\S+)/g;
	for (let m = re.exec(line); m; m = re.exec(line)) out.push(m[1] ?? m[2] ?? m[3]);
	return out;
}

export interface Parsed {
	task: string;
	overrides: Partial<Settings> & { models?: Partial<Record<Phase, string>> };
	error?: string;
}

/** `/cycle [--rounds N] [--test CMD] [--plan M] [--code M] [--review M] [--fix M] [--timeout MIN] <task>` */
export function parseRun(line: string): Parsed {
	const ws = words(line);
	const overrides: Parsed["overrides"] = {};
	const rest: string[] = [];
	const need = (i: number, flag: string): string => {
		if (i >= ws.length) throw new Error(`${flag} needs a value`);
		return ws[i];
	};
	try {
		for (let i = 0; i < ws.length; i++) {
			const w = ws[i];
			if (w === "--rounds" || w === "--timeout") {
				const n = Number(need(++i, w));
				if (!Number.isInteger(n) || n < 1) throw new Error(`${w} must be a whole number of 1 or more`);
				if (w === "--rounds") overrides.rounds = n;
				else overrides.stepTimeoutMin = n;
			} else if (w === "--test") {
				overrides.testCommand = need(++i, w);
			} else if (w === "--allow-fallback") {
				overrides.allowFallback = true;
			} else if (w === "--plan" || w === "--code" || w === "--review" || w === "--fix") {
				overrides.models = { ...overrides.models, [w.slice(2)]: need(++i, w) };
			} else if (w.startsWith("--")) {
				throw new Error(`unknown option ${w}`);
			} else {
				rest.push(w);
			}
		}
	} catch (e) {
		return { task: "", overrides, error: (e as Error).message };
	}
	return { task: rest.join(" ").trim(), overrides };
}

export function applyOverrides(base: Settings, o: Parsed["overrides"]): Settings {
	return { ...base, ...o, models: { ...base.models, ...o.models }, thinking: { ...base.thinking, ...o.thinking } };
}

// ---- verdicts --------------------------------------------------------------------------------------------------

const VERDICT_LINE = /^\s*\**VERDICT:?\**\s*:?\s*(APPROVE|CHANGES)\b/gim;

/** A `VERDICT: APPROVE|CHANGES` line in the reviewer's answer, for a model that did not call the verdict tool. */
export function verdictFromText(text: string): Verdict | undefined {
	const m = [...text.matchAll(VERDICT_LINE)].pop();
	if (!m) return undefined;
	const verdict = m[1].toLowerCase() as Verdict["verdict"];
	const after = text.slice((m.index ?? 0) + m[0].length).trim();
	const body = verdict === "changes" ? (after || text.slice(0, m.index).trim()) : "";
	return {
		verdict, source: "text", summary: text.slice(0, m.index).trim().slice(-800),
		findings: verdict === "changes" && body ? [{ problem: body }] : [],
	};
}

export function describeFinding(f: Finding, n: number): string {
	return `${n}. ${f.file ? `${f.file}: ` : ""}${f.problem}${f.fix ? ` Fix: ${f.fix}` : ""}`;
}

export function listFindings(findings: Finding[]): string {
	return findings.map((f, i) => describeFinding(f, i + 1)).join("\n");
}

/** Stable identity of a set of findings, to notice the same ones coming back. */
export function fingerprint(findings: Finding[]): string {
	const norm = findings
		.map((f) => `${(f.file ?? "").toLowerCase()}|${f.problem.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim()}`)
		.sort()
		.join("\n");
	return createHash("sha256").update(norm).digest("hex").slice(0, 16);
}

// ---- decisions -------------------------------------------------------------------------------------------------

export type Decision = { kind: "approve" } | { kind: "fix" } | { kind: "stop"; reason: string };

export interface ReviewFacts {
	verdict: Verdict | undefined;
	conflict: boolean;
	treeUnchanged: boolean;
}

/** What to do after a review. Anything but a clean, unconflicted approval of an untouched tree is not an approval. */
export function afterReview(run: RunState, f: ReviewFacts): Decision {
	if (!f.treeUnchanged) return { kind: "stop", reason: "the review changed files; approval is void (review must be read-only)" };
	if (!f.verdict) return { kind: "stop", reason: "the reviewer gave no verdict (no cycle_verdict call and no VERDICT line)" };
	if (f.conflict) return { kind: "stop", reason: "the reviewer gave conflicting verdicts" };
	if (f.verdict.verdict === "approve") return { kind: "approve" };
	if (!f.verdict.findings.length) return { kind: "stop", reason: "the review asked for changes but listed no findings" };
	const fp = fingerprint(f.verdict.findings);
	if (run.fingerprints.includes(fp)) return { kind: "stop", reason: "the same findings came back after a fix" };
	if (run.round >= run.settings.rounds) return { kind: "stop", reason: `still not approved after ${run.round} review rounds` };
	return { kind: "fix" };
}

// ---- prompts ---------------------------------------------------------------------------------------------------

export function planPrompt(task: string): string {
	return `You are the planner. Plan this task for the repository in the current directory; do NOT edit any files (you only have read-only tools).
Inspect existing files first, including untracked files. Build on what is already there, preserving existing work and using its structure and stack. Only start from scratch when the folder has no project files.
Read what you need, then give a concrete, numbered implementation plan: files to change, what to change, edge cases, and how to verify it.

Task: ${task}`;
}

export function codePrompt(task: string, plan: string, testCommand: string): string {
	return `You are the coder. Implement the plan below in this repository, completely. Keep to the repository's style.
Build on existing files, including untracked files; preserve existing work and avoid replacing the project wholesale.
${testNote(testCommand)}When you are done, summarise what you changed in a few lines.

Task: ${task}

Plan:
${plan}`;
}

export function resumeCodePrompt(testCommand: string): string {
	return `You are the coder. This work was interrupted. Carry on implementing the plan above exactly where it was left: check what is already done (\`git status\`, \`git diff\`, the files), finish the rest, and do not start over or redo finished parts. If everything is already implemented, say so. ${testNote(testCommand)}When you are done, summarise what you changed.`;
}

export function fixPrompt(findings: string, testCommand: string): string {
	return `You are the coder. Fix exactly these review findings, nothing else, then summarise what you changed:

${findings}
${testNote(testCommand)}`;
}

export function resumeFixPrompt(findings: string): string {
	return `You are the coder. This work was interrupted. Carry on fixing exactly these review findings, checking first what is already done (\`git status\`, \`git diff\`); do not redo finished fixes:

${findings}

When you are done, summarise what you changed.`;
}

export function testFailPrompt(cmd: string, output: string): string {
	return `The tests fail after your change. Make them pass without weakening them. The command and its output:

$ ${cmd}
${output}`;
}

export function reviewPrompt(task: string, plan: string, report: string, tests: string): string {
	return `You are the reviewer, in a fresh session: you have not seen the coder's conversation. You only have read-only tools, plus cycle_diff (the current changes) and cycle_verdict. Do NOT try to edit anything.
Review the changes made for this task against the plan. Check correctness, edge cases, the plan, and tests. Read the changed files where the diff below is not enough.
When you have finished, call cycle_verdict exactly once: verdict "approve" only if there is nothing left that must change, otherwise "changes" with each real problem as a finding (file, problem, fix). Then end your answer with exactly one line, either "VERDICT: APPROVE" or "VERDICT: CHANGES".

Task: ${task}

Plan:
${plan}

${tests}Changes (git diff against the start of this run, and new files):
${report}`;
}

function testNote(cmd: string): string {
	return cmd ? `The tests run with \`${cmd}\` after you finish; make them pass. ` : "";
}

// ---- text helpers ----------------------------------------------------------------------------------------------

export function truncateMid(text: string, limit: number): string {
	if (text.length <= limit) return text;
	const head = Math.floor(limit * 0.6);
	const tail = limit - head;
	return `${text.slice(0, head)}\n... [${text.length - limit} characters omitted] ...\n${text.slice(text.length - tail)}`;
}

export interface Answer {
	text: string;
	stopReason?: string;
	errorMessage?: string;
	toolCalls: number;
}

/** The last assistant turn of a session branch: its text, why it stopped, and an error if there was one. */
export function lastAnswer(entries: ReadonlyArray<any>): Answer | undefined {
	for (let i = entries.length - 1; i >= 0; i--) {
		const m = entries[i]?.type === "message" ? entries[i].message : undefined;
		if (m?.role !== "assistant") continue;
		const blocks: any[] = Array.isArray(m.content) ? m.content : [];
		return {
			text: blocks.filter((b) => b?.type === "text").map((b) => b.text ?? "").join("\n").trim(),
			stopReason: m.stopReason,
			errorMessage: m.errorMessage,
			toolCalls: blocks.filter((b) => b?.type === "toolCall").length,
		};
	}
	return undefined;
}

export function stamp(d = new Date()): string {
	const p = (n: number) => String(n).padStart(2, "0");
	return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

export function slug(text: string): string {
	return text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32) || "task";
}
