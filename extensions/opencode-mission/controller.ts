import { createHash } from "node:crypto";
import { realpath } from "node:fs/promises";
import { canonicalModel, clip, id, initial, note, objective, readOnly, template, validateAssignment, validateObjectives,
  type Assignment, type Evidence, type Objective, type Profile, type Role, type State, type Worker } from "./model.ts";
import { criteriaList, criterionEvidence } from "./evidence.ts";
import { missionTitle } from "./titles.ts";
import { changes, head, initialiseRepository, snapshot, workspacePolicy } from "./workspace.ts";

export interface Result { status: "done" | "blocked" | "approve" | "changes"; summary: string; evidence?: Evidence[] }
export interface Runtime {
  save(state: State): Promise<void>;
  rename?(sessionID: string, title: string, previous?: string): Promise<boolean>;
  run(worker: Worker, profile: Profile, prompt: string, signal: AbortSignal): Promise<Result>;
  interrupt(sessionID: string): Promise<void>;
  idle(sessionID: string): Promise<boolean>;
  deliver(sessionID: string, text: string): Promise<void>;
}

// Share scheduling locks across hot reloads and equivalent workspace locations in one server.
const registry = globalThis as typeof globalThis & { __hangarMissionLeases?: Set<string> };
const leases = registry.__hangarMissionLeases ??= new Set<string>();
function lease(directory: string) {
  if (leases.has(directory)) throw new Error("Another mission controller is working in this workspace");
  leases.add(directory);
  return () => leases.delete(directory);
}

export class Controller {
  state!: State;
  private active?: AbortController;
  private loop?: Promise<void>;
  private consulting = false;
  private consultationOwner?: string;
  private consultDone?: Promise<void>;
  private finishConsult?: () => void;
  private closed = false;
  readonly directory: string;
  readonly runtime: Runtime;
  constructor(directory: string, runtime: Runtime) { this.directory = directory; this.runtime = runtime; }

