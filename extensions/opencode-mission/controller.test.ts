import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Controller, type Runtime, type Result } from "./controller.ts";
import { initial, type Profile, type Worker } from "./model.ts";
import { snapshot } from "./workspace.ts";

async function fixture(run: (worker: Worker, profile: Profile, prompt: string, signal: AbortSignal, directory: string) => Promise<Result>) {
  const directory = await mkdtemp(join(tmpdir(), "hangar-controller-"));
  const runtime: Runtime = { save: async () => {}, interrupt: async () => {}, idle: async () => true, deliver: async () => {},
    run: (worker, profile, prompt, signal) => run(worker, profile, prompt, signal, directory) };
  const controller = new Controller(directory, runtime);
  controller.state = initial("owner", directory, false); controller.state.helper = false; controller.state.allowChecksReview = true;
  await controller.action("owner", "create", { task: "Implement and validate a small change" });
  return controller;
}
async function settle(controller: Controller) {
  const deadline = Date.now() + 10000;
  while (controller.state.mission?.status === "running") {
    if (Date.now() > deadline) throw new Error("Controller did not reach an execution boundary");
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  // The state is saved before the execution promise releases the workspace lease.
  await new Promise(resolve => setTimeout(resolve, 5));
}

test("review findings cause fix, recheck and fresh snapshot approval", async () => {
  const calls: Profile[] = []; let reviews = 0, edits = 0;
  const c = await fixture(async (_, profile, _prompt, _signal, directory) => {
    calls.push(profile);
    if (profile === "implement") await writeFile(join(directory, "app.txt"), `change ${++edits}`);
    return profile === "review" ? (++reviews === 1 ? { status: "changes", summary: "Handle the empty input" } : { status: "approve", summary: "Empty input is handled" }) : { status: "done", summary: "Completed with evidence" };
  });
  await c.action("owner", "start"); await settle(c);
  assert.equal(c.state.mission!.status, "done");
  assert.deepEqual(calls, ["research", "implement", "check", "review", "implement", "check", "review"]);
  assert.equal(c.state.mission!.objectives.at(-1)!.snapshot, await snapshot(c.directory, c.state.mission!.base));
  await c.close();
});

test("identical findings after a fix pause without another fix", async () => {
  let edits = 0;
  const c = await fixture(async (_, profile, _prompt, _signal, directory) => {
    if (profile === "implement") await writeFile(join(directory, "app.txt"), `attempt ${++edits}`);
    return profile === "review" ? { status: "changes", summary: "Same concrete finding" } : { status: "done", summary: "Done" };
  });
  await c.action("owner", "start"); await settle(c);
  assert.equal(c.state.mission!.status, "paused"); assert.match(c.state.mission!.reason!, /same review findings/);
  assert.equal(edits, 2); await c.close();
});

test("edits after approval append fresh checks and a review", async () => {
  const calls: Profile[] = []; let edits = 0;
  const c = await fixture(async (_, profile, _prompt, _signal, directory) => {
    calls.push(profile); if (profile === "implement") await writeFile(join(directory, "app.txt"), `version ${++edits}`);
    return { status: profile === "review" ? "approve" : "done", summary: "Done" };
  });
  await c.action("owner", "start"); await settle(c);
  await c.action("owner", "add", { title: "A follow-up change", profile: "implement" });
  await c.action("owner", "resume"); await settle(c);
  assert.equal(c.state.mission!.status, "done"); assert.deepEqual(calls.slice(-3), ["implement", "check", "review"]); await c.close();
});

test("review cannot approve a workspace it changed", async () => {
  const c = await fixture(async (_, profile, _prompt, _signal, directory) => {
    if (profile === "review") await writeFile(join(directory, "illegal.txt"), "review edited code");
    return { status: profile === "review" ? "approve" : "done", summary: "Done" };
  });
  await c.action("owner", "start"); await settle(c);
  assert.equal(c.state.mission!.status, "paused"); assert.match(c.state.mission!.reason!, /Read-only workspace changed/); await c.close();
});

test("a check cannot use the review approval verdict", async () => {
  const c = await fixture(async (_, profile) => ({ status: profile === "check" ? "approve" : "done", summary: "Done" }));
  await c.action("owner", "start"); await settle(c);
  assert.equal(c.state.mission!.status, "paused"); assert.match(c.state.mission!.reason!, /Only a review/); await c.close();
});

test("review attempt budget stops before scheduling another fix", async () => {
  const c = await fixture(async (_, profile) => ({ status: profile === "review" ? "changes" : "done", summary: "A finding" }));
  c.state.maxAttempts = 1; await c.action("owner", "start"); await settle(c);
  assert.equal(c.state.mission!.status, "paused"); assert.match(c.state.mission!.reason!, /Attempt budget reached/);
  assert.equal(c.state.mission!.objectives.length, 4); await c.close();
});

test("clear archives results and worker sessions without changing files", async () => {
  const c = await fixture(async (_, profile, _prompt, _signal, directory) => {
    if (profile === "implement") await writeFile(join(directory, "app.txt"), "retained work");
    return { status: profile === "review" ? "approve" : "done", summary: "Recorded outcome" };
  });
  await c.action("owner", "start"); await settle(c);
  const before = await snapshot(c.directory, c.state.mission!.base), id = c.state.mission!.id;
  await c.action("owner", "clear");
  assert.equal(c.state.mission, undefined); assert.equal(c.state.archives!.length, 1);
  assert.equal(c.state.archives![0].mission.id, id); assert.equal(c.state.archives![0].workers.length, 4);
  assert.equal(await snapshot(c.directory, c.state.archives![0].mission.base), before);
  assert.match((await import("./report.ts")).missionReport(c.state)!, /Recorded outcome/);
  await c.action("owner", "create", { task: "Another task" }); assert.equal(c.state.archives!.length, 1); await c.close();
});

test("starting a new mission archives the previous completed one", async () => {
  const c = await fixture(async (_, profile) => ({ status: profile === "review" ? "approve" : "done", summary: "Completed previous mission" }));
  await c.action("owner", "start"); await settle(c);
  const old = c.state.mission!.id; await c.action("owner", "create", { task: "Next mission" });
  assert.equal(c.state.archives![0].mission.id, old); assert.notEqual(c.state.mission!.id, old); assert.equal(c.state.workers.length, 0); await c.close();
});

test("pause after objective waits for success instead of interrupting", async () => {
  let finish!: () => void, started!: () => void;
  const began = new Promise<void>(resolve => { started = resolve; });
  const gate = new Promise<void>(resolve => { finish = resolve; });
  let calls = 0;
  const c = await fixture(async () => { calls++; started(); await gate; return { status: "done", summary: "Boundary completed" }; });
  await c.action("owner", "start"); await began;
  await assert.rejects(c.action("owner", "clear"), /Finish or cancel/);
  await c.action("owner", "pause", { boundary: true });
  assert.equal(c.state.mission!.status, "running"); finish(); await settle(c);
  assert.equal(c.state.mission!.status, "paused"); assert.equal(calls, 1);
  assert.equal(c.state.mission!.objectives[0].status, "done"); assert.equal(c.state.workers[0].status, "done"); await c.close();
});

test("plugin reload records a specific stop reason and never auto-resumes", async () => {
  let started!: () => void; const began = new Promise<void>(resolve => { started = resolve; });
  const c = await fixture(async (_, _profile, _prompt, signal) => {
    started(); return new Promise<Result>((_resolve, reject) => signal.addEventListener("abort", () => reject(signal.reason), { once: true }));
  });
  await c.action("owner", "start"); await began; await c.close();
  assert.equal(c.state.mission!.pauseCause, "update"); assert.match(c.state.mission!.reason!, /reloaded/);
  assert.match(c.state.workers[0].detail, /plugin update/);
  const restored = new Controller(c.directory, c.runtime); await restored.restore(structuredClone(c.state));
  assert.equal(restored.state.mission!.status, "paused"); assert.equal(restored.state.mission!.pauseCause, "update"); await restored.close();
});

test("external Wingman worker is rejected without changing configuration", async () => {
  const c = await fixture(async () => ({ status: "done", summary: "Done" }));
  c.state.localOnly = false; c.state.wingman.enabled = true;
  await assert.rejects(c.action("owner", "assign", { role: "worker", assignment: { executor: "codex", model: "signed-in-model" } }), /Wingman.*OpenCode/i);
  assert.equal(c.state.assignments.worker.executor, "opencode"); assert.equal(c.state.wingman.enabled, true); await c.close();
});

test("missing evidence is visible and strict evidence blocks completion", async () => {
  const c = await fixture(async () => ({ status: "done", summary: "Claimed complete" }));
  c.state.requireEvidence = true; await c.action("owner", "start"); await settle(c);
  assert.equal(c.state.mission!.status, "paused"); assert.match(c.state.mission!.reason!, /evidence/);
  assert.equal(c.state.mission!.objectives[0].evidence![0].status, "unverified"); await c.close();
});

test("unmet criteria contradict done even without strict evidence", async () => {
  const c = await fixture(async () => ({ status: "done", summary: "Claimed complete", evidence: [{ criterion: "A grounded implementation plan", status: "unmet", detail: "No plan exists" }] }));
  await c.action("owner", "start"); await settle(c);
  assert.equal(c.state.mission!.objectives[0].status, "blocked"); assert.match(c.state.mission!.reason!, /evidence/); await c.close();
});

test("snapshot comparison reports later edits, including archived missions", async () => {
  const c = await fixture(async (_, profile) => ({ status: profile === "review" ? "approve" : "done", summary: "Done" }));
  await c.action("owner", "start"); await settle(c); const id = c.state.mission!.id;
  await c.action("owner", "verify"); assert.equal(c.state.snapshotChecks![id].matches, true);
  await writeFile(join(c.directory, "later.txt"), "edited after completion");
  await c.action("owner", "clear"); await c.action("owner", "verify", { id });
  assert.equal(c.state.snapshotChecks![id].matches, false); await c.close();
});

test("steering retains completed work and reaches the next worker prompt", async () => {
  let c: Controller, block = false, started!: () => void;
  const began = new Promise<void>(resolve => { started = resolve; }); const prompts: string[] = [];
  c = await fixture(async (_, profile, prompt, signal) => {
    prompts.push(prompt);
    if (block && profile === "implement") { block = false; started(); return new Promise<Result>((_resolve, reject) => signal.addEventListener("abort", () => reject(signal.reason), { once: true })); }
    return { status: profile === "review" ? "approve" : "done", summary: "Kept completed result" };
  });
  await c.action("owner", "start"); await settle(c);
  const completed = c.state.mission!.objectives.map(item => ({ id: item.id, result: item.result }));
  await c.action("owner", "add", { title: "Follow-up", profile: "implement" }); block = true;
  await c.action("owner", "resume"); await began;
  await c.action("owner", "steer", { text: "Preserve the legacy export format" });
  assert.equal(c.state.mission!.status, "paused"); assert.equal(c.state.mission!.instructions!.length, 1);
  await c.action("owner", "resume"); await settle(c);
  assert.ok(prompts.at(-1)!.includes("Preserve the legacy export format"));
  for (const old of completed) assert.equal(c.state.mission!.objectives.find(item => item.id === old.id)!.result, old.result);
  await c.close();
});

test("default model edits require explicit application to pending objectives", async () => {
  const c = await fixture(async () => ({ status: "done", summary: "Done" }));
  const original = c.state.mission!.objectives[1].assignment.model;
  await c.action("owner", "assign", { role: "worker", assignment: { executor: "opencode", model: "hangar/helper" } });
  assert.equal(c.state.mission!.objectives[1].assignment.model, original);
  await c.action("owner", "apply-defaults"); assert.equal(c.state.mission!.objectives[1].assignment.model, "hangar/helper"); await c.close();
});

test("criterion-specific evidence is retained in completion reports", async () => {
  let c: Controller;
  c = await fixture(async (worker, profile) => {
    const item = c.state.mission!.objectives.find(item => item.id === worker.objectiveID)!;
    return { status: profile === "review" ? "approve" : "done", summary: "Reported result", evidence: [{ criterion: item.criteria, status: "met", detail: "Concrete worker report" }] };
  });
  c.state.requireEvidence = true; await c.action("owner", "start"); await settle(c);
  assert.equal(c.state.mission!.status, "done");
  assert.match((await import("./completion.ts")).completionText(c.state), /worker-reported met/); await c.close();
});


test("Wingman enablement validates an external default before changing state", async () => {
  const c = await fixture(async () => ({ status: "done", summary: "Done" }));
  c.state.localOnly = false; c.state.assignments.worker = { executor: "codex", model: "signed-in-model" };
  await assert.rejects(c.action("owner", "wingman", { enabled: true }), /interactive worker must use OpenCode/);
  assert.equal(c.state.wingman.enabled, false); await c.close();
});

test("pending legacy review fingerprints migrate without losing findings", async () => {
  const c = await fixture(async () => ({ status: "done", summary: "Done" }));
  const review = c.state.mission!.objectives.at(-1)!;
  review.status = "pending"; review.attempts = 1; review.result = "Previously reported finding"; review.snapshot = "a".repeat(64);
  c.state.mission!.status = "paused";
  const restored = new Controller(c.directory, c.runtime); await restored.restore(structuredClone(c.state));
  assert.equal(restored.state.mission!.objectives.at(-1)!.findingsFingerprint, "a".repeat(64));
  assert.equal(restored.state.mission!.objectives.at(-1)!.snapshot, undefined);
  assert.equal(restored.state.mission!.objectives.at(-1)!.result, "Previously reported finding");
  await c.close(); await restored.close();
});
