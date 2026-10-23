// Drives one run: a session per phase, the phase's model and tools, one prompt at a time, and the stop rules.
import type { ExtensionAPI, ExtensionCommandContext, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import {
	afterReview, type Answer, codePrompt, fingerprint, fixPrompt, lastAnswer, listFindings, type Phase, planPrompt,
	READ_ONLY, resumeCodePrompt, resumeFixPrompt, reviewPrompt, type RunState, type Status, testFailPrompt,
	type Verdict, verdictFromText, MAX_TEST_FAILS,
} from "./contract.ts";
import { changeReport, type Exec, runDir, runTests, saveRun, snapshot, writeArtifact } from "./store.ts";

export const CYCLE_TOOLS = ["cycle_verdict", "cycle_diff"];
const READ_TOOLS = ["read", "grep", "glob", "find", "ls"];
const START_WAIT_MS = 180_000;
const IDLE_WAIT_MS = 120_000;

/** A run stops (or is interrupted) with a status and a reason; the step it was on is kept for resuming. */
export class Stop extends Error {
	status: Status;
	reason: string;
	constructor(status: Status, reason: string) {
		super(reason);
		this.status = status;
		this.reason = reason;
	}
}

class Tracker {
	started = false;
	finished = false;
	private resolve!: () => void;
	done = new Promise<void>((r) => (this.resolve = r));
	onStart() {
		this.started = true;
	}
	onEnd() {
		if (!this.started) return;
		this.finished = true;
		this.resolve();
	}
}

export interface Active {
	run: RunState;
	abort: AbortController;
	cancelled: boolean;
	policy?: { phase: Phase; allow: Set<string> };
	tracker?: Tracker;
	reviewing: boolean;
	verdict?: Verdict;
	verdictCalls: number;
	conflict: boolean;
	servedBy: Set<string>;
	fallbacks: Set<string>;
}

/** What the event handlers and the controller share, per extension instance. */
export class Shared {
	active?: Active;
	lastCtx?: ExtensionContext;
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

export class Cycle {
	private a!: Active;
	private cmd!: ExtensionCommandContext;
	private tests = "";
	private resuming = false;
	private baseTools: string[] = [];

	private pi: ExtensionAPI;
	private shared: Shared;
	private exec: Exec;

	constructor(pi: ExtensionAPI, shared: Shared, exec: Exec) {
		this.pi = pi;
		this.shared = shared;
		this.exec = exec;
	}

	// ---- plumbing ---------------------------------------------------------------------------------------------

	private get run(): RunState {
		return this.a.run;
	}

	private say(text: string, level: "info" | "warning" | "error" = "info") {
		this.run.log.push(`${new Date().toISOString()} ${text}`);
		try {
			(this.shared.lastCtx ?? this.cmd).ui.notify(`cycle: ${text}`, level);
		} catch {}
	}

	private status(text: string | undefined) {
		try {
			(this.shared.lastCtx ?? this.cmd).ui.setStatus("cycle", text);
		} catch {}
	}

	private sm() {
		return (this.shared.lastCtx ?? this.cmd).sessionManager;
	}

	private sessionFile(): string | undefined {
		try {
			return this.sm().getSessionFile() ?? undefined;
		} catch {
			return undefined;
		}
	}

	private sessionId(): string | undefined {
		try {
			return this.sm().getSessionId();
		} catch {
			return undefined;
		}
	}

	private checkCancel() {
		if (this.a.cancelled) throw new Stop("cancelled", "cancelled with /cycle cancel");
	}

	// ---- entry point ------------------------------------------------------------------------------------------

	async execute(cmd: ExtensionCommandContext, run: RunState, resuming: boolean): Promise<void> {
		this.cmd = cmd;
		this.resuming = resuming;
		this.tests = "";
		this.a = {
			run, abort: new AbortController(), cancelled: false, reviewing: false, verdictCalls: 0, conflict: false,
			servedBy: new Set(), fallbacks: new Set(),
		};
		this.shared.active = this.a;
		this.baseTools = this.pi.getActiveTools().filter((t) => !CYCLE_TOOLS.includes(t));
		const saved = { tools: this.baseTools, model: cmd.model, thinking: this.pi.getThinkingLevel() };
		run.status = "running";
		run.stopReason = undefined;
		run.sessions.orig ??= this.sessionFile();
		saveRun(run);
		try {
			while (run.step !== "done") {
				this.checkCancel();
				this.status(`cycle: ${run.step} · round ${run.round}/${run.settings.rounds} · Esc interrupts, /cycle cancel stops`);
				switch (run.step) {
					case "plan": await this.plan(); break;
					case "code": await this.code(); break;
					case "fix": await this.fix(); break;
					case "test": await this.test(); break;
					case "review": await this.review(); break;
				}
				saveRun(run);
			}
		} catch (e) {
			if (e instanceof Stop) {
				run.status = e.status;
				run.stopReason = e.reason;
			} else {
				run.status = "interrupted";
				run.stopReason = `unexpected error: ${(e as Error)?.message ?? e}`;
			}
		} finally {
			await this.finish(saved);
			saveRun(run);
			this.shared.active = undefined;
			this.status(undefined);
		}
	}

	/** Back to the coder's session with the user's own model and tools, and a summary that stays in the transcript. */
	private async finish(saved: { tools: string[]; model: unknown; thinking: unknown }) {
		const run = this.run;
		this.a.policy = undefined;
		try {
			if (run.sessions.code && this.sessionFile() !== run.sessions.code) await this.cmd.switchSession(run.sessions.code);
		} catch (e) {
			this.say(`could not return to the coder's session: ${(e as Error)?.message ?? e}`, "warning");
		}
		try {
			await this.pi.setActiveTools(saved.tools);
			if (saved.model) await this.pi.setModel(saved.model as any);
			if (saved.thinking) this.pi.setThinkingLevel(saved.thinking as any);
		} catch {}
		const text = this.summary();
		this.say(text.split("\n")[0], run.status === "approved" ? "info" : "warning");
		try {
			this.pi.sendMessage({ customType: "hangar.cycle", content: text, display: true } as any);
		} catch {}
	}

	summary(): string {
		const run = this.run;
		const m = run.settings.models;
		const head = run.status === "approved"
			? `Cycle ${run.id} approved after ${run.round} review round(s).`
			: `Cycle ${run.id} ${run.status} at ${run.step}${run.stopReason ? `: ${run.stopReason}` : ""}.`;
		const next = run.status === "approved"
			? "Look over `git diff`, then commit when you are happy."
			: run.status === "interrupted" || run.status === "cancelled"
				? "Carry on with /cycle resume."
				: "Fix by hand or start again with /cycle.";
		return [
			head, `Task: ${run.task}`,
			`Models: plan ${m.plan} · code ${m.code} · review ${m.review} · fix ${m.fix}`,
			run.served.length ? `Served by: ${run.served.join("; ")}` : "Served by: unknown (no Hangar headers seen)",
			`Run folder: ${runDir(run.id)}`, next,
		].join("\n");
	}

	// ---- one phase's setup and one prompt ---------------------------------------------------------------------

	private async enter(phase: Phase, how: { newFrom?: string; switchTo?: string }) {
		const before = this.sessionId();
		const res = how.switchTo ? await this.cmd.switchSession(how.switchTo) : await this.cmd.newSession({ parentSession: how.newFrom });
		if (res.cancelled) throw new Stop("interrupted", "another extension cancelled the session change");
		if (!how.switchTo && this.sessionId() === before) {
			if (this.cmd.sessionManager.getSessionId() === before) throw new Stop("interrupted", "could not confirm the new session");
			this.shared.lastCtx = this.cmd;
		}
		const spec = this.run.settings.models[phase];
		const model = this.cmd.models.resolve(spec);
		if (!model) throw new Stop("interrupted", `model "${spec}" is not available (try /cycle set ${phase} <model>)`);
		if (!(await this.pi.setModel(model))) throw new Stop("interrupted", `no API key for model "${spec}"`);
		const thinking = this.run.settings.thinking[phase];
		if (thinking) this.pi.setThinkingLevel(thinking as any);

		const all = new Set(this.pi.getAllTools().map((t: any) => t.name as string));
		if (READ_ONLY.has(phase)) {
			const allow = new Set(READ_TOOLS.filter((t) => all.has(t)));
			if (phase === "review") CYCLE_TOOLS.forEach((t) => allow.add(t));
			await this.pi.setActiveTools([...allow]);
			this.a.policy = { phase, allow };
		} else {
			await this.pi.setActiveTools(this.baseTools);
			this.a.policy = undefined;
		}
		this.say(`${phase}: ${spec}${how.switchTo ? " (back in the coder's session)" : ""}`);
	}

	/** Send one prompt and wait for the agent's run to end, treating anything but a clean stop as an interruption. */
	private async step(label: string, phase: Phase, prompt: string): Promise<Answer> {
		const a = this.a;
		const run = this.run;
		await this.cmd.waitForIdle();                  // a message the user typed between steps must finish first
		const tracker = new Tracker();
		a.tracker = tracker;
		a.verdict = undefined;
		a.verdictCalls = 0;
		a.conflict = false;
		a.servedBy.clear();
		a.fallbacks.clear();
		saveRun(run);
		this.pi.sendUserMessage(prompt);

		const limit = run.settings.stepTimeoutMin * 60_000;
		const began = Date.now();
		let timedOut = false;
		let aborted = false;
		while (!tracker.finished) {
			await Promise.race([tracker.done, sleep(2000)]);
			if (tracker.finished) break;
			const elapsed = Date.now() - began;
			const why = a.cancelled ? "cancelled"
				: !tracker.started && elapsed > START_WAIT_MS ? "no-start"
				: elapsed > limit ? "timeout" : "";
			if (!why) continue;
			if (!aborted) {
				this.cmd.abort();
				aborted = true;
			}
			if (why === "no-start") {
				a.tracker = undefined;
				throw new Stop("interrupted", "the prompt never started a turn");
			}
			timedOut = why === "timeout";
			if (elapsed > limit + IDLE_WAIT_MS) break;
		}
		await Promise.race([this.cmd.waitForIdle(), sleep(IDLE_WAIT_MS)]);
		a.tracker = undefined;
		this.recordSession(phase);
		this.checkCancel();
		if (timedOut) throw new Stop("interrupted", `${label} ran past ${run.settings.stepTimeoutMin} minutes`);

		const ans = lastAnswer(this.sm().getBranch());
		if (!ans) throw new Stop("interrupted", `${label}: no answer was recorded`);
		if (ans.stopReason && ans.stopReason !== "stop") {
			throw new Stop("interrupted", `${label} ended early (${ans.stopReason}${ans.errorMessage ? `: ${ans.errorMessage}` : ""})`);
		}
		for (const s of a.servedBy) {
			const line = `${label}: ${s}`;
			if (!run.served.includes(line)) run.served.push(line);
		}
		if (a.fallbacks.size && !run.settings.allowFallback) {
			throw new Stop("interrupted", `${label} was answered by a fallback (${[...a.fallbacks].join(", ")}) rather than ${run.settings.models[phase]}; re-run with --allow-fallback to accept that`);
		}
		return ans;
	}

	private recordSession(phase: Phase) {
		const file = this.sessionFile();
		if (!file) return;
		if (phase === "plan") this.run.sessions.plan = file;
		else if (phase === "code" || phase === "fix") this.run.sessions.code = file;
	}

	private tree() {
		return snapshot(this.exec, this.run.cwd, this.run.baseSha);
	}

	// ---- phases -----------------------------------------------------------------------------------------------

	private async plan() {
		const run = this.run;
		await this.enter("plan", { newFrom: run.sessions.orig });
		const before = await this.tree();
		const ans = await this.step("plan", "plan", planPrompt(run.task));
		if (!ans.text) throw new Stop("interrupted", "the planner gave an empty plan");
		run.plan = ans.text;
		writeArtifact(run, "plan.md", `${ans.text}\n`);
		if ((await this.tree()) !== before) this.say("the planner changed files despite read-only tools; check git status", "warning");
		run.step = "code";
	}

	private async code() {
		const run = this.run;
		const resumeIn = this.resuming ? run.sessions.code : undefined;
		this.resuming = false;
		if (resumeIn) {
			await this.enter("code", { switchTo: resumeIn });
			await this.step("code", "code", resumeCodePrompt(run.settings.testCommand));
		} else {
			await this.enter("code", { newFrom: run.sessions.plan });
			await this.step("code", "code", codePrompt(run.task, run.plan ?? "", run.settings.testCommand));
		}
		run.step = "test";
	}

	private async fix() {
		const run = this.run;
		const findings = listFindings(run.findings);
		const resumeIn = this.resuming ? run.sessions.code : undefined;
		this.resuming = false;
		if (run.sessions.code) {
			await this.enter("fix", { switchTo: run.sessions.code });
			await this.step("fix", "fix", resumeIn ? resumeFixPrompt(findings) : fixPrompt(findings, run.settings.testCommand));
		} else {
			await this.enter("fix", { newFrom: run.sessions.plan });
			await this.step("fix", "fix", `${codePrompt(run.task, run.plan ?? "", run.settings.testCommand)}\n\nThen fix these review findings:\n${findings}`);
		}
		run.step = "test";
	}

	private async test() {
		const run = this.run;
		const cmd = run.settings.testCommand;
		if (!cmd) {
			run.step = "review";
			return;
		}
		this.say(`tests: ${cmd}`);
		const r = await runTests(this.exec, run.cwd, cmd, run.settings.testTimeoutMin, this.a.abort.signal);
		writeArtifact(run, `test-${run.round}-${run.testFails}.txt`, `$ ${cmd}\n${r.output}\n`);
		this.checkCancel();
		if (r.ok) {
			this.tests = `Tests pass (\`${cmd}\`).\n\n`;
			run.testFails = 0;
			run.step = "review";
			return;
		}
		run.testFails++;
		this.say(`tests failed (${run.testFails}/${MAX_TEST_FAILS})`, "warning");
		if (run.testFails >= MAX_TEST_FAILS) throw new Stop("stopped", `the tests still fail after ${run.testFails} attempts`);
		this.tests = `Tests FAIL (\`${cmd}\`) but are listed for context.\n\n`;
		run.findings = [{ file: "tests", problem: testFailPrompt(cmd, r.output) }];
		run.step = "fix";
	}

	private async review() {
		const run = this.run;
		const a = this.a;
		run.round++;
		const before = await this.tree();
		run.reviewedSnapshot = before;
		const report = await changeReport(this.exec, run.cwd, run.baseSha, run.settings.diffLimitKb * 1024);
		await this.enter("review", { newFrom: run.sessions.code ?? run.sessions.plan });
		a.reviewing = true;
		let ans: Answer;
		try {
			ans = await this.step(`review ${run.round}`, "review", reviewPrompt(run.task, run.plan ?? "", report, this.tests));
		} finally {
			a.reviewing = false;
		}
		const text = verdictFromText(ans.text);
		const verdict = a.verdict ?? text;
		const conflict = a.conflict || (!!a.verdict && !!text && a.verdict.verdict !== text.verdict);
		const treeUnchanged = (await this.tree()) === before;
		writeArtifact(run, `review-${run.round}.md`, `${ans.text}\n\n---\n${JSON.stringify({ verdict, conflict, treeUnchanged }, null, 2)}\n`);

		const d = afterReview(run, { verdict, conflict, treeUnchanged });
		if (d.kind === "approve") {
			run.status = "approved";
			run.step = "done";
		} else if (d.kind === "fix") {
			run.fingerprints.push(fingerprint(verdict!.findings));
			run.findings = verdict!.findings;
			this.say(`review ${run.round}: ${verdict!.findings.length} finding(s); fixing`);
			run.step = "fix";
		} else {
			throw new Stop("stopped", d.reason);
		}
	}
}
