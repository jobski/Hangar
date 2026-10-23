// Everything on disk or in git: settings, the run journal (outside the repo), tree snapshots, diffs, test runs.
import { createHash } from "node:crypto";
import { mkdirSync, readdirSync, readFileSync, renameSync, statSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { DEFAULTS, type RunState, type Settings, truncateMid } from "./contract.ts";

export type Exec = (
	cmd: string,
	args: string[],
	opts?: { cwd?: string; timeout?: number; signal?: AbortSignal },
) => Promise<{ stdout: string; stderr: string; code: number; killed: boolean }>;

const agentDir = () => process.env.PI_CODING_AGENT_DIR || join(homedir(), ".omp", "agent");
export const settingsFile = () => join(agentDir(), "cycle.json");
const runsDir = () => join(agentDir(), "cycle", "runs");
export const runDir = (id: string) => join(runsDir(), id);

// ---- settings --------------------------------------------------------------------------------------------------

/** Settings from cycle.json over the defaults; a bad field is reported and ignored, never fatal. */
export function loadSettings(): { settings: Settings; warnings: string[] } {
	const warnings: string[] = [];
	let raw: any = {};
	try {
		raw = JSON.parse(readFileSync(settingsFile(), "utf8"));
	} catch (e: any) {
		if (e?.code !== "ENOENT") warnings.push(`cycle.json unreadable (${e?.message ?? e}); using defaults`);
	}
	const s: Settings = { ...DEFAULTS, models: { ...DEFAULTS.models }, thinking: {} };
	const num = (k: "rounds" | "stepTimeoutMin" | "testTimeoutMin" | "diffLimitKb") => {
		if (raw[k] === undefined) return;
		if (Number.isInteger(raw[k]) && raw[k] >= 1) s[k] = raw[k];
		else warnings.push(`cycle.json: ${k} must be a whole number of 1 or more`);
	};
	(["rounds", "stepTimeoutMin", "testTimeoutMin", "diffLimitKb"] as const).forEach(num);
	if (typeof raw.testCommand === "string") s.testCommand = raw.testCommand;
	if (typeof raw.allowFallback === "boolean") s.allowFallback = raw.allowFallback;
	for (const p of ["plan", "code", "review", "fix"] as const) {
		if (typeof raw.models?.[p] === "string" && raw.models[p].trim()) s.models[p] = raw.models[p].trim();
		else if (raw.models?.[p] !== undefined) warnings.push(`cycle.json: models.${p} must be a model name`);
		if (typeof raw.thinking?.[p] === "string") s.thinking[p] = raw.thinking[p];
	}
	return { settings: s, warnings };
}

export function saveSettings(s: Settings): void {
	mkdirSync(agentDir(), { recursive: true });
	atomicWrite(settingsFile(), `${JSON.stringify(s, null, 2)}\n`);
}

// ---- journal ---------------------------------------------------------------------------------------------------

function atomicWrite(path: string, text: string): void {
	const tmp = `${path}.${process.pid}.tmp`;
	writeFileSync(tmp, text);
	renameSync(tmp, path);
}

export function saveRun(run: RunState): void {
	mkdirSync(runDir(run.id), { recursive: true });
	atomicWrite(join(runDir(run.id), "state.json"), JSON.stringify(run, null, 2));
}

export function writeArtifact(run: RunState, name: string, text: string): void {
	mkdirSync(runDir(run.id), { recursive: true });
	writeFileSync(join(runDir(run.id), name), text);
}

export function loadRun(id: string): RunState | undefined {
	try {
		const run = JSON.parse(readFileSync(join(runDir(id), "state.json"), "utf8")) as RunState;
		return run.version === 1 ? run : undefined;
	} catch {
		return undefined;
	}
}

/** This directory's most recent run, newest first. */
export function runsFor(cwd: string): RunState[] {
	let ids: string[] = [];
	try {
		ids = readdirSync(runsDir());
	} catch {
		return [];
	}
	return ids
		.map((id) => {
			try {
				return { id, at: statSync(join(runDir(id), "state.json")).mtimeMs };
			} catch {
				return undefined;
			}
		})
		.filter((x): x is { id: string; at: number } => !!x)
		.sort((a, b) => b.at - a.at)
		.map((x) => loadRun(x.id))
		.filter((r): r is RunState => !!r && r.cwd === cwd);
}

// ---- git: snapshots and diffs ----------------------------------------------------------------------------------

const MAX_HASHED = 50 * 1024 * 1024;

async function git(exec: Exec, cwd: string, args: string[]): Promise<{ out: string; ok: boolean }> {
	const r = await exec("git", args, { cwd, timeout: 60_000 });
	return { out: r.stdout, ok: r.code === 0 };
}

export async function headSha(exec: Exec, cwd: string): Promise<string> {
	const r = await git(exec, cwd, ["rev-parse", "--verify", "HEAD"]);
	return r.ok ? r.out.trim() : "none";
}

export async function isRepo(exec: Exec, cwd: string): Promise<boolean> {
	return (await git(exec, cwd, ["rev-parse", "--is-inside-work-tree"])).out.trim() === "true";
}

async function untracked(exec: Exec, cwd: string): Promise<string[]> {
	const r = await git(exec, cwd, ["ls-files", "--others", "--exclude-standard", "-z"]);
	return r.out.split("\0").filter(Boolean).sort();
}

/** A hash of the tracked changes since `base` plus the content of every untracked file. */
export async function snapshot(exec: Exec, cwd: string, base: string): Promise<string> {
	const h = createHash("sha256");
	const diff = base === "none" ? await git(exec, cwd, ["diff", "--binary"]) : await git(exec, cwd, ["diff", "--binary", base]);
	h.update(diff.out).update("\0");
	for (const f of await untracked(exec, cwd)) {
		h.update(f).update("\0");
		try {
			const p = join(cwd, f);
			if (statSync(p).size > MAX_HASHED) h.update(`size:${statSync(p).size}`);
			else h.update(readFileSync(p));
		} catch {
			h.update("unreadable");
		}
		h.update("\0");
	}
	return h.digest("hex");
}

/** The status, the diff since `base`, and new files' content (each capped), within `limitBytes` overall. */
export async function changeReport(exec: Exec, cwd: string, base: string, limitBytes: number, path?: string): Promise<string> {
	const only = path ? ["--", path] : [];
	const status = await git(exec, cwd, ["status", "--short", ...only]);
	const diff = base === "none" ? await git(exec, cwd, ["diff", ...only]) : await git(exec, cwd, ["diff", base, ...only]);
	const parts = [`$ git status --short\n${status.out.trim() || "(clean)"}`, `$ git diff ${base === "none" ? "" : `${base.slice(0, 10)} `}${path ? `-- ${path}` : ""}\n${diff.out.trim() || "(no changes to tracked files)"}`];
	for (const f of await untracked(exec, cwd)) {
		if (path && f !== path && !f.startsWith(`${path.replace(/\/$/, "")}/`)) continue;
		try {
			const text = readFileSync(join(cwd, f), "utf8");
			parts.push(`new file ${f}:\n${text.includes("\0") ? "(binary)" : truncateMid(text, 6000)}`);
		} catch {
			parts.push(`new file ${f}: (unreadable)`);
		}
	}
	return truncateMid(parts.join("\n\n"), limitBytes);
}

// ---- tests -----------------------------------------------------------------------------------------------------

export async function runTests(exec: Exec, cwd: string, cmd: string, timeoutMin: number, signal: AbortSignal) {
	const r = await exec("bash", ["-lc", cmd], { cwd, timeout: timeoutMin * 60_000, signal });
	const out = truncateMid(`${r.stdout}${r.stderr ? `\n[stderr]\n${r.stderr}` : ""}`.trim(), 6000);
	return { ok: r.code === 0 && !r.killed, killed: r.killed, output: r.killed ? `${out}\n[killed: timeout or cancelled]` : out };
}
