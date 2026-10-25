import { Plugin } from "@opencode/plugin";
import { createHash } from "node:crypto";
import { lstat, realpath } from "node:fs/promises";
import { join, relative } from "node:path";
import { Controller, type Result } from "./controller.ts";
import { externalAgent, patchPaths, processRun } from "./agents.ts";
import { clip, id, readOnly, validateAssignment, type Profile, type State, type Worker } from "./model.ts";
import { missionRPC } from "./rpc.ts";
import { missionReport } from "./report.ts";
import { changes, head, snapshot } from "./workspace.ts";
import { QUESTION_INSTRUCTIONS, QUESTION_TOOL, questionOffers } from "./questions.ts";
import { roleLabel, toolActivity, working } from "./progress.ts";

type Context = Plugin.Context;
type SessionID = Parameters<Context["session"]["get"]>[0]["sessionID"];
type ModelRef = NonNullable<Parameters<Context["session"]["create"]>[0]>["model"];
interface Managed { worker: Worker; profile: Profile; result?: Result; finalising?: boolean; conflict?: boolean; expected?: { machine: string; profile: string }; signal: AbortSignal }
const READ_TOOLS = new Set(["mission_read", "mission_glob", "mission_grep", "mission_diff", "mission_result"]);
const WORK_TOOLS = new Set(["read", "glob", "grep", "write", "edit", "patch", "apply_patch", "shell", "question", "mission_ask_expert", ...READ_TOOLS]);
const critical = (path: string) => /(^|\/)(\.git|\.github|\.env(?:\..*)?|Jenkinsfile|\.gitlab-ci\.yml)(\/|$)|(?:\.tfvars(?:\.json)?|\.tfstate(?:\..*)?|(?:package-lock|npm-shrinkwrap)\.json|(?:pnpm-lock|yarn)\.ya?ml|yarn\.lock|bun\.lockb?|Cargo\.lock|poetry\.lock|uv\.lock|go\.sum)$|(^|\/)(migrations|prod|production)(\/|$)/i.test(path);
const modelRef = (name: string): ModelRef => { const slash = name.indexOf("/"); return { providerID: name.slice(0, slash), id: name.slice(slash + 1) } as ModelRef; };

