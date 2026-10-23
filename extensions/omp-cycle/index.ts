// /cycle: plan -> code -> review -> fix until approved, each phase on its own model, inside omp.
//
//   /cycle [--rounds N] [--test CMD] [--plan M] [--code M] [--review M] [--fix M] [--timeout MIN] <task>
//   /cycle resume [id] | status | cancel | settings | set <key> <value>
//
// Models, rounds and the test command live in ~/.omp/agent/cycle.json; /cycle settings edits them in the TUI.
import type { ExtensionAPI, ExtensionCommandContext } from "@oh-my-pi/pi-coding-agent";
import { Cycle, Shared } from "./controller.ts";
import {
	applyOverrides, type Finding, type Phase, newRun, parseRun, type Settings, slug, stamp, words,
} from "./contract.ts";
import { changeReport, headSha, isRepo, loadSettings, runDir, runsFor, saveRun, saveSettings, settingsFile, type Exec } from "./store.ts";

const PHASES: Phase[] = ["plan", "code", "review", "fix"];
const HELP = [
	"/cycle [--rounds N] [--test CMD] [--plan M] [--code M] [--review M] [--fix M] [--timeout MIN] [--allow-fallback] <task>",
	"/cycle resume [id] [--allow-fallback]   carry on an interrupted run   /cycle status   /cycle cancel",
	"/cycle settings   edit models, rounds, tests    /cycle set <plan|code|review|fix|rounds|test|timeout|fallback> <value>",
].join("\n");

