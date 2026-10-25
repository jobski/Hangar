import type { Evidence } from "./model.ts";
export function criteriaList(criteria: string): string[] {
  return criteria.split(/\n+/).map(line => line.replace(/^\s*(?:[-*•]|\d+[.)])\s*/, "").trim()).filter(Boolean);
}
const key = (text: string) => text.toLowerCase().replace(/\s+/g, " ").trim();
export function criterionEvidence(criteria: string, supplied?: Evidence[]): Evidence[] {
  if (supplied !== undefined && (!Array.isArray(supplied) || supplied.length > 30)) throw new Error("Evidence must contain at most 30 criterion entries");
  const entries = (supplied ?? []).map(entry => {
    if (!entry || typeof entry.criterion !== "string" || typeof entry.detail !== "string" ||
        !["met", "unmet", "unverified"].includes(entry.status) || !entry.criterion.trim() || !entry.detail.trim())
      throw new Error("Each evidence entry needs a criterion, met/unmet/unverified status and supporting detail");
    return { criterion: entry.criterion.slice(0, 2000), status: entry.status, detail: entry.detail.slice(0, 4000) };
  });
  for (const criterion of criteriaList(criteria)) if (!entries.some(entry => key(entry.criterion) === key(criterion)))
    entries.push({ criterion, status: "unverified", detail: "No criterion-specific evidence was supplied by the worker." });
  return entries;
}
