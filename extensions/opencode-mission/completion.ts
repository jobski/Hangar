import { clip, type Mission, type State } from "./model.ts";
import { assignmentName } from "./progress.ts";

export function completionText(state: State, mission: Mission | undefined = state.mission ?? state.archives?.at(-1)?.mission): string {
  if (!mission) return "No current mission. Open Mission history for archived results.";
  const lines = [`MISSION · ${mission.status.toUpperCase()}`, mission.task,
    mission.finishedAt ? `Finished: ${new Date(mission.finishedAt).toLocaleString()}` : "", mission.reason ?? "",
    "RESULTS · reported by Mission workers"];
  for (const item of mission.objectives) lines.push(`\n${item.title} · ${item.status} · ${assignmentName(item)}`,
    `Done when: ${item.criteria}`, item.result ? clip(item.result, 6000) : "No result recorded.",
    ...(item.evidence?.map(entry => `Evidence · ${entry.status === "met" ? "worker-reported met" : entry.status}: ${entry.criterion} — ${entry.detail}`) ?? ["Evidence: criterion coverage not recorded (legacy result)."]));
  const outstanding = mission.objectives.filter(item => item.status !== "done");
  const uncertain = mission.objectives.flatMap(item => (item.evidence ?? []).filter(entry => entry.status !== "met").map(entry => `${item.title}: ${entry.criterion} · ${entry.status}`));
  const comparison = state.snapshotChecks?.[mission.id];
  lines.push("\nSNAPSHOT", comparison ? `${comparison.matches ? "Current files match" : "Current files differ from"} the recorded completion snapshot (compared ${comparison.at}). This does not rerun validation.` : "Current files have not been compared with the recorded completion snapshot.");
  lines.push("\nCRITERIA NEEDING EVIDENCE", uncertain.join("\n") || (mission.objectives.some(item => !item.evidence) ? "Legacy results have no criterion-specific evidence record." : "No unverified or unmet criteria recorded."));
  lines.push("\nOUTSTANDING", outstanding.length ? outstanding.map(item => `${item.title}: ${item.status}`).join("\n") : "No unfinished or skipped objectives recorded.");
  lines.push("\nCompletion describes the recorded objectives. Commits, publishing and deployment require their own recorded evidence and authorisation.");
  return lines.filter(Boolean).join("\n");
}