export default function cycle(pi: ExtensionAPI) {
	const z = pi.zod;
	const shared = new Shared();
	const exec: Exec = (cmd, args, opts) => pi.exec(cmd, args, opts);
	const engine = new Cycle(pi, shared, exec);
	pi.setLabel("Cycle");

	// ---- tools the reviewer uses; inactive except during a review -------------------------------------------

	pi.registerTool({
		name: "cycle_verdict",
		label: "Cycle verdict",
		description: "Record the review's verdict once the review is finished: approve, or changes with each finding.",
		defaultInactive: true,
		parameters: z.object({
			verdict: z.enum(["approve", "changes"]),
			summary: z.string(),
			findings: z.array(z.object({ file: z.string().optional(), problem: z.string(), fix: z.string().optional() })).optional(),
		}),
		async execute(_id: string, params: { verdict: "approve" | "changes"; summary: string; findings?: Finding[] }) {
			const a = shared.active;
			if (!a?.reviewing) throw new Error("No review is in progress.");
			const findings = (params.findings ?? []).filter((f) => f.problem?.trim());
			if (params.verdict === "changes" && !findings.length) throw new Error('A "changes" verdict needs at least one finding (file, problem, fix).');
			if (a.verdict && a.verdict.verdict !== params.verdict) a.conflict = true;
			a.verdictCalls++;
			a.verdict = { verdict: params.verdict, summary: params.summary, findings, source: "tool" };
			return {
				content: [{ type: "text", text: `Verdict recorded: ${params.verdict}${findings.length ? ` with ${findings.length} finding(s)` : ""}. Finish with the VERDICT line.` }],
				details: a.verdict,
			};
		},
	});

	pi.registerTool({
		name: "cycle_diff",
		label: "Cycle diff",
		description: "Show the changes made during this cycle (git status, diff against the start, new files), optionally for one path.",
		defaultInactive: true,
		parameters: z.object({ path: z.string().optional() }),
		async execute(_id: string, params: { path?: string }, _signal: unknown, _update: unknown, ctx: { cwd: string }) {
			const a = shared.active;
			if (!a) throw new Error("No cycle is running.");
			const p = params.path?.trim();
			if (p && (p.startsWith("-") || p.split("/").includes("..") || p.startsWith("/"))) throw new Error("path must be relative, inside the repository");
			const text = await changeReport(exec, ctx.cwd, a.run.baseSha, a.run.settings.diffLimitKb * 1024, p || undefined);
			return { content: [{ type: "text", text }], details: { path: p } };
		},
	});

	// ---- events ---------------------------------------------------------------------------------------------

	for (const name of ["session_start", "session_switch"] as const) {
		pi.on(name, (_e: unknown, ctx: any) => {
			if (ctx.agent?.kind !== "sub") shared.lastCtx = ctx;
		});
	}
	pi.on("agent_start", (_e: unknown, ctx: any) => {
		if (ctx.agent?.kind === "sub") return;
		shared.lastCtx = ctx;
		shared.active?.tracker?.onStart();
	});
	pi.on("agent_end", (e: any, ctx: any) => {
		if (ctx.agent?.kind === "sub") return;
		shared.lastCtx = ctx;
		if (e.willContinue || e.awaitingAsyncWork) return;
		shared.active?.tracker?.onEnd();
	});

	// the phase's tool allowlist, enforced as well as offered: read-only phases cannot edit, run shell or delegate
	pi.on("tool_call", (e: any, ctx: any) => {
		const p = shared.active?.policy;
		if (!p || ctx.agent?.kind === "sub") return;
		if (!p.allow.has(e.toolName)) {
			return { block: true, reason: `The ${p.phase} phase is read-only. Allowed tools: ${[...p.allow].join(", ")}.` };
		}
	});
	pi.on("before_subagent_spawn", () => {
		const p = shared.active?.policy;
		if (p) return { block: true, reason: `The ${p.phase} phase is read-only; no subagents.` };
	});

	pi.on("after_provider_response", (e: any) => {
		const a = shared.active;
		if (!a?.tracker) return;
		const h = e.headers ?? {};
		const get = (k: string): string | undefined => (typeof h.get === "function" ? h.get(k) : h[k]) || undefined;
		const by = get("x-hangar-served-by");
		if (by) a.servedBy.add(`${by}${get("x-hangar-engine") ? ` (${get("x-hangar-engine")})` : ""}`);
		const fb = get("x-hangar-fallback");
		if (fb) a.fallbacks.add(fb);
	});

	pi.on("session_shutdown", () => {
		const a = shared.active;
		if (a) {
			a.cancelled = true;
			a.abort.abort();
		}
	});

	// ---- /cycle ---------------------------------------------------------------------------------------------

	pi.registerCommand("cycle", {
		description: "Plan, code, review and fix until approved, each phase on its own model (see /cycle help)",
		handler: async (args: string, ctx: ExtensionCommandContext) => {
			const say = (t: string, level: "info" | "warning" | "error" = "info") => ctx.ui.notify(t, level);
			try {
				const first = words(args)[0] ?? "";
				const rest = args.trim().slice(first.length).trim();
				switch (first) {
					case "":
					case "help": return say(`${HELP}\n${statusText(ctx.cwd) ?? ""}`.trim());
					case "status": return say(statusText(ctx.cwd) ?? "No cycle runs here yet.");
					case "cancel": return cancel(say);
					case "settings": return await settingsUi(ctx, say);
					case "set": return setting(rest, say);
					case "resume": return await resume(ctx, rest, say);
					default: return await start(ctx, args, say);
				}
			} catch (e) {
				say(`cycle: ${(e as Error)?.message ?? e}`, "error");
			}
		},
	});

	// ---- command bodies -------------------------------------------------------------------------------------

	type Say = (t: string, level?: "info" | "warning" | "error") => void;

	function busy(ctx: ExtensionCommandContext, say: Say): boolean {
		if (shared.active) {
			say(`A cycle is already running (${shared.active.run.id}). /cycle cancel stops it.`, "warning");
			return true;
		}
		if (!ctx.isIdle()) {
			say("Wait for the current turn to finish first.", "warning");
			return true;
		}
		return false;
	}

	/** Every phase's model must resolve before anything starts, so a typo fails now, not an hour in. */
	function missingModels(ctx: ExtensionCommandContext, s: Settings): string[] {
		return PHASES.filter((p) => !ctx.models.resolve(s.models[p])).map((p) => `${p}: "${s.models[p]}"`);
	}

	async function start(ctx: ExtensionCommandContext, args: string, say: Say) {
		if (busy(ctx, say)) return;
		const parsed = parseRun(args);
		if (parsed.error) return say(`${parsed.error}\n${HELP}`, "error");
		if (!parsed.task) return say(HELP, "warning");
		const { settings, warnings } = loadSettings();
		warnings.forEach((w) => say(w, "warning"));
		const s = applyOverrides(settings, parsed.overrides);
		const missing = missingModels(ctx, s);
		if (missing.length) return say(`Models not available: ${missing.join(", ")}. Fix with /cycle settings or the --plan/--code/--review/--fix options.`, "error");
		if (!(await isRepo(exec, ctx.cwd))) {
			const r = await exec("git", ["init", "-b", "main"], { cwd: ctx.cwd });
			if (r.code !== 0) return say(`Not a git repository and git init failed: ${r.stderr.trim()}`, "error");
			say("Not a git repository: initialised one here (main branch).", "warning");
		}
		const run = newRun(`${slug(parsed.task)}-${stamp()}`, ctx.cwd, parsed.task, s, await headSha(exec, ctx.cwd));
		saveRun(run);
		say(`Run ${run.id}\nplan ${s.models.plan} · code ${s.models.code} · review ${s.models.review} · fix ${s.models.fix}\nFolder: ${runDir(run.id)}`);
		await engine.execute(ctx, run, false);
	}

	async function resume(ctx: ExtensionCommandContext, rest: string, say: Say) {
		if (busy(ctx, say)) return;
		const ws = words(rest);
		const id = ws.find((w) => !w.startsWith("--"));
		const runs = runsFor(ctx.cwd).filter((r) => r.status === "interrupted" || r.status === "cancelled" || r.status === "running");
		const run = id ? runsFor(ctx.cwd).find((r) => r.id === id) : runs[0];
		if (!run) return say(id ? `No run ${id} here.` : "Nothing to resume here (only interrupted or cancelled runs resume).", "warning");
		if (run.status === "approved" || run.step === "done") return say(`Run ${run.id} is already approved.`, "warning");
		if (ws.includes("--allow-fallback")) run.settings.allowFallback = true;
		const missing = missingModels(ctx, run.settings);
		if (missing.length) return say(`Models not available: ${missing.join(", ")}.`, "error");
		say(`Resuming ${run.id} at ${run.step} (round ${run.round}).`);
		await engine.execute(ctx, run, true);
	}

	function cancel(say: Say) {
		const a = shared.active;
		if (!a) return say("No cycle is running.", "warning");
		a.cancelled = true;
		a.abort.abort();
		(shared.lastCtx as any)?.abort?.();
		say(`Cancelling ${a.run.id}; it can be resumed with /cycle resume.`);
	}

	function statusText(cwd: string): string | undefined {
		const run = shared.active?.run ?? runsFor(cwd)[0];
		if (!run) return undefined;
		return [
			`Latest run ${run.id}: ${shared.active ? "running" : run.status}${run.stopReason ? ` (${run.stopReason})` : ""}`,
			`Step ${run.step}, review round ${run.round}/${run.settings.rounds}. Task: ${run.task}`,
			run.served.length ? `Served by: ${run.served.slice(-4).join("; ")}` : undefined,
			`Folder: ${runDir(run.id)}`,
		].filter(Boolean).join("\n");
	}

	// ---- settings -------------------------------------------------------------------------------------------

	function setting(rest: string, say: Say) {
		const ws = words(rest);
		const key = ws[0];
		const value = ws.slice(1).join(" ").trim();
		const { settings } = loadSettings();
		if (!key) return say(`${settingsText(settings)}\nFile: ${settingsFile()}\nChange with /cycle set <plan|code|review|fix|rounds|test|timeout|fallback> <value>.`);
		if (!value && key !== "test") return say(`/cycle set ${key} needs a value.`, "error");
		const whole = () => {
			const n = Number(value);
			if (!Number.isInteger(n) || n < 1) throw new Error(`${key} must be a whole number of 1 or more`);
			return n;
		};
		if ((PHASES as string[]).includes(key)) settings.models[key as Phase] = value;
		else if (key === "rounds") settings.rounds = whole();
		else if (key === "timeout") settings.stepTimeoutMin = whole();
		else if (key === "test") settings.testCommand = value;
		else if (key === "fallback") settings.allowFallback = /^(on|yes|true|1)$/i.test(value);
		else return say(`Unknown setting "${key}".`, "error");
		saveSettings(settings);
		say(`Saved.\n${settingsText(settings)}`);
	}

	function settingsText(s: Settings): string {
		return [
			`Models: plan ${s.models.plan} · code ${s.models.code} · review ${s.models.review} · fix ${s.models.fix}`,
			`Rounds: ${s.rounds} · step limit: ${s.stepTimeoutMin} min · tests: ${s.testCommand || "(none)"} · fallback: ${s.allowFallback ? "allowed" : "stops the run"}`,
		].join("\n");
	}

	async function settingsUi(ctx: ExtensionCommandContext, say: Say) {
		if (!ctx.hasUI) return setting("", say);
		for (;;) {
			const { settings: s } = loadSettings();
			const rows = [
				...PHASES.map((p) => `${p[0].toUpperCase()}${p.slice(1)} model: ${s.models[p]}`),
				`Review rounds: ${s.rounds}`,
				`Test command: ${s.testCommand || "(none)"}`,
				`Step time limit: ${s.stepTimeoutMin} min`,
				`Fallback answers: ${s.allowFallback ? "allowed" : "stop the run"}`,
				"Done",
			];
			const pick = await ctx.ui.select("Cycle settings", rows);
			if (!pick || pick === "Done") return;
			const idx = rows.indexOf(pick);
			if (idx < 4) {
				const phase = PHASES[idx];
				const names = ctx.models.list().map((m: any) => `${m.provider}/${m.id}`);
				const choice = await ctx.ui.select(`${phase} model`, [...new Set([s.models[phase], ...names])]);
				if (choice) s.models[phase] = choice;
			} else if (idx === 4 || idx === 6) {
				const label = idx === 4 ? "Review rounds" : "Step time limit (minutes)";
				const v = Number(await ctx.ui.input(label, String(idx === 4 ? s.rounds : s.stepTimeoutMin)));
				if (Number.isInteger(v) && v >= 1) {
					if (idx === 4) s.rounds = v;
					else s.stepTimeoutMin = v;
				}
			} else if (idx === 5) {
				const v = await ctx.ui.input("Test command (empty for none)", s.testCommand);
				if (v !== undefined) s.testCommand = v.trim();
			} else if (idx === 7) {
				s.allowFallback = !s.allowFallback;
			}
			saveSettings(s);
		}
	}
}
