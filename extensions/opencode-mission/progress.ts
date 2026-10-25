import { canonicalModel, type Role, type State, type Worker } from "./model.ts";

export const working = (worker: Worker) => ["waiting", "loading", "working"].includes(worker.status);
export const roleLabel = (role: Role) => role === "expert" ? "thinker" : role;
export const assignmentName = (worker: Pick<Worker, "assignment">) => worker.assignment.executor === "opencode"
  ? canonicalModel(worker.assignment.model) : `${worker.assignment.executor} / ${worker.assignment.model}`;
export function recordedAssignment(worker: Pick<Worker, "assignment">) {
  return worker.assignment.executor === "opencode" && canonicalModel(worker.assignment.model) !== worker.assignment.model
    ? `Recorded role ID at execution: ${worker.assignment.model}` : "";
}
export function flowLabel(text: string) {
  return text.replace(/^expert(?=:| finished:)/, "thinker")
    .replace(/^((?:helper|worker|thinker): opencode\/|Question consultation accepted: opencode\/)(hangar\/(?:glm|qwen|main))(?=$|:)/,
      (_, prefix: string, model: string) => prefix + canonicalModel(model));
}
export function duration(ms: number) {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  return seconds < 60 ? `${seconds}s` : seconds < 3600 ? `${Math.floor(seconds / 60)}m ${seconds % 60}s`
    : `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
}
export function primaryWorker(state?: State) {
  const current = state?.mission?.objectives.find(o => o.status === "running")
    ?? state?.mission?.objectives.find(o => !["done", "skipped"].includes(o.status));
  const workers = state?.workers.filter(w => w.objectiveID && w.objectiveID === current?.id) ?? [];
  return workers.find(w => working(w) && w.attention) ?? workers.find(w => working(w) && w.role !== "helper")
    ?? workers.find(working) ?? workers.at(-1);
}
export function missionProgress(state?: State, now = Date.now()) {
  const mission = state?.mission;
  if (!mission) return undefined;
  const done = mission.objectives.filter(o => o.status === "done").length;
  const skipped = mission.objectives.filter(o => o.status === "skipped").length;
  const total = mission.objectives.length;
  const current = mission.objectives.find(o => o.status === "running")
    ?? mission.objectives.find(o => !["done", "skipped"].includes(o.status));
  const worker = primaryWorker(state);
  const started = mission.startedAt ?? state?.flow.findLast(e => e.text === "Mission started")?.at;
  const elapsed = started ? duration((mission.finishedAt ? Date.parse(mission.finishedAt) : now) - Date.parse(started)) : undefined;
  const age = worker?.updatedAt ? now - Date.parse(worker.updatedAt) : undefined;
  const heading = worker?.attention && mission.status === "running" ? "Needs your input"
    : ({ ready: "Ready to start", running: "Running", paused: "Paused", done: "Completed", cancelled: "Cancelled" })[mission.status];
  const detail = mission.reason || (mission.status === "running"
    ? `${current?.title ?? "Preparing next objective"} · ${worker ? assignmentName(worker) : current ? assignmentName(current) : "preparing"} · ${worker?.detail ?? "Preparing worker"}`
    : mission.status === "done" ? mission.objectives.findLast(o => o.status === "done")?.result ?? "All objectives completed"
    : mission.status === "cancelled" ? "Execution stopped. Existing changes are retained."
    : mission.status === "ready" ? "Inspect the objectives, then start the mission."
    : "Resume when ready, or edit the remaining objectives.");
  return { mission, current, worker, heading, detail, elapsed, done, skipped, total,
    count: `${done}/${total} objectives complete${skipped ? ` · ${skipped} skipped` : ""}`,
    freshness: age === undefined ? "Waiting for the first worker event" : `${duration(age)} since last worker event`,
    quiet: mission.status === "running" && age !== undefined && age > 60000,
    preview: worker?.preview?.replace(/\s+/g, " ").trim(),
  };
}

export function toolActivity(tool: string, input: unknown) {
  const value = input && typeof input === "object" ? input as Record<string, unknown> : {};
  const target = value.filePath ?? value.path ?? value.pattern ?? value.description;
  const name = tool.replace(/^mission_/, "");
  const action: Record<string, string> = { read: "Reading a file", glob: "Finding files", grep: "Searching source",
    edit: "Editing a file", write: "Writing a file", patch: "Applying changes", apply_patch: "Applying changes",
    shell: "Running a command", question: "Preparing a question", diff: "Inspecting workspace changes",
    result: "Recording completion result", ask_expert: "Requesting expert advice" };
  return `${action[name] ?? `Using ${name}`}${typeof target === "string" ? ` · ${target.slice(0, 140)}` : ""}`;
}