  async restore(saved?: State) {
    if (!saved) return;
    if (saved.version !== 1 || await realpath(saved.directory) !== await realpath(this.directory)) throw new Error("Unsupported mission journal");
    saved.directory = this.directory;
    for (const item of saved.mission?.objectives ?? []) if (item.profile === "review" && !["done", "skipped"].includes(item.status) && item.attempts > 0 && item.result && item.snapshot && !item.findingsFingerprint) {
      item.findingsFingerprint = item.snapshot; item.snapshot = undefined;
    }
    saved.failureThreshold ??= 2;
    saved.archives ??= [];
    saved.advice ??= { enabled: true, declined: {} };
    for (const assignment of Object.values(saved.assignments))
      if (assignment.executor === "opencode") assignment.model = canonicalModel(assignment.model);
    for (const item of saved.mission?.objectives ?? [])
      if (!["done", "skipped"].includes(item.status) && item.assignment.executor === "opencode")
        item.assignment.model = canonicalModel(item.assignment.model);
    if (saved.mission) {
      const history = saved.flow.slice(saved.flow.findLastIndex(e => e.text.startsWith("Mission created:")) + 1);
      saved.mission.startedAt ??= history.find(e => e.text === "Mission started")?.at;
      if (["done", "cancelled"].includes(saved.mission.status)) saved.mission.finishedAt ??=
        history.findLast(e => e.text === "Mission completed" || e.text === "Mission cancelled")?.at;
    }
    if (leases.has(this.directory)) throw new Error("A mission is active in another workspace location; connect to its session");
    this.state = saved;
    const recovered = saved.mission?.status === "running" || saved.workers.some(w => ["working", "waiting", "loading"].includes(w.status));
    for (const worker of saved.workers.filter(w => ["working", "waiting", "loading"].includes(w.status))) {
      if (worker.sessionID) await this.runtime.interrupt(worker.sessionID);
      worker.status = "interrupted";
      worker.detail = "Recovered after server/plugin restart; previous changes retained";
      worker.finishedAt = worker.updatedAt = new Date().toISOString();
    }
    if (saved.mission?.status === "running") {
      saved.mission.status = "paused"; saved.mission.pauseCause = "recovery";
      saved.mission.reason = "OpenCode restarted during work. Changes and completed objectives were retained; resume when ready.";
    }
    for (const item of saved.mission?.objectives ?? []) if (item.status === "running") item.status = "pending";
    await this.nameOwner();
    if (recovered) note(saved, "Recovered interrupted work; resume explicitly after checking the retained changes");
    await this.save();
  }
  private async nameOwner() {
    const s = this.state;
    if (!s?.mission || !this.runtime.rename) return;
    const title = missionTitle(s.mission);
    try {
      if (await this.runtime.rename(s.owner, title, s.sessionTitles?.[s.owner])) {
        s.sessionTitles ??= {}; s.sessionTitles[s.owner] = title;
      }
    } catch { note(s, "Session title could not be updated; mission execution is unaffected"); }
  }
  private archiveCurrent() {
    const s = this.state, mission = s.mission;
    if (!mission) return;
    if (!["done", "cancelled"].includes(mission.status)) throw new Error("Finish or cancel the mission before clearing it");
    const ids = new Set(mission.objectives.map(item => item.id));
    const start = s.flow.findLastIndex(event => event.text.startsWith("Mission created:"));
    const record = structuredClone({ mission, workers: s.workers.filter(worker => worker.objectiveID && ids.has(worker.objectiveID)),
      flow: s.flow.slice(Math.max(0, start)), archivedAt: new Date().toISOString() });
    s.archives ??= [];
    const previous = s.archives.findIndex(item => item.mission.id === mission.id);
    if (previous < 0) s.archives.push(record); else s.archives[previous] = record;
    s.workers = s.workers.filter(worker => !worker.objectiveID || !ids.has(worker.objectiveID));
    s.mission = undefined;
  }
  async ensure(owner: string) {
    if (this.closed) throw new Error("Mission plugin is updating; wait for the interface to reconnect");
    if (!this.state) this.state = initial(owner, this.directory, (await workspacePolicy(this.directory)).paperCut);
    if (this.state.owner !== owner) {
      if (this.loop || this.consulting || this.state.wingman.enabled ||
          this.state.mission && !["done", "cancelled"].includes(this.state.mission.status))
        throw new Error("This mission is controlled from another chat. Use Open mission's controlling session in the sidebar to manage it.");
      const release = lease(this.directory);
      try {
        if (!await this.runtime.idle(this.state.owner) || !await this.runtime.idle(owner))
          throw new Error("Wait for both chats to finish before moving mission control to this chat.");
        this.state.owner = owner;
        note(this.state, "Mission control moved to the current chat; earlier results retained");
        await this.save();
      } finally { release(); }
    }
    return this.state;
  }
  async save() { if (this.state) await this.runtime.save(structuredClone(this.state)); }
  private changed(text: string, workerID?: string) { note(this.state, text, workerID); return this.save(); }
  private editable() {
    if (this.loop || this.consulting) throw new Error("Pause and wait for active workers before editing assignments or objectives");
  }
  async action(owner: string, action: string, payload: Record<string, unknown> = {}): Promise<State> {
    if (action === "status" && this.state) return this.state;
    const s = await this.ensure(owner);
    if (action === "status") return s;
    if (action === "pause" && payload.boundary === true) {
      if (s.mission?.status !== "running") throw new Error("A mission must be running to pause after its current objective");
      s.mission.pauseRequested = true;
      await this.changed("Pause requested: finish the current objective, then wait before starting another worker");
      return s;
    }
    if (action === "pause" || action === "cancel") {
      if (s.mission) {
        s.mission.status = action === "pause" ? "paused" : "cancelled";
        s.mission.pauseRequested = false; s.mission.pauseCause = "user";
        s.mission.reason = action === "pause" ? "Paused by you. Existing changes are retained; resume when ready." : "Cancelled by you. Existing changes are retained.";
      }
      if (s.mission && action === "cancel") s.mission.finishedAt = new Date().toISOString();
      s.wingman.pending = undefined;
      this.active?.abort(new Error(action === "pause" ? "Paused by owner" : "Cancelled by owner"));
      await this.loop;
      await this.changed(action === "pause" ? "Mission paused" : "Mission cancelled");
      return s;
    }
    if (action === "steer") {
      if (!s.mission || ["done", "cancelled"].includes(s.mission.status)) throw new Error("Steering needs an unfinished mission. Add a follow-up objective to a completed mission instead.");
      const text = String(payload.text ?? "").trim();
      if (!text || text.length > 8000) throw new Error("Give between 1 and 8000 characters of instructions");
      if (!await this.runtime.idle(owner)) throw new Error("Wait for the controlling chat to finish before steering");
      await this.action(owner, "pause");
      s.mission.instructions ??= [];
      s.mission.instructions.push({ at: new Date().toISOString(), text });
      const next = s.mission.objectives.find(item => !["done", "skipped"].includes(item.status));
      if (next) { next.attempts = 0; next.status = "pending"; next.findingsFingerprint = undefined; }
      s.mission.pauseCause = "steering"; s.mission.reason = "New instructions saved for the next worker. Resume to continue with the retained changes.";
      await this.changed(`Instructions received: ${clip(text, 500)}`);
      return s;
    }
    if (action === "wingman") {
      if (typeof payload.enabled !== "boolean") throw new Error("Specify enabled");
      if (this.loop) throw new Error("Pause the mission before changing Wingman mode");
      if (payload.enabled && s.assignments.worker.executor !== "opencode") throw new Error("Wingman's interactive worker must use OpenCode. Choose an OpenCode worker default first; external agents can be thinkers or Mission objective workers.");
      s.wingman.enabled = payload.enabled;
      if (payload.enabled) { s.wingman.consultations = 0; s.wingman.failures = {}; }
      if (!payload.enabled) { s.wingman.pending = undefined; if (this.consulting) this.active?.abort(); }
      await this.changed(`Wingman ${payload.enabled ? "enabled" : "disabled"}`);
      return s;
    }
    if (action === "consult") {
      if (!s.wingman.enabled || this.loop) throw new Error("Consult now requires Wingman on and no running mission");
      await this.requestExpert(String(payload.question ?? "Inspect the current task and advise on the next step"));
      await this.onIdle();
      return s;
    }
    if (action === "verify") {
      this.editable();
      const mission = payload.id ? s.archives?.find(record => record.mission.id === payload.id)?.mission : s.mission ?? s.archives?.at(-1)?.mission;
      if (!mission || !["done", "cancelled"].includes(mission.status)) throw new Error("Snapshot comparison needs a finished or archived mission");
      const expected = mission.completedSnapshot ?? mission.objectives.findLast(item => item.status === "done")?.snapshot;
      if (!expected) throw new Error("No completion snapshot was recorded for this mission");
      s.snapshotChecks ??= {};
      s.snapshotChecks[mission.id] = { at: new Date().toISOString(), matches: expected === await snapshot(this.directory, mission.base) };
      await this.save(); return s;
    }
    if (action === "clear" && s.mission && !["done", "cancelled"].includes(s.mission.status))
      throw new Error("Finish or cancel this mission before clearing it. Clear preserves results in history; it does not cancel work.");
    this.editable();
    if (!await this.runtime.idle(owner)) throw new Error("Wait for the main conversation to finish before changing mission settings");
    if (action === "clear") {
      this.archiveCurrent();
      await this.changed("Mission archived and cleared from the display; results retained in history");
    } else if (action === "create") {
      if (s.mission && !["done", "cancelled"].includes(s.mission.status)) throw new Error("Finish or cancel the existing mission first");
      const task = String(payload.task ?? "").trim();
      if (!task) throw new Error("Describe the mission");
      await initialiseRepository(this.directory);
      const created = template(s, task, await head(this.directory));
      this.archiveCurrent(); s.mission = created;
      await this.nameOwner();
      s.wingman.consultations = 0; s.wingman.failures = {}; s.wingman.pending = undefined;
      s.wingman.enabled = false;
      await this.changed(`Mission created: ${task}`);
    } else if (action === "cloud") {
      if (typeof payload.enabled !== "boolean") throw new Error("Specify enabled");
      if (!payload.enabled) {
        const copy = { ...s, localOnly: true };
        Object.values(s.assignments).forEach(a => validateAssignment(a, copy));
        validateObjectives(copy);
      }
      s.localOnly = !payload.enabled;
      await this.changed(`Cloud delegation ${payload.enabled ? "explicitly enabled" : "disabled"}`);
    } else if (action === "offers") {
      s.advice.enabled = payload.enabled === true;
      await this.changed(`Technical question offers ${s.advice.enabled ? "enabled" : "disabled"}`);
    } else if (action === "helper") {
      s.helper = payload.enabled === true;
      await this.changed(`Helper ${s.helper ? "enabled" : "disabled"}`);
    } else if (action === "require-evidence") {
      s.requireEvidence = payload.enabled === true;
      await this.changed(`Criterion evidence requirement ${s.requireEvidence ? "enabled" : "disabled"}`);
    } else if (action === "checks") {
      s.allowChecksReview = payload.enabled === true;
      await this.changed(`Checks and reviews ${s.allowChecksReview ? "explicitly enabled" : "disabled"}`);
    } else if (action === "limits") {
      for (const key of ["maxAttempts", "maxConsultations", "timeoutMin", "failureThreshold"] as const) {
        if (payload[key] === undefined) continue;
        const value = Number(payload[key]);
        if (!Number.isInteger(value) || value < 1 || value > (key === "timeoutMin" ? 180 : 20)) throw new Error(`Invalid ${key}`);
      }
      for (const key of ["maxAttempts", "maxConsultations", "timeoutMin", "failureThreshold"] as const)
        if (payload[key] !== undefined) s[key] = Number(payload[key]);
      await this.changed("Execution limits changed by owner");
    } else if (action === "assign") {
      const role = payload.role as Role;
      if (!["helper", "worker", "expert"].includes(role)) throw new Error("Unknown role");
      const assignment = payload.assignment as Assignment;
      validateAssignment(assignment, s);
      if (role === "worker" && s.wingman.enabled && assignment.executor !== "opencode") throw new Error("Wingman's interactive worker must use OpenCode. Turn Wingman off before assigning an external Mission worker default.");
      s.assignments[role] = { ...assignment };
      await this.changed(`Default ${role} assignment changed`);
    } else if (action === "apply-defaults") {
      if (!s.mission) throw new Error("Create a mission first");
      for (const item of s.mission.objectives) if (!["done", "skipped"].includes(item.status))
        item.assignment = { ...s.assignments[item.profile === "research" || item.profile === "review" ? "expert" : "worker"] };
      await this.changed("Default models explicitly applied to unfinished objectives; completed assignments retained");
    } else if (["add", "edit", "skip", "move"].includes(action)) {
      const mission = s.mission;
      if (!mission) throw new Error("Create a mission first");
      const proposed = structuredClone(mission);
      const index = proposed.objectives.findIndex(o => o.id === payload.id);
      if (action === "add") {
        const profile = (payload.profile ?? "implement") as Profile;
        if (!["research", "implement", "check", "review"].includes(profile)) throw new Error("Unknown permission profile");
        const item = objective(String(payload.title ?? ""), profile,
          (payload.assignment as Assignment) ?? s.assignments[profile === "review" || profile === "research" ? "expert" : "worker"], String(payload.criteria ?? payload.title ?? ""));
        const previous = proposed.objectives.at(-1);
        if (previous) item.dependencies = [previous.id];
        proposed.objectives.push(item);
      } else {
        if (index < 0) throw new Error("Unknown objective");
        const item = proposed.objectives[index];
        if (["done", "skipped"].includes(item.status)) throw new Error("Completed history is immutable; add a follow-up objective");
        if (action === "skip") item.status = "skipped";
        if (action === "edit") {
          if (payload.title !== undefined) item.title = String(payload.title);
          if (payload.criteria !== undefined) item.criteria = String(payload.criteria);
          if (payload.assignment) item.assignment = payload.assignment as Assignment;
          item.attempts = 0;
          item.status = "pending";
        }
        if (action === "move") {
          const to = Number(payload.index);
          if (!Number.isInteger(to) || to < 0 || to >= proposed.objectives.length) throw new Error("Invalid position");
          if (proposed.objectives.slice(to, index).some(o => ["done", "skipped"].includes(o.status))) throw new Error("Pending work cannot move ahead of completed history");
          proposed.objectives.splice(index, 1);
          proposed.objectives.splice(to, 0, item);
          proposed.objectives.forEach((o, i) => { o.dependencies = i ? [proposed.objectives[i - 1].id] : []; });
        }
      }
      proposed.status = "paused";
      validateObjectives({ ...s, mission: proposed });
      s.mission = proposed;
      await this.changed(`Objectives updated (${action})`);
    } else if (action === "resume" || action === "start") {
      if (!s.mission || s.mission.status === "cancelled") throw new Error("Create a mission first");
      if (s.wingman.enabled) throw new Error("Disable Wingman before starting a mission");
      if (!await this.runtime.idle(s.owner)) throw new Error("Wait for the main conversation to become idle");
      validateObjectives(s);
      await initialiseRepository(this.directory);
      for (const item of s.mission.objectives) if (item.status === "blocked") item.status = "pending";
      const release = lease(this.directory);
      s.mission.status = "running";
      const resumed = Boolean(s.mission.startedAt);
      s.mission.startedAt ??= new Date().toISOString();
      s.mission.finishedAt = undefined;
      s.mission.reason = undefined; s.mission.pauseCause = undefined; s.mission.pauseRequested = false;
      this.active = new AbortController();
      const signal = this.active.signal;
      this.loop = (async () => {
        await this.changed(resumed ? "Mission resumed" : "Mission started");
        await this.execute(signal);
      })().catch(async error => {
        if (s.mission?.status === "running") { s.mission.status = "paused"; s.mission.pauseCause ??= "error"; s.mission.reason = String(error.message ?? error); }
        for (const item of s.mission?.objectives ?? []) if (item.status === "running") item.status = "pending";
        await this.changed(`Execution stopped: ${error.message ?? error}`);
      }).finally(() => { release(); this.loop = undefined; this.active = undefined; });
      await this.save();
    } else throw new Error(`Unknown action: ${action}`);
    return s;
  }
  async worker(role: Role, assignment: Assignment, profile: Profile, prompt: string, signal: AbortSignal, objectiveID?: string,
    consultation?: { parentID: string; oneOffCloud: boolean }): Promise<Result> {
    validateAssignment(assignment, consultation?.oneOffCloud ? { ...this.state, localOnly: false } : this.state);
    const startedAt = new Date().toISOString();
    const worker: Worker = { id: id(), role, assignment: { ...assignment }, objectiveID, ...consultation,
      status: "waiting", detail: "Preparing session", startedAt, updatedAt: startedAt };
    this.state.workers.push(worker);
    await this.changed(`${role}: ${assignment.executor}/${assignment.model}`, worker.id);
    try {
      const result = await this.runtime.run(worker, profile, prompt, signal);
      worker.status = "done"; worker.result = result.summary; worker.detail = "Completed";
      worker.attention = undefined; worker.finishedAt = worker.updatedAt = new Date().toISOString();
      await this.changed(`${role} finished: ${clip(result.summary, 300)}`, worker.id);
      return result;
    } catch (error) {
      worker.status = signal.aborted ? "interrupted" : "failed";
      const cause = signal.aborted ? signal.reason ?? error : error;
      worker.detail = cause instanceof Error ? cause.message : String(cause);
      worker.attention = undefined; worker.finishedAt = worker.updatedAt = new Date().toISOString();
      await this.changed(`${role}: ${worker.detail}`, worker.id);
      throw error;
    }
  }
  canOffer() {
    return !this.closed && !this.loop && !this.consulting && (!this.state || this.state.advice.enabled &&
      (!this.state.mission || ["done", "cancelled"].includes(this.state.mission.status)));
  }
  async declineAdvice(owner: string, fingerprint: string) {
    if (!this.state) await this.ensure(owner);
    const previous = this.state.advice.declined[owner] ?? [];
    this.state.advice.declined[owner] = [...new Set([...previous, fingerprint])].slice(-100);
    await this.save();
  }
  async consultOnce(owner: string, assignment: Assignment, question: string, signal: AbortSignal) {
    if (!this.canOffer()) throw new Error("Question consultations are unavailable while a mission or consultation is active");
    if (!this.state) await this.ensure(owner);
    if (assignment.executor === "opencode" && assignment.model !== "hangar/thinker") throw new Error("Local question consultations use hangar/thinker");
    const release = lease(this.directory);
    this.consultDone = new Promise(resolve => { this.finishConsult = resolve; });
    this.consulting = true; this.consultationOwner = owner; this.active = new AbortController();
    try {
      await this.changed(`Question consultation accepted: ${assignment.executor}/${assignment.model}`);
      const base = await head(this.directory), before = await snapshot(this.directory, base);
      const result = await this.worker("expert", assignment, "research",
        `Answer this technical question with grounded advice. Do not make user preference decisions or grant approvals. Do not edit, run checks or perform a general code review.\n${question}\nWorkspace:\n${await changes(this.directory, base)}`,
        AbortSignal.any([signal, this.active.signal]), undefined, { parentID: owner, oneOffCloud: assignment.executor !== "opencode" });
      signal.throwIfAborted(); this.active.signal.throwIfAborted();
      if (before !== await snapshot(this.directory, base)) throw new Error("Workspace changed during consultation; advice is stale");
      if (result.status !== "done") throw new Error(`Consultant did not complete the question: ${result.summary}`);
      await this.changed("Question advice returned to the current worker");
      return result.summary;
    } finally {
      release(); this.consulting = false; this.consultationOwner = undefined; this.active = undefined;
      try { await this.save(); } finally { this.finishConsult?.(); this.consultDone = undefined; this.finishConsult = undefined; }
    }
  }
  private async execute(signal: AbortSignal) {
    const s = this.state, mission = s.mission!;
    while (mission.status === "running") {
      signal.throwIfAborted();
      if (mission.pauseRequested && mission.objectives.some(item => !["done", "skipped"].includes(item.status))) {
        mission.pauseRequested = false; mission.status = "paused"; mission.pauseCause = "user";
        mission.reason = "Current objective finished. Paused at an objective boundary; safe to update or steer, then resume.";
        await this.changed("Mission paused at an objective boundary"); return;
      }
      const item = mission.objectives.find(o => !["done", "skipped"].includes(o.status));
      if (!item) {
        const review = mission.objectives.findLast(objective => objective.profile === "review" && objective.status === "done");
        if (review?.snapshot && review.snapshot !== await snapshot(this.directory, mission.base) && s.allowChecksReview) {
          this.appendValidation("Workspace changed after review");
          await this.changed("Reviewed snapshot changed; fresh validation required"); continue;
        }
        mission.pauseRequested = false;
        mission.completedSnapshot = await snapshot(this.directory, mission.base);
        mission.status = "done"; mission.finishedAt = new Date().toISOString(); await this.changed("Mission completed"); return;
      }
      if (item.attempts >= s.maxAttempts) throw new Error(`Attempt budget reached for ${item.title}; edit the objective or add a follow-up`);
      if (!item.dependencies.every(dep => mission.objectives.some(o => o.id === dep && ["done", "skipped"].includes(o.status))))
        throw new Error("Objective dependencies are incomplete");
      const before = await snapshot(this.directory, mission.base);
      item.status = "running";
      await this.changed(`Objective started: ${item.title}`);
      const report = await changes(this.directory, mission.base);
      const completed = mission.objectives.filter(o => o.status === "done").map(o => `${o.title}: ${clip(o.result ?? "", 8000)}`);
      const support = s.workers.filter(w => w.role === "helper" && w.status === "done" && mission.objectives.some(o => o.id === w.objectiveID))
        .map(w => `Helper support (verify against current files): ${clip(w.result ?? "", 6000)}`);
      const context = [...completed, ...support].join("\n\n");
      const prompt = `Mission: ${mission.task}\nOwner's later instructions (apply to remaining work, preserving completed history):\n${(mission.instructions ?? []).map(instruction => `${instruction.at}: ${instruction.text}`).join("\n") || "none"}\nObjective: ${item.title}\nCompletion criteria: ${item.criteria}\nReport evidence for each criterion separately: ${JSON.stringify(criteriaList(item.criteria))}. Include an evidence array with criterion (exact text), status (met, unmet or unverified), and detail (actual files, commands/results or limitations). Never invent evidence.\nPermission profile: ${item.profile}\nPrevious results:\n${context}\nPrevious attempt:\n${item.result ?? "none"}\nWorkspace:\n${report}\n${item.profile === "review" ? "Return approve or changes, with concrete findings. Approval applies only to the supplied workspace snapshot." : item.profile === "check" ? "Return done only when the required checks pass; return changes for failing checks with evidence, or blocked for an unavailable tool or environment." : "Complete this objective and report done or blocked honestly."}`;
      const helper = s.helper && item.profile === "research"
        ? this.worker("helper", s.assignments.helper, "research", `Read-only support: locate relevant files for this task. Return a concise map with file paths. Do not implement or review.\n${mission.task}`, AbortSignal.any([signal, AbortSignal.timeout(120000)]), item.id).catch(async error => {
          await this.changed(`Optional Helper unavailable: ${error.message ?? error}`); return undefined;
        }) : Promise.resolve(undefined);
      let result: Result;
      try {
        result = await this.worker(readOnly(item.profile) ? "expert" : "worker", item.assignment, item.profile, prompt, signal, item.id);
      } finally { await helper; }
      signal.throwIfAborted();
      if (!["done", "blocked", "approve", "changes"].includes(result.status) || typeof result.summary !== "string" || !result.summary.trim()) throw new Error("Worker returned an invalid result");
      if (item.profile === "check" && result.status === "approve") throw new Error("Only a review objective can approve changes");
      if ((item.profile === "research" || item.profile === "implement") && !["done", "blocked"].includes(result.status)) throw new Error("Worker returned a result incompatible with its permission profile");
      const after = await snapshot(this.directory, mission.base);
      if (readOnly(item.profile) && before !== after) throw new Error("Read-only workspace changed; result invalidated");
      item.attempts++;
      item.result = result.summary;
      item.evidence = criterionEvidence(item.criteria, result.evidence); item.evidenceAt = new Date().toISOString();
      if (["done", "approve"].includes(result.status) && (item.evidence.some(entry => entry.status === "unmet") ||
          s.requireEvidence && item.evidence.some(entry => entry.status !== "met"))) {
        item.status = "blocked"; mission.pauseCause = "blocked";
        throw new Error("Completion criteria lack sufficient reported evidence. Inspect the objective's evidence before retrying.");
      }
      if (s.wingman.pending && result.status === "blocked") {
        const question = s.wingman.pending; s.wingman.pending = undefined;
        item.result += `\nExpert advice:\n${await this.expert(question, report, signal)}`;
        item.status = "pending";
        continue;
      }
      if (result.status === "blocked") { item.status = "blocked"; mission.pauseCause = "blocked"; throw new Error(`Objective blocked: ${result.summary}`); }
      if (item.profile === "check" && result.status === "changes") {
        if (item.attempts >= s.maxAttempts) throw new Error(`Attempt budget reached for ${item.title}; inspect the findings before retrying`);
        const fix = objective(`Fix failed checks for ${item.title}`, "implement", s.assignments.worker, result.summary);
        const index = mission.objectives.indexOf(item);
        fix.dependencies = [...item.dependencies]; item.dependencies = [fix.id];
        mission.objectives.splice(index, 0, fix); item.status = "pending";
        if (item.attempts >= 2) item.result += `\nExpert advice:\n${await this.expert(`Checks failed ${item.attempts} times. ${result.summary}`, report, signal)}`;
        fix.criteria = item.result;
        await this.changed("Checks failed; inserted a fix before retrying"); continue;
      }
      if (item.profile === "review" && result.status !== "approve") {
        if (result.status !== "changes") throw new Error("Reviewer did not give an explicit verdict");
        const fingerprint = createHash("sha256").update(result.summary.toLowerCase().replace(/\s+/g, " ")).digest("hex");
        if (item.findingsFingerprint === fingerprint) throw new Error("The same review findings returned after a fix");
        if (item.attempts >= s.maxAttempts) throw new Error(`Attempt budget reached for ${item.title}; inspect the findings before retrying`);
        item.findingsFingerprint = fingerprint;
        const fix = objective(`Fix findings from ${item.title}`, "implement", s.assignments.worker, result.summary);
        const index = mission.objectives.indexOf(item);
        fix.dependencies = [...item.dependencies]; item.dependencies = [fix.id];
        const previousCheck = mission.objectives.findLast(objective => objective.profile === "check");
        if (previousCheck) {
          const check = objective("Recheck after review fixes", "check", s.assignments.worker, previousCheck.criteria);
          check.dependencies = [fix.id]; item.dependencies = [check.id];
          mission.objectives.splice(index, 0, fix, check);
        } else mission.objectives.splice(index, 0, fix);
        item.status = "pending";
        await this.changed("Review requested changes; inserted fix and any required recheck"); continue;
      }
      item.status = "done"; item.snapshot = after;
      if (!readOnly(item.profile) && before !== after && s.allowChecksReview) {
        const earlierReview = mission.objectives.some(o => o.profile === "review" && o.status === "done");
        const pendingReview = mission.objectives.some(o => o.profile === "review" && o.status === "pending");
        if (earlierReview && !pendingReview) {
          this.appendValidation("Follow-up changes");
          await this.changed("Code changed after approval; added a fresh review");
        }
      }
      await this.changed(`Objective completed: ${item.title}`);
    }
  }
  private appendValidation(reason: string) {
    const s = this.state, mission = s.mission!;
    const check = mission.objectives.findLast(item => item.profile === "check");
    let dependency = mission.objectives.at(-1)!.id;
    if (check) {
      const fresh = objective(`${reason}: check`, "check", s.assignments.worker, check.criteria);
      fresh.dependencies = [dependency]; mission.objectives.push(fresh); dependency = fresh.id;
    }
    const review = objective(`${reason}: review`, "review", s.assignments.expert, "Approve the current snapshot or list concrete findings");
    review.dependencies = [dependency]; mission.objectives.push(review);
  }
  requestExpert(question: string) {
    if (!this.state || (!this.state.wingman.enabled && !this.loop)) throw new Error("Wingman is off");
    if (this.state.wingman.consultations >= this.state.maxConsultations) throw new Error("Consultation budget reached");
    this.state.wingman.pending = clip(question, 8000);
    return this.changed("Expert consultation queued for the next safe boundary");
  }
  private async expert(question: string, report: string, signal: AbortSignal) {
    const s = this.state;
    if (s.wingman.consultations >= s.maxConsultations) throw new Error("Consultation budget reached");
    s.wingman.consultations++;
    const before = await snapshot(this.directory, s.mission?.base ?? await head(this.directory));
    const result = await this.worker("expert", s.assignments.expert, "research",
      `Diagnose and advise only. Do not edit, run checks or perform a general code review.\nQuestion: ${question}\nEvidence:\n${report}`, signal);
    if (before !== await snapshot(this.directory, s.mission?.base ?? await head(this.directory))) throw new Error("Workspace changed during consultation; advice is stale");
    if (result.status === "blocked") throw new Error(`Expert blocked: ${result.summary}`);
    return result.summary;
  }
  async onIdle() {
    const s = this.state;
    if (!s?.wingman.enabled || !s.wingman.pending || this.loop || this.consulting || this.closed) return;
    if (!await this.runtime.idle(s.owner)) return;
    const release = lease(this.directory);
    this.consultDone = new Promise(resolve => { this.finishConsult = resolve; });
    this.consulting = true; this.consultationOwner = s.owner; this.active = new AbortController();
    const question = s.wingman.pending; s.wingman.pending = undefined;
    try {
      const report = await changes(this.directory, await head(this.directory));
      const advice = await this.expert(question, report, this.active.signal);
      this.active.signal.throwIfAborted();
      if (!await this.runtime.idle(s.owner)) throw new Error("New user work started; consultation retained in worker history without automatic continuation");
      await this.runtime.deliver(s.owner, `Wingman expert advice:\n${advice}\nContinue the user's current task using this advice and repository instructions.`);
    } catch (error) { await this.changed(`Consultation stopped: ${error instanceof Error ? error.message : error}`); }
    finally {
      release(); this.consulting = false; this.consultationOwner = undefined; this.active = undefined;
      try { await this.save(); } finally { this.finishConsult?.(); this.consultDone = undefined; this.finishConsult = undefined; }
    }
  }
  async failure(key: string, evidence: string) {
    const s = this.state;
    if (!s?.wingman.enabled || this.consulting) return;
    const count = (s.wingman.failures[key] ?? 0) + 1;
    s.wingman.failures[key] = count;
    if (count === s.failureThreshold) await this.requestExpert(`The same command failed ${count} times. Diagnose from evidence:\n${clip(evidence, 12000)}`);
    await this.save();
  }
  async userInput(owner = this.state?.owner) {
    if (owner === this.state?.owner && this.state?.mission?.status === "running") await this.action(this.state.owner, "pause");
    if (this.consulting && owner === this.consultationOwner) { this.active?.abort(new Error("New user input superseded expert consultation")); await this.consultDone; }
  }
  async close() {
    this.closed = true;
    if (this.state?.mission?.status === "running") {
      this.state.mission.status = "paused"; this.state.mission.pauseCause = "update";
      this.state.mission.pauseRequested = false;
      this.state.mission.reason = "OpenCode reloaded the Mission plugin during work. Completed results and file changes are retained; resume explicitly.";
      await this.changed("Plugin update paused the mission; retained changes require explicit resume");
    }
    if (this.state) this.state.wingman.pending = undefined;
    this.active?.abort(new Error("Paused for Mission plugin update; file changes retained"));
    await this.loop;
    await this.consultDone;
    for (const item of this.state?.mission?.objectives ?? []) if (item.status === "running") item.status = "pending";
    await this.save();
  }
}