export default Plugin.define({
  id: "hangar.mission",
  async setup(ctx) {
    const managed = new Map<string, Managed>();
    const expectedModels = new Map<string, { machine: string; profile: string }>();
    const adviceParents = new Set<string>();
    const attention = new Map<string, Map<string, "permission" | "question">>();
    let mainWorker: Worker | undefined;
    const lifetime = new AbortController();
    let saveQueue = Promise.resolve();
    let progressTimer: ReturnType<typeof setTimeout> | undefined;
    const directory = await realpath(ctx.location.directory);
    const stateKey = `mission.workspace.${createHash("sha256").update(directory).digest("hex")}`;
    const controller = new Controller(directory, {
      save: state => {
        saveQueue = saveQueue.catch(() => {}).then(() => ctx.storage.set(stateKey, JSON.parse(JSON.stringify(state))));
        return saveQueue;
      },
      interrupt: async sessionID => { if (!sessionID.includes(":")) await ctx.session.interrupt({ sessionID: sessionID as SessionID }); },
      idle: async sessionID => {
        try { await ctx.session.wait({ sessionID: sessionID as SessionID }, { signal: AbortSignal.timeout(300) }); return true; }
        catch { return false; }
      },
      deliver: async (sessionID, text) => { await ctx.session.synthetic({ sessionID: sessionID as SessionID, text, description: "Wingman expert advice", delivery: "queue", metadata: { hangarMissionAdvice: true } }); },
      run: async (worker, profile, prompt, outer) => {
        const signal = AbortSignal.any([outer, lifetime.signal, AbortSignal.timeout(controller.state.timeoutMin * 60000)]);
        if (worker.assignment.executor !== "opencode") {
          const base = controller.state.mission?.base ?? await head(directory);
          const before = await snapshot(directory, base);
          worker.status = "working"; await controller.save();
          const { result, patch } = await externalAgent(worker.assignment, profile, prompt, directory, signal, worker, () => controller.save());
          if (patch) {
            if (readOnly(profile)) throw new Error("Read-only agent returned a patch");
            const paths = patchPaths(patch);
            if (paths.some(critical)) throw new Error("Agent patch touches protected files; apply those changes manually with explicit consent");
            for (const path of paths) {
              const parts = path.split("/");
              for (let i = 1; i <= parts.length; i++) {
                try { if ((await lstat(join(directory, ...parts.slice(0, i)))).isSymbolicLink()) throw new Error("Agent patch traverses a symlink"); }
                catch (error: any) { if (error.code !== "ENOENT") throw error; }
              }
            }
            const removed = paths.filter(path => patch.includes(`--- a/${path}\n+++ /dev/null`));
            if (removed.length > 2) throw new Error("Bulk removal requires explicit manual approval");
            worker.attention = "permission"; worker.updatedAt = new Date().toISOString();
            worker.detail = "Needs your approval · apply delegated agent patch"; await controller.save();
            try {
              await ctx.permission.assert({ sessionID: controller.state.owner as SessionID, action: "edit", resources: paths,
                metadata: { description: "Apply delegated agent patch", patch } });
            } finally { worker.attention = undefined; }
            signal.throwIfAborted();
            if (await snapshot(directory, base) !== before) throw new Error("Workspace changed while the agent prepared its patch; rerun the objective");
            await processRun("git", ["apply", "--check", "-"], directory, patch, signal);
            await processRun("git", ["apply", "-"], directory, patch, signal);
          }
          return result;
        }
        const readonly = readOnly(profile);
        const permissions = readonly
          ? [{ action: "*", resource: "*", effect: "deny" as const }, ...["read", "glob", "grep", ...READ_TOOLS].map(action => ({ action, resource: "*", effect: "allow" as const }))]
          : ["edit", "shell", "execute"].map(action => ({ action, resource: "*", effect: "ask" as const }));
        const session = await ctx.session.create({ parentID: (worker.parentID ?? controller.state.owner) as SessionID, title: `Mission ${roleLabel(worker.role)}: ${worker.objectiveID ?? "consultation"}`,
          model: modelRef(worker.assignment.model), permissions });
        worker.sessionID = session.id;
        const scope: Managed = { worker, profile, signal };
        managed.set(session.id, scope);
        const abort = () => { void ctx.session.interrupt({ sessionID: session.id }).catch(() => {}); };
        signal.addEventListener("abort", abort, { once: true });
        try {
          signal.throwIfAborted();
          await controller.save();
          await ctx.session.prompt({ sessionID: session.id, text: `Follow all repository instructions and preserve existing changes. Do not commit, push, publish, send messages, change secrets, lockfiles, infrastructure, CI or applied migrations without explicit consent. Use Australian English. ${!controller.state.allowChecksReview ? "Do not run test suites, write tests or perform code reviews in this repository." : ""}\n${readonly ? "You have read-only tools. Do not edit or execute commands." : "Only make changes authorised by the objective. Honour tool permission requests."}\n${prompt}\nFinish by calling mission_result with an honest status and a useful summary. If expert help is needed, call mission_ask_expert and then mission_result with status blocked. If you cannot call the result tool, end with MISSION_RESULT: {"status":"done|blocked|approve|changes","summary":"your result"}, choosing exactly one status.` }, { signal });
          await ctx.session.wait({ sessionID: session.id }, { signal });
          signal.throwIfAborted();
          let info = await ctx.session.get({ sessionID: session.id });
          if (info.outcome !== "succeeded") throw new Error(`Agent execution ${info.outcome ?? "has no terminal outcome"}`);
          const readResult = async () => {
            const messages = await ctx.session.context({ sessionID: session.id });
            const last = messages.filter(m => m.type === "assistant").at(-1);
            const text = last?.content.filter(c => c.type === "text").map(c => c.text).join("\n") ?? "";
            worker.transcript = text;
            for (const match of text.matchAll(/^MISSION_RESULT:\s*(\{.*\})\s*$/gm)) {
              const result = JSON.parse(match[1]) as Result;
              if (!["done", "blocked", "approve", "changes"].includes(result.status) || !result.summary?.trim()) throw new Error("Invalid explicit worker result");
              if (scope.result && scope.result.status !== result.status) scope.conflict = true;
              scope.result = result;
            }
          };
          await readResult();
          if (!scope.result) {
            scope.finalising = true;
            worker.detail = "Recording explicit completion result"; await controller.save();
            await ctx.session.prompt({ sessionID: session.id, text: "Record the result of the work already performed. Do not inspect, edit or run anything further. Call mission_result now, or output exactly MISSION_RESULT: followed by a JSON object with status (done, blocked, approve or changes) and summary. Only reviewers may approve. If you did not complete the objective, use blocked." }, { signal });
            await ctx.session.wait({ sessionID: session.id }, { signal });
            info = await ctx.session.get({ sessionID: session.id });
            if (info.outcome !== "succeeded") throw new Error(`Result recording ${info.outcome}`);
            await readResult();
          }
          if (scope.conflict) throw new Error("Worker returned conflicting results");
          if (!scope.result) throw new Error("Worker returned no explicit completion result; transcript retained in worker details");
          return scope.result;
        } finally {
          if (signal.aborted) await ctx.session.interrupt({ sessionID: session.id }).catch(() => {});
          signal.removeEventListener("abort", abort); managed.delete(session.id); attention.delete(session.id);
        }
      },
    });
    await controller.restore(await ctx.storage.get(stateKey) as unknown as State | undefined);
    const activity = (worker: Worker, detail: string) => {
      if (lifetime.signal.aborted || !working(worker)) return;
      worker.detail = worker.attention === "permission" ? "Needs your approval in the worker session"
        : worker.attention === "question" ? "Waiting for your answer in the worker session" : detail;
      worker.updatedAt = new Date().toISOString();
      if (!progressTimer) progressTimer = setTimeout(() => {
        progressTimer = undefined;
        void controller.save().catch(error => console.error("Could not save worker progress", error));
      }, 1000);
    };

    await ctx.tool.transform(tools => {
      for (const name of ["read", "glob", "grep"]) {
        const original = tools.list().find(t => t.name === name);
        if (original) tools.add({ ...original, name: `mission_${name}`, options: { codemode: false } });
      }
      tools.add({ name: "mission_result", description: "Complete the current mission objective with an explicit result. Call once at the end.", options: { codemode: false },
        input: { type: "object", properties: { status: { type: "string", enum: ["done", "blocked", "approve", "changes"] }, summary: { type: "string", minLength: 1 } }, required: ["status", "summary"], additionalProperties: false },
        execute: async (input, context) => {
          const scope = managed.get(context.sessionID);
          if (!scope) throw new Error("This tool is only available to a mission worker");
          if (scope.result) throw new Error("Only one result is permitted");
          scope.result = input as Result;
          return { content: "Result recorded. Finish this turn without further tools." };
        } });
      tools.add({ name: "mission_diff", description: "Read the current workspace status and changes, including staged and new files.", options: { codemode: false },
        input: { type: "object", properties: {}, additionalProperties: false },
        execute: async () => ({ content: await changes(directory, controller.state?.mission?.base ?? await head(directory)) }) });
      tools.add({ name: "mission_ask_expert", description: "Queue a focused expert consultation. Finish the current turn so the worker model can safely yield before the expert is loaded. Mission workers must then report blocked.", options: { codemode: false },
        input: { type: "object", properties: { question: { type: "string", minLength: 1 } }, required: ["question"], additionalProperties: false },
        execute: async (input, context) => {
          const scope = managed.get(context.sessionID);
          if (scope?.worker.role !== "worker" && context.sessionID !== controller.state?.owner) throw new Error("Only the active worker can request expert help");
          await controller.requestExpert((input as { question: string }).question);
          return { content: "Expert queued. Finish this turn now; do not continue editing." };
        } });
    });
    await questionOffers(ctx, controller, directory, sessionID => managed.has(sessionID), sessionID => adviceParents.add(sessionID), lifetime.signal);

    await ctx.session.hook("context", async event => {
      const scope = managed.get(event.sessionID);
      if (!scope && controller.state?.mission) {
        const session = await ctx.session.get({ sessionID: event.sessionID });
        if (!session.parentID && await realpath(session.location.directory) === directory) {
          const report = missionReport(controller.state);
          if (report) event.system.push({ type: "text", text: report });
        }
      }
      if (!scope && controller.canOffer() && !(await ctx.session.get({ sessionID: event.sessionID })).parentID) {
        if (event.tools[QUESTION_TOOL]) event.system.push({ type: "text", text: QUESTION_INSTRUCTIONS });
      } else delete event.tools[QUESTION_TOOL];
      if (!scope && event.sessionID !== controller.state?.owner) {
        for (const name of Object.keys(event.tools)) if (name.startsWith("mission_")) delete event.tools[name];
        return;
      }
      if (scope && readOnly(scope.profile)) for (const name of Object.keys(event.tools)) if (!READ_TOOLS.has(name)) delete event.tools[name];
      if (scope && !readOnly(scope.profile)) for (const name of Object.keys(event.tools)) if (!WORK_TOOLS.has(name)) delete event.tools[name];
      if (!scope) {
        delete event.tools.mission_result;
        if (!controller.state?.wingman.enabled) delete event.tools.mission_ask_expert;
        if (controller.state?.mission?.status === "running") throw new Error("Pause the mission before starting main-session work");
      }
      if (scope?.result) for (const name of Object.keys(event.tools)) delete event.tools[name];
      if (scope?.finalising) for (const name of Object.keys(event.tools)) if (name !== "mission_result") delete event.tools[name];
      if (controller.state?.wingman.pending && (scope?.worker.role === "worker" || !scope)) {
        for (const name of Object.keys(event.tools)) if (!scope || name !== "mission_result") delete event.tools[name];
      }
      if (!scope && controller.state?.localOnly) delete event.tools.subagent;
    });
    await ctx.session.hook("compaction", async event => {
      if (!controller.state?.mission || managed.has(event.sessionID)) return;
      const session = await ctx.session.get({ sessionID: event.sessionID });
      if (!session.parentID && await realpath(session.location.directory) === directory) {
        const report = missionReport(controller.state);
        if (report) event.system.push({ type: "text", text: report });
      }
    });
    await ctx.session.hook("prompt", async event => {
      if (!event.metadata?.hangarMissionAdvice) await controller.userInput(event.sessionID);
    });
    await ctx.tool.hook("execute.before", event => {
      const scope = managed.get(event.sessionID);
      if (scope?.signal.aborted) throw new Error("Worker interrupted");
      if (scope?.result) throw new Error("Objective already reported a result");
      if (scope?.finalising && event.tool !== "mission_result") throw new Error("Only result recording is permitted now");
      if (scope && event.tool === "shell" && (event.input as { background?: boolean })?.background) throw new Error("Mission shell commands must finish in the foreground");
      if (scope && readOnly(scope.profile) && !READ_TOOLS.has(event.tool)) throw new Error("Read-only mission worker cannot execute this tool");
      if (scope && !readOnly(scope.profile) && !WORK_TOOLS.has(event.tool)) throw new Error("Mission workers use local file and shell tools; delegate through the mission controller");
      if (event.sessionID === controller.state?.owner && controller.state?.wingman.enabled && controller.state.localOnly && event.tool === "subagent") throw new Error("Use mission_ask_expert for scheduled local assistance");
      if (scope) activity(scope.worker, scope.finalising ? "Recording completion result" : toolActivity(event.tool, event.input));
    });
    await ctx.permission.hook("evaluate", event => {
      if (!managed.has(event.sessionID)) return;
      if (event.action === "edit" && event.resources.some(critical)) { event.effect = "ask"; event.message = "Protected repository files need explicit consent"; }
    });

    await ctx.session.hook("title", event => {
      if (managed.has(event.sessionID)) event.result = "Mission worker";
    });

    await ctx.session.hook("http.request", async event => {
      const scope = managed.get(event.sessionID);
      const wingman = event.sessionID === controller.state?.owner && controller.state?.wingman.enabled;
      if (!scope && !wingman && !adviceParents.has(event.sessionID)) return;
      const state = controller.state;
      const assignment = { executor: "opencode" as const, model: `${event.model.providerID}/${event.model.id}` };
      if (scope || wingman) validateAssignment(assignment, state);
      if (scope && event.kind === "primary" && assignment.model !== scope.worker.assignment.model) throw new Error("Worker model differs from its objective assignment");
      if (event.model.providerID !== "hangar") return;
      const signal = scope?.signal ?? lifetime.signal;
      const origin = new URL(event.request.url).origin;
      const headers = new Headers();
      for (const name of ["authorization", "x-api-key"]) { const value = event.request.headers.get(name); if (value) headers.set(name, value); }
      const loading = AbortSignal.any([signal, AbortSignal.timeout(10 * 60000)]);
      const roles = async () => {
        const response = await fetch(`${origin}/api/roles`, { headers, signal: loading });
        if (!response.ok) throw new Error(`Hangar role lookup failed (${response.status})`);
        return await response.json() as Array<{ name: string; targets: Array<{ machine: string; profile: string; state: string }>; saved_targets: Array<{ machine: string; profile: string }> }>;
      };
      let role = (await roles()).find(r => r.name === event.model.id);
      const own = role?.saved_targets?.[0];
      if (!own) throw new Error("Hangar role needs an explicit primary target; implicit or follow-running routing is not accepted");
      const ready = () => role?.targets.some(t => t.machine === (own.machine ?? "") && t.profile === own.profile && t.state === "ready");
      if (!ready()) {
        if (scope) { scope.worker.status = "loading"; activity(scope.worker, `Loading ${event.model.id} through Hangar`); await controller.save(); }
        const warm = await fetch(`${origin}/v1/hangar/warm`, { method: "POST", headers: new Headers([...headers, ["content-type", "application/json"]]), body: JSON.stringify({ model: event.model.id }), signal: loading });
        if (!warm.ok) throw new Error(`Hangar could not prepare the requested model (${warm.status})`);
        const warmed = await warm.json() as { state?: string; reason?: string };
        if (warmed.state === "refused") throw new Error(warmed.reason ?? "Hangar refused model loading");
        while (!ready()) {
          await new Promise<void>((resolve, reject) => {
            const abort = () => { clearTimeout(timer); reject(new Error("Model loading interrupted or timed out")); };
            const timer = setTimeout(() => { loading.removeEventListener("abort", abort); resolve(); }, 1500);
            loading.addEventListener("abort", abort, { once: true }); if (loading.aborted) abort();
          });
          role = (await roles()).find(r => r.name === event.model.id);
          if (!role?.saved_targets?.some(t => t.machine === own.machine && t.profile === own.profile)) throw new Error("Hangar role changed while loading");
        }
      }
      expectedModels.set(event.sessionID, own);
      if (scope) { scope.expected = own; scope.worker.status = "working"; activity(scope.worker, "Model ready · waiting for response"); await controller.save(); }
    });
    await ctx.session.hook("http.response", async event => {
      const scope = managed.get(event.sessionID);
      const wingman = event.sessionID === controller.state?.owner && controller.state?.wingman.enabled;
      if ((!scope && !wingman && !adviceParents.has(event.sessionID)) || event.model.providerID !== "hangar") return;
      if (!event.response.ok) return;
      const served = event.response.headers.get("x-hangar-served-by"), expected = expectedModels.get(event.sessionID);
      if (!expected || event.response.headers.has("x-hangar-fallback") || !served ||
          (expected.machine ? served !== `${expected.machine}/${expected.profile}` : !served.endsWith(`/${expected.profile}`))) {
        await event.response.body?.cancel();
        throw new Error("Hangar could not verify the requested model; fallback response rejected");
      }
    });

    await ctx.tool.hook("execute.after", async event => {
      const scope = managed.get(event.sessionID);
      if (scope) {
        activity(scope.worker, event.status === "completed" ? event.tool === "mission_result" ? "Completion result recorded" : `${event.tool.replace(/^mission_/, "")} completed · continuing`
          : `${event.tool.replace(/^mission_/, "")} failed · handling the error`);
      }
      if (event.sessionID !== controller.state?.owner || !controller.state.wingman.enabled) return;
      const input = event.input as Record<string, unknown>;
      if (!["bash", "shell", "execute"].includes(event.tool)) return;
      const metadata = event.status === "completed" ? event.result.metadata : undefined;
      const exit = metadata?.exit ?? metadata?.exitCode ?? metadata?.exit_code;
      if (event.status === "error" || typeof exit === "number" && exit !== 0) {
        const command = String(input?.command ?? input?.code ?? JSON.stringify(input));
        const key = createHash("sha256").update(command).digest("hex");
        await controller.failure(key, `${command}\n${JSON.stringify(event.status === "error" ? event.error : event.result)}`);
      } else if (exit === 0) {
        const command = String(input?.command ?? input?.code ?? JSON.stringify(input));
        delete controller.state.wingman.failures[createHash("sha256").update(command).digest("hex")];
        await controller.save();
      }
    });

    const registration = await ctx.rpc.register(missionRPC, { action: async (raw, call) => {
      try {
        const input = raw as { sessionID: string; action: string; payload: string };
        const session = await ctx.session.get({ sessionID: input.sessionID as SessionID });
        if (await realpath(session.location.directory) !== await realpath(directory)) throw new Error("Session belongs to another workspace");
        if (managed.has(input.sessionID)) throw new Error("Control missions from the parent session");
        const payload = JSON.parse(input.payload);
        if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("Invalid action payload");
        const state = await controller.action(input.sessionID, input.action, payload);
        if ((input.action === "wingman" && payload.enabled) || input.action === "assign" && payload.role === "worker" && state.wingman.enabled) {
          const assignment = state.assignments.worker;
          if (assignment.executor !== "opencode") { state.wingman.enabled = false; throw new Error("Wingman's main worker must use opencode; external agents can be experts or objective executors"); }
          await ctx.session.switchModel({ sessionID: input.sessionID as SessionID, model: modelRef(assignment.model)! });
          if (!mainWorker || mainWorker.status === "done") {
            mainWorker = { id: id(), sessionID: input.sessionID, role: "worker", assignment: { ...assignment }, status: "waiting", detail: "Interactive worker ready" };
            state.workers.push(mainWorker);
          }
          await controller.save();
        }
        return { json: JSON.stringify(state) };
      } catch (error) {
        return call.error("mission.action", error instanceof Error ? error.message : String(error), {});
      }
    } });
    const eventLoop = (async () => {
      for await (const event of ctx.event.subscribe({ signal: lifetime.signal })) {
        const eventSession = event.type === "form.created" ? event.data.form.sessionID : "sessionID" in event.data ? event.data.sessionID : undefined;
        const scope = typeof eventSession === "string" ? managed.get(eventSession) : undefined;
        if (scope) {
          const worker = scope.worker;
          if (event.type === "session.text.started") worker.preview = "";
          if (event.type === "session.text.delta") {
            if (!scope.finalising) worker.preview = ((worker.preview ?? "") + event.data.delta).slice(-12000);
            activity(worker, scope.finalising ? "Recording completion result" : "Writing response");
          }
          if (event.type === "session.reasoning.delta") activity(worker, scope.finalising ? "Recording completion result" : "Thinking through the objective");
          if (event.type === "permission.asked" || event.type === "form.created") {
            const pending = attention.get(eventSession!) ?? new Map<string, "permission" | "question">();
            pending.set(event.type === "permission.asked" ? event.data.id : event.data.form.id,
              event.type === "permission.asked" ? "permission" : "question");
            attention.set(eventSession!, pending);
            worker.attention = [...pending.values()].includes("permission") ? "permission" : "question";
            activity(worker, "Needs your input");
          }
          if (event.type === "permission.replied" || event.type === "form.replied" || event.type === "form.cancelled") {
            const pending = attention.get(eventSession!);
            pending?.delete(event.type === "permission.replied" ? event.data.requestID : event.data.id);
            worker.attention = pending?.size ? [...pending.values()].includes("permission") ? "permission" : "question" : undefined;
            activity(worker, "Your response was received · continuing");
          }
          if (event.type === "session.retry.scheduled") activity(worker, `Provider retry ${event.data.attempt} scheduled`);
          if (event.type === "session.execution.succeeded") activity(worker, "Finalising the objective result");
        }
        if ("sessionID" in event.data && ["session.execution.succeeded", "session.execution.interrupted", "session.execution.failed"].includes(event.type)) adviceParents.delete(event.data.sessionID);
        if (!("sessionID" in event.data) || event.data.sessionID !== controller.state?.owner) continue;
        if (mainWorker && controller.state.wingman.enabled) {
          if (event.type === "session.execution.started") { mainWorker.status = "working"; mainWorker.detail = "Interactive work"; }
          if (event.type === "session.execution.succeeded") { mainWorker.status = "waiting"; mainWorker.detail = "Ready for your next request"; }
          if (event.type === "session.execution.interrupted" || event.type === "session.execution.failed") { mainWorker.status = "interrupted"; mainWorker.detail = "Interactive turn stopped"; }
          await controller.save();
        }
        if (event.type === "session.execution.succeeded") await controller.onIdle();
      }
    })().catch(error => { if (!lifetime.signal.aborted) console.error("Mission event stream stopped", error); });
    return async () => { lifetime.abort(); if (progressTimer) clearTimeout(progressTimer); await controller.close(); await registration.dispose(); await eventLoop; };
  },
});
