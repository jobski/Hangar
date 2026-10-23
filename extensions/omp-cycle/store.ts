// Everything on disk or in git: settings, the run journal (outside the repo), tree snapshots, diffs, test runs.
import { createHash } from "node:crypto";
import { lstatSync, mkdirSync, readFileSync, readdirSync, readlinkSync, renameSync, statSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { DEFAULTS, type RunState, type Settings, truncateMid } from "./contract.ts";

export interface ExecResult {
	stdout: string;
	stderr: string;
	code: number;
	killed: boolean;
}

export type Exec = (
	cmd: string,
	args: string[],
	opts?: { cwd?: string; timeout?: number; signal?: AbortSignal },
) => Promise<ExecResult>;

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
const MAX_REPORTED = 1024 * 1024; // a new file bigger than this is described, never read into the report

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
	// Without a base commit the work can already be staged, and `git diff` alone would miss it.
	const [diffs, files] = await Promise.all([
		base === "none"
			? Promise.all([git(exec, cwd, ["diff", "--binary"]), git(exec, cwd, ["diff", "--binary", "--cached"])])
			: Promise.all([git(exec, cwd, ["diff", "--binary", base])]),
		untracked(exec, cwd),
	]);
	for (const d of diffs) h.update(d.out).update("\0");
	for (const f of files) {
		h.update(f).update("\0");
		try {
			const p = join(cwd, f);
			const st = lstatSync(p); // never follow a link: a new file can point at /dev/zero
			if (!st.isFile()) h.update(st.isSymbolicLink() ? `link:${readlinkSync(p)}` : "not a regular file");
			else if (st.size > MAX_HASHED) h.update(`size:${st.size}`);
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
	const tail = path ? ` -- ${path}` : "";
	const none = base === "none";
	const [status, files, unstaged, staged, since] = await Promise.all([
		git(exec, cwd, ["status", "--short", ...only]),
		untracked(exec, cwd),
		none ? git(exec, cwd, ["diff", ...only]) : Promise.resolve(null),
		none ? git(exec, cwd, ["diff", "--cached", ...only]) : Promise.resolve(null),
		none ? Promise.resolve(null) : git(exec, cwd, ["diff", base, ...only]),
	]);
	const body = (r: { out: string } | null, empty: string) => (r?.out ?? "").trim() || empty;
	const parts = none
		? [
				`$ git status --short\n${body(status, "(clean)")}`,
				`$ git diff${tail}\n${body(unstaged, "(no unstaged changes to tracked files)")}`,
				`$ git diff --cached${tail} (this run has no base commit, so staged work is shown apart)\n${body(staged, "(nothing staged)")}`,
			]
		: [
				`$ git status --short\n${body(status, "(clean)")}`,
				`$ git diff ${base.slice(0, 10)}${tail}\n${body(since, "(no changes to tracked files)")}`,
			];
	for (const f of files) {
		if (path && f !== path && !f.startsWith(`${path.replace(/\/$/, "")}/`)) continue;
		try {
			const p = join(cwd, f);
			const st = lstatSync(p); // a link's target could be /dev/zero or a pipe: show the link, never read it
			if (!st.isFile()) {
				parts.push(`new ${st.isSymbolicLink() ? `link ${f} -> ${readlinkSync(p)}` : `entry ${f}: not a regular file`}`);
				continue;
			}
			if (st.size > MAX_REPORTED) {
				parts.push(`new file ${f}: ${st.size} bytes, too large to show here - read it with the read tool`);
				continue;
			}
			const text = readFileSync(p, "utf8");
			parts.push(`new file ${f}:\n${text.includes("\0") ? "(binary)" : truncateMid(text, 6000)}`);
		} catch {
			parts.push(`new file ${f}: (unreadable)`);
		}
	}
	return truncateMid(parts.join("\n\n"), limitBytes);
}

// ---- tests -----------------------------------------------------------------------------------------------------

/** A shell that is not there at all (a spawn that failed, or the OS rejecting it) - not a failing test. */
function shellMissing(x: { code?: number | string; stderr?: string; message?: string }): boolean {
	if (x.code === "ENOENT" || x.code === "EACCES") return true;
	if (/enoent|no such file or directory/i.test(x.message ?? "")) return true;
	return x.code === 127 && (/bash\b[^\n]*(not found|no such file|is not recognized)/i.test(x.stderr ?? "") || /not found: bash/i.test(x.stderr ?? ""));
}

/** The test command in a login shell; where bash is missing (Hangar runs on Windows too), the same command in that machine's shell. */
export async function runTests(exec: Exec, cwd: string, cmd: string, timeoutMin: number, signal: AbortSignal) {
	const opts = { cwd, timeout: timeoutMin * 60_000, signal };
	const shells: Array<[string, string[]]> = [["bash", ["-lc", cmd]], process.platform === "win32" ? ["cmd", ["/c", cmd]] : ["sh", ["-lc", cmd]]];
	let r: ExecResult | undefined;
	let missing = "";
	for (const [bin, args] of shells) {
		try {
			const got = await exec(bin, args, opts);
			if (!shellMissing({ code: got.code, stderr: got.stderr })) {
				r = got;
				break;
			}
			missing = `${bin}: ${(got.stderr || got.stdout).trim().split("\n")[0] || "not available"}`;
		} catch (e: unknown) {
			const err = e as { code?: string; message?: string }; // a spawn failure carries its errno and message
			if (signal.aborted || !shellMissing({ code: err.code, message: err.message })) throw e;
			missing = `${bin}: ${err.message ?? e}`;
		}
	}
	if (!r) {
		const shell = shells[1][0];
		return { ok: false, killed: false, output: truncateMid(`Could not run the tests: this machine has neither bash nor ${shell} (${missing})`, 6000) };
	}
	const out = truncateMid(`${r.stdout}${r.stderr ? `\n[stderr]\n${r.stderr}` : ""}`.trim(), 6000);
	return { ok: r.code === 0 && !r.killed, killed: r.killed, output: r.killed ? `${out}\n[killed: timeout or cancelled]` : out };
}
