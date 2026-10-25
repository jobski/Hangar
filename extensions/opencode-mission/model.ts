import { randomUUID } from "node:crypto";

export type Executor = "opencode" | "codex" | "claude";
export type Role = "helper" | "worker" | "expert";
export type Profile = "research" | "implement" | "check" | "review";
export interface Assignment { executor: Executor; model: string }
export interface Objective {
  id: string; title: string; criteria: string; profile: Profile; assignment: Assignment;
  dependencies: string[]; status: "pending" | "running" | "done" | "blocked" | "skipped";
  attempts: number; result?: string; snapshot?: string;
}
export interface Worker {
  id: string; sessionID?: string; parentID?: string; oneOffCloud?: boolean; objectiveID?: string; role: Role; assignment: Assignment;
  status: "waiting" | "loading" | "working" | "done" | "failed" | "interrupted";
  detail: string; result?: string; transcript?: string; preview?: string;
  startedAt?: string; updatedAt?: string; finishedAt?: string; attention?: "permission" | "question";
}
export interface Flow { at: string; text: string; workerID?: string }
export interface Mission {
  id: string; task: string; base: string; status: "ready" | "running" | "paused" | "done" | "cancelled";
  objectives: Objective[]; reason?: string;
  startedAt?: string; finishedAt?: string;
}
export interface State {
  version: 1; owner: string; directory: string; localOnly: boolean; helper: boolean;
  paperCut: boolean; allowChecksReview: boolean;
  assignments: Record<Role, Assignment>; mission?: Mission; workers: Worker[]; flow: Flow[];
  wingman: { enabled: boolean; failures: Record<string, number>; pending?: string; consultations: number };
  advice: { enabled: boolean; declined: Record<string, string[]> };
  maxAttempts: number; maxConsultations: number; timeoutMin: number; failureThreshold: number;
}
export const id = () => randomUUID();
export const local = (model: string): Assignment => ({ executor: "opencode", model: `hangar/${model}` });
export const canonicalModel = (model: string) => model.replace(/^hangar\/(glm|qwen|main)(?=$|:)/,
  (_, role: string) => `hangar/${role === "glm" ? "thinker" : "worker"}`);
export function initial(owner: string, directory: string, paperCut: boolean): State {
  return { version: 1, owner, directory, paperCut, allowChecksReview: paperCut,
    localOnly: true, helper: true, assignments: { helper: local("helper"), worker: local("worker"), expert: local("thinker") },
    workers: [], flow: [], wingman: { enabled: false, failures: {}, consultations: 0 },
    advice: { enabled: true, declined: {} },
    maxAttempts: 3, maxConsultations: 3, timeoutMin: 90, failureThreshold: 2 };
}
export function objective(title: string, profile: Profile, assignment: Assignment, criteria = title): Objective {
  return { id: id(), title, criteria, profile, assignment: { ...assignment }, dependencies: [], status: "pending", attempts: 0 };
}
export function template(state: State, task: string, base: string): Mission {
  const items = [objective("Understand and plan", "research", state.assignments.expert, "Provide a grounded implementation plan"),
    objective("Implement", "implement", state.assignments.worker, "Implement the task and describe the resulting changes")];
  if (state.allowChecksReview) items.push(
    objective("Check", "check", state.assignments.worker, "Run appropriate repository checks and report the actual results"),
    objective("Review", "review", state.assignments.expert, "Approve the exact snapshot or identify concrete findings"));
  items.forEach((item, i) => { if (i) item.dependencies = [items[i - 1].id]; });
  return { id: id(), task, base, status: "ready", objectives: items };
}
export function validateAssignment(value: Assignment, state: State): void {
  if (!["opencode", "codex", "claude"].includes(value.executor) || typeof value.model !== "string") throw new Error("Invalid executor or model");
  if (state.localOnly && (value.executor !== "opencode" || !/^hangar\/(helper|worker|thinker)$/.test(value.model)))
    throw new Error("Local-only policy permits hangar/helper, hangar/worker and hangar/thinker only. Enable cloud explicitly first.");
  if (value.executor === "opencode" && !/^[^/\s]+\/[^\s]+$/.test(value.model)) throw new Error("Use provider/model");
}
export function validateObjectives(state: State): void {
  const items = state.mission?.objectives ?? [];
  const seen = new Set<string>();
  for (const item of items) {
    if (!["done", "skipped"].includes(item.status)) validateAssignment(item.assignment, state);
    if (!item.title.trim() || !item.criteria.trim()) throw new Error("An objective needs a title and completion criteria");
    if ((item.profile === "check" || item.profile === "review") && !state.allowChecksReview)
      throw new Error("Checks and reviews are disabled by repository policy; enable them explicitly first");
    if (!item.dependencies.every(dep => seen.has(dep))) throw new Error("Dependencies must appear before their objective");
    seen.add(item.id);
  }
}
export function note(state: State, text: string, workerID?: string): void {
  state.flow.push({ at: new Date().toISOString(), text, workerID });
}
export const readOnly = (profile: Profile) => profile === "research" || profile === "review";
export const clip = (value: string, max = 24000) => value.length <= max ? value : `${value.slice(0, max / 2)}\n… truncated …\n${value.slice(-max / 2)}`;
