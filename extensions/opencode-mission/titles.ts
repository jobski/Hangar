import type { Mission, State, Worker } from "./model.ts";
import { roleLabel } from "./progress.ts";
const compact = (text: string, length: number) => text.replace(/\s+/g, " ").trim().slice(0, length);
export const missionTitle = (mission: Mission) => `Mission: ${compact(mission.task, 85)}`;
export function workerTitle(state: State, worker: Worker) {
  const objective = state.mission?.objectives.find(item => item.id === worker.objectiveID);
  const task = objective?.title ?? (worker.role === "helper" ? "File discovery" : "Consultation");
  return `${roleLabel(worker.role)}: ${compact(task, 55)}${state.mission ? ` · ${compact(state.mission.task, 45)}` : ""}`;
}
