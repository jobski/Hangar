import { Plugin } from "@opencode/plugin/tui";
import { LiveOutput, type OutputMode } from "./live-output.tsx";
import { createSignal, For, Show } from "solid-js";
import type { Assignment, Objective, Profile, Role, State, Worker } from "./model.ts";
import { missionRPC } from "./rpc.ts";
import { completionText } from "./completion.ts";
import { assignmentName, duration, flowLabel, nextMissionAction, missionProgress, primaryWorker, recordedAssignment, roleLabel, working } from "./progress.ts";

const symbol = (status: string) => ({ done: "✓", running: "●", working: "●", loading: "◐", waiting: "○", blocked: "!", failed: "!", skipped: "–", interrupted: "■" }[status] ?? "○");
const short = (text: string, limit = 140) => text.length > limit ? text.slice(0, limit) + "…" : text;
export default Plugin.define({
  id: "hangar.mission.ui",
  setup(context) {
    const [state, setState] = createSignal<State>();
    const [error, setError] = createSignal("");
    const [historyOpen, setHistoryOpen] = createSignal(false);
    const activeWorkers = () => state()?.workers.filter(working) ?? [];
    const pastWorkers = () => state()?.workers.filter(worker => !working(worker)) ?? [];
    const historyLabel = () => {
      const counts = ["interrupted", "failed", "done"].flatMap(status => {
        const count = pastWorkers().filter(worker => worker.status === status).length;
        return count ? [`${count} ${status === "done" ? "completed" : status}`] : [];
      });
      return `History · ${counts.join(", ")}`;
    };
    const [now, setNow] = createSignal(Date.now());
    const [outputPreferences, setOutputPreferences] = context.storage.store<{ mode: OutputMode }>("hangar.mission.output", { initial: { mode: "compact" } });
    const setOutputMode = (mode: OutputMode) => { void setOutputPreferences(draft => { draft.mode = mode; }); };
    async function outputMode(mode?: string) {
      const selected = ["compact", "verbose", "hidden"].includes(mode ?? "") ? mode as OutputMode
        : await context.ui.dialog.select<OutputMode>({ title: "Mission live output", current: outputPreferences.mode, options: [
          { title: "Compact: status and latest output", value: "compact" },
          { title: "Verbose: scrollable command output and worker text", value: "verbose" },
          { title: "Hidden: status line and controls only", value: "hidden" }] });
      if (selected) setOutputMode(selected);
    }
    const rpc = context.client.rpc(missionRPC);
    let disposed = false, refreshing = false;
    let lastNotice = "";
    const sessionID = () => {
      const route = context.ui.router.current();
      return route.type === "session" ? context.data.session.root(route.sessionID) : undefined;
    };
    async function action(action: string, payload: Record<string, unknown> = {}) {
      const owner = sessionID();
      if (!owner) throw new Error("Open a session first");
      const value = await rpc.action({ sessionID: owner, action, payload: JSON.stringify(payload) }, { location: context.data.session.get(owner)?.location });
      const next = JSON.parse((value as { json: string }).json) as State;
      if (!disposed) {
        if (state()?.mission && !next.mission) context.ui.panel.close();
        setState(next); setError("");
        const event = next.flow.findLast(e => /^(Mission (created|started|resumed|completed|paused|cancelled)|Objective (started|completed)|Execution stopped:|Checks failed|Review requested)/.test(e.text));
        const key = event ? `${next.mission?.id}:${event.at}:${event.text}` : "";
        if (key && lastNotice && key !== lastNotice && owner === next.owner) context.ui.toast.show({
          title: "Mission", message: short(event!.text, 220), sessionID: owner,
          variant: next.mission?.status === "paused" ? "warning" : next.mission?.status === "done" ? "success" : "info",
        });
        lastNotice = key;
      }
      return next;
    }
    async function guarded(work: () => Promise<unknown>) {
      try { await work(); }
      catch (error) { context.ui.toast.show({ variant: "error", message: error instanceof Error ? error.message : String((error as any)?.message ?? error) }); }
    }
    async function refresh() {
      if (refreshing || !sessionID() || disposed) return;
      refreshing = true;
      try { await action("status"); }
      catch (error) { if (!disposed) setError(String((error as any)?.message ?? error)); }
      finally { refreshing = false; }
    }
    async function paused() {
      if (state()?.mission?.status === "running") await action("pause");
    }
    async function pickAssignment(current: Assignment, interactiveWorker = false): Promise<Assignment | undefined> {
      const executor = await context.ui.dialog.select({ title: "Executor", current: current.executor,
        options: [{ title: "OpenCode / Hangar or connected provider", value: "opencode" },
          ...(!state()?.localOnly && !interactiveWorker ? [{ title: "Codex agent", value: "codex" }, { title: "Claude Code agent", value: "claude" }] : [])] });
      if (!executor) return;
      if (executor !== "opencode") {
        const model = await context.ui.dialog.prompt({ title: `${executor === "codex" ? "Codex" : "Claude Code"} model`, value: current.executor === executor ? current.model : "", description: "Use a model available to your signed-in agent. Implementation objectives return a patch for the host to apply." });
        if (model?.trim()) return { executor: executor as Assignment["executor"], model: model.trim() };
        return;
      }
      const connected = context.data.location.model.list(context.data.session.get(sessionID()!)?.location) ?? [];
      const defaults = ["hangar/helper", "hangar/worker", "hangar/thinker"];
      const choices = [...new Set([...defaults, ...connected.map(m => `${m.providerID}/${m.id}`)])].filter(m => !state()?.localOnly || defaults.includes(m));
      const model = await context.ui.dialog.select({ title: "Model", current: current.model, options: choices.map(value => ({ title: value, value })) });
      if (model) return { executor: "opencode", model };
    }
    async function create(task?: string) {
      const description = task?.trim() || await context.ui.dialog.prompt({ title: "New mission", description: "Describe the task. Objectives are editable before execution." });
      if (!description?.trim()) return;
      await action("create", { task: description });
      if (task?.trim()) await action("start");
      else await objectives();
    }
    async function add() {
      await paused();
      const title = await context.ui.dialog.prompt({ title: "Add objective" });
      if (!title?.trim()) return;
      const criteria = await context.ui.dialog.prompt({ title: "Done when", value: title });
      if (!criteria?.trim()) return;
      const profile = await context.ui.dialog.select<Profile>({ title: "Permissions", options: [
        { title: "Research / advice (read-only)", value: "research" }, { title: "Implementation", value: "implement" },
        ...(state()?.allowChecksReview ? [{ title: "Checks", value: "check" as const }, { title: "Review (read-only)", value: "review" as const }] : [])] });
      if (profile) await action("add", { title, criteria, profile });
    }
    async function edit(item: Objective) {
      const choice = await context.ui.dialog.select({ title: item.title, options: [
        { title: "View result and criteria", value: "details" },
        ...(!["done", "skipped"].includes(item.status) ? [
          { title: "Change this objective's executor / model", description: "Affects only this objective; default role models are separate", value: "assignment" }, { title: "Edit title and completion criteria", value: "edit" },
          { title: "Move objective", value: "move" }, { title: "Skip objective", value: "skip" }] : [])] });
      if (!choice) return;
      if (choice === "details") { await context.ui.dialog.alert({ title: item.title, message: `Done when: ${item.criteria}\nEvidence: ${(item.evidence ?? []).map(entry => `${entry.status}: ${entry.criterion} — ${entry.detail}`).join("\n") || "No criterion-specific evidence recorded"}\n${assignmentName(item)}\n${recordedAssignment(item)}\nAttempts: ${item.attempts}\n\n${item.result ?? "No result yet"}` }); return; }
      await paused();
      if (choice === "assignment") {
        const assignment = await pickAssignment(item.assignment);
        if (assignment) await action("edit", { id: item.id, assignment });
      } else if (choice === "edit") {
        const title = await context.ui.dialog.prompt({ title: "Objective title", value: item.title });
        if (!title?.trim()) return;
        const criteria = await context.ui.dialog.prompt({ title: "Completion criteria", value: item.criteria });
        if (criteria?.trim()) await action("edit", { id: item.id, title, criteria });
      } else if (choice === "move") {
        const index = await context.ui.dialog.select({ title: "Move to position", options: (state()?.mission?.objectives ?? []).map((o, index) => ({ title: `${index + 1}. ${o.title}`, value: index })) });
        if (index !== undefined) await action("move", { id: item.id, index });
      } else if (choice === "skip") await action("skip", { id: item.id });
    }
    async function help() {
      await context.ui.dialog.alert({ title: "Mission and Wingman guide", message: [
        "MANAGE: /mission clear archives a finished/cancelled mission and clears its display. /mission history opens earlier results and worker sessions. /mission summary opens a completion/progress report; /mission verify compares current files with its completion snapshot without rerunning checks.",
        "STEER: /mission steer <instructions> pauses safely, records your correction and offers resume. /mission pause after finishes the current objective before pausing. A forced plugin update instead retains changes and shows an explicit update-pause reason.",
        "EVIDENCE: completion reports distinguish worker-reported met criteria from unmet/unverified criteria. Enable Require criterion evidence in settings to block completion when evidence is missing. Legacy reports remain visible as having no criterion evidence.",
        "START: /mission <task> creates and starts a mission. /mission opens its objectives. /mission pause pauses; /mission resume continues.",
        "ADD A REVIEW: /mission settings → enable Checks / reviews. Then /mission add → enter a title and completion criteria → choose Review (read-only). Resume to run it. Findings create a worker fix followed by another review; approval finishes it. Repeated findings, attempt limits or blockers pause execution.",
        "MODELS: /mission settings → worker, thinker or helper → choose the executor and model. Worker implements/fixes; thinker plans, reviews and advises Wingman; helper locates files. Defaults are local Hangar roles. Change their underlying model targets in Hangar.",
        "EXISTING OBJECTIVES: /mission → select an unfinished objective → Change executor / model. Default model changes apply to new objectives; completed history stays intact.",
        "WINGMAN: /wingman on enables assistance. /wingman consult <question> asks the configured thinker. /wingman off disables it. Model defaults and limits are shared with Mission in this workspace.",
        "CLOUD: disabled by default. Enable Cloud delegation in settings to select connected providers or the installed Codex / Claude Code agents for objectives. Normal-chat cloud question consultations ask for consent each time.",
        "PROGRESS: The live pane above the composer shows worker text and captured command output. /mission output compact|verbose|hidden changes its display without pausing the mission. In Verbose, scroll up to stop following and use Jump to latest to follow again. /mission activity shows results and flow; /mission watch opens the active worker.",
        "SHORTCUTS: /mission-help, /mission-settings (also /wingman-settings), /mission-add. Settings and objective edits pause active mission work. New missions include checks/review only when enabled; enabling them does not add objectives to an existing mission.",
      ].join("\n\n") });
    }
    async function history() {
      const records = state()?.archives?.toReversed() ?? [];
      if (!records.length) { await context.ui.dialog.alert({ title: "Mission history", message: "No archived missions yet. Clear a finished/cancelled mission, or start a new one, to archive it." }); return; }
      const chosen = await context.ui.dialog.select({ title: "Mission history", options: records.map(record => ({
        title: `${record.mission.status} · ${short(record.mission.task, 100)}`, description: new Date(record.archivedAt).toLocaleString(), value: record.mission.id })) });
      const record = records.find(item => item.mission.id === chosen);
      if (!record) return;
      const choice = await context.ui.dialog.select({ title: short(record.mission.task, 80), options: [
        { title: "Completion report and results", value: "report" }, { title: "Worker sessions", value: "workers" }, { title: "Compare current files with completion snapshot", value: "verify" }] });
      if (choice === "report") await context.ui.dialog.alert({ title: "Archived mission report", message: completionText(state()!, record.mission) });
      if (choice === "verify") { await action("verify", { id: record.mission.id }); await context.ui.dialog.alert({ title: "Snapshot comparison", message: completionText(state()!, record.mission) }); }
      if (choice === "workers") {
        if (!record.workers.length) { await context.ui.dialog.alert({ title: "Archived workers", message: "No worker sessions were started for this mission." }); return; }
        const selected = await context.ui.dialog.select({ title: "Archived workers", options: record.workers.map(worker => ({ title: `${roleLabel(worker.role)} · ${worker.status} · ${assignmentName(worker)}`, description: worker.detail, value: worker.id })) });
        const worker = record.workers.find(item => item.id === selected); if (worker) await workerDetails(worker);
      }
    }
    async function clear() { await action("clear"); context.ui.panel.close(); }
    async function objectives() {
      const mission = state()?.mission;
      if (!mission) {
        const choice = await context.ui.dialog.select({ title: "Mission", options: [
          { title: "New mission", value: "new" }, { title: "Archived missions and results", value: "history" },
          { title: "Models and settings", value: "settings" }, { title: "Help", value: "help" }] });
        if (choice === "new") await create(); else if (choice === "history") await history();
        else if (choice === "settings") await settings(); else if (choice === "help") await help();
        return;
      }
      const choice = await context.ui.dialog.select({ title: `Mission · ${mission.status}`, options: [
        ...mission.objectives.map(o => ({ title: `${symbol(o.status)} ${o.title}`, description: assignmentName(o), value: o.id })),
        { title: "+ Add objective", description: "Add research, implementation, checks or a review (when enabled)", value: "add" },
        ...(!["done", "cancelled"].includes(mission.status) ? [{ title: "Give Mission instructions (steer)", value: "steer" }] : []),
        { title: "Completion / progress report", value: "summary" },
        { title: "Models and settings", description: "Worker, thinker, helper, review policy and execution limits", value: "settings" },
        { title: "Archived missions and results", value: "history" },
        ...(["done", "cancelled"].includes(mission.status) ? [{ title: "Clear mission display (archive results)", value: "clear" }] : []),
        { title: "Help: reviews, models and Wingman", value: "help" },
        { title: mission.status === "running" ? "Pause mission" : "Start / resume mission", value: "run" },
        ...(mission.status === "running" ? [{ title: "Pause after current objective", description: "Finish this objective before pausing for an update or new instructions", value: "boundary" }] : []),
        { title: "Cancel mission", value: "cancel" }] });
      if (choice === "add") await add();
      else if (choice === "steer") await steer();
      else if (choice === "summary") await summary();
      else if (choice === "settings") await settings();
      else if (choice === "help") await help();
      else if (choice === "history") await history();
      else if (choice === "clear") await clear();
      else if (choice === "run") await action(mission.status === "running" ? "pause" : "resume");
      else if (choice === "cancel") await action("cancel");
      else if (choice === "boundary") await action("pause", { boundary: true });
      else { const item = mission.objectives.find(o => o.id === choice); if (item) await edit(item); }
    }
    async function settings() {
      const s = await action("status");
      const choice = await context.ui.dialog.select({ title: "Default models for new objectives / Wingman", options: [
        ...(["helper", "worker", "expert"] as Role[]).map(role => ({ title: `${roleLabel(role)} default: ${assignmentName({ assignment: s.assignments[role] })}`,
          description: role === "worker" ? `New implementation/check/fix objectives${s.wingman.enabled ? "; Wingman worker must use OpenCode" : "; external agents supported for Missions"}` : role === "expert" ? "New planning/review objectives and Wingman adviser" : "Optional read-only file discovery", value: role })),
        ...(s.mission ? [{ title: "Apply defaults to unfinished objectives", description: "Explicitly replace existing pending assignments; completed history stays unchanged", value: "apply-defaults" }] : []),
        { title: `Helper support: ${s.helper ? "on" : "off"}`, value: "helper-toggle" },
        { title: `Technical question offers: ${s.advice.enabled ? "on" : "off"}`, value: "offers" },
        { title: `Cloud delegation: ${s.localOnly ? "disabled" : "enabled"}`, value: "cloud" },
        { title: "Attempt, consultation and time limits", value: "limits" },
        { title: `Require criterion evidence: ${s.requireEvidence ? "on" : "off"}`, description: "When on, missing/unverified evidence blocks done or approval; all claims remain worker-reported", value: "require-evidence" },
        { title: `Checks / reviews: ${s.allowChecksReview ? "enabled" : "disabled"}`, value: "checks" }] });
      if (!choice) return;
      await paused();
      if (["helper", "worker", "expert"].includes(choice)) {
        const role = choice as Role, assignment = await pickAssignment(s.assignments[role], role === "worker" && s.wingman.enabled);
        if (assignment) await action("assign", { role, assignment });
      } else if (choice === "apply-defaults") {
        if (await context.ui.dialog.confirm({ title: "Update unfinished objectives?", message: "Use the current thinker default for pending research/reviews and worker default for pending implementation/checks. This replaces individual pending assignments; completed history is retained." })) await action("apply-defaults");
      } else if (choice === "limits") {
        const key = await context.ui.dialog.select({ title: "Execution limit", options: [
          { title: `Attempts per objective: ${s.maxAttempts}`, value: "maxAttempts" },
          { title: `Expert consultations: ${s.maxConsultations}`, value: "maxConsultations" },
          { title: `Objective timeout (minutes): ${s.timeoutMin}`, value: "timeoutMin" },
          { title: `Failures before escalation: ${s.failureThreshold}`, value: "failureThreshold" }] });
        if (!key) return;
        const value = await context.ui.dialog.prompt({ title: "New limit", value: String(s[key as keyof State]) });
        if (value !== undefined) await action("limits", { [key]: Number(value) });
      } else if (choice === "offers") await action("offers", { enabled: !s.advice.enabled });
      else if (choice === "helper-toggle") await action("helper", { enabled: !s.helper });
      else if (choice === "cloud") {
        if (s.localOnly && !await context.ui.dialog.confirm({ title: "Enable cloud delegation?", message: "This permits explicitly assigned objectives and consultations to send code and context to connected cloud providers, Codex or Claude Code. Their usage limits and billing apply." })) return;
        await action("cloud", { enabled: s.localOnly });
      } else if (choice === "require-evidence") await action("require-evidence", { enabled: !s.requireEvidence });
      else if (choice === "checks") {
        if (!s.allowChecksReview && !await context.ui.dialog.confirm({ title: "Enable checks and reviews?", message: "This explicitly permits check and review objectives for this workspace. Existing repository restrictions still apply. Add the objectives to the mission after enabling." })) return;
        await action("checks", { enabled: !s.allowChecksReview });
      }
    }
    async function workerDetails(worker: Worker) {
      const choice = await context.ui.dialog.select({ title: `${roleLabel(worker.role)} · ${worker.status}`, options: [
        ...(worker.sessionID && !worker.sessionID.includes(":") ? [{ title: "Watch live worker session", value: "open" }] : []),
        { title: "View latest output and result", value: "details" }] });
      if (choice === "open") context.ui.router.navigate({ type: "session", sessionID: worker.sessionID! });
      if (choice === "details") await context.ui.dialog.alert({ title: assignmentName(worker), message: `${recordedAssignment(worker)}\n${worker.detail}\n${worker.updatedAt ? `Last event: ${new Date(worker.updatedAt).toLocaleTimeString()}` : ""}\n\n${worker.result ?? worker.preview ?? worker.transcript ?? "No output yet. The worker may be preparing or waiting for the model."}` });
    }
    async function workers() {
      const list = [...activeWorkers(), ...pastWorkers().toReversed()];
      if (!list.length) { await context.ui.dialog.alert({ title: "Mission workers", message: "No worker has started yet." }); return; }
      const selected = await context.ui.dialog.select({ title: "Mission workers and history", options: list.map(worker => ({
        title: `${symbol(worker.status)} ${roleLabel(worker.role)} · ${assignmentName(worker)} · ${worker.status}`,
        description: worker.detail, value: worker.id })) });
      const worker = list.find(worker => worker.id === selected);
      if (worker) await workerDetails(worker);
    }
    async function watch() {
      const s = state();
      const worker = primaryWorker(s) ?? s?.workers.findLast(w => s.mission?.objectives.some(o => o.id === w.objectiveID));
      if (!worker) { await context.ui.dialog.alert({ title: "Mission", message: "No worker has started yet. Start or resume the mission to begin." }); return; }
      if (worker.sessionID && !worker.sessionID.includes(":")) context.ui.router.navigate({ type: "session", sessionID: worker.sessionID });
      else await workerDetails(worker);
    }
    async function steer(instructions?: string) {
      const text = instructions?.trim() || await context.ui.dialog.prompt({ title: "Steer Mission", description: "Give a correction or additional requirement. Active work pauses; changes and completed results are retained. The next unfinished objective gets a fresh attempt budget." });
      if (!text?.trim()) return;
      await action("steer", { text });
      const choice = await context.ui.dialog.select({ title: "Instructions saved for the next worker", options: [
        { title: "Resume with these instructions", value: "resume" }, { title: "Keep paused and inspect objectives", value: "paused" }] });
      if (choice === "resume") await action("resume");
    }
    async function resolveAttention() {
      const next = nextMissionAction(state());
      if (next?.action === "watch") await watch();
      else if (next?.action === "settings") await settings();
      else if (next?.action === "objectives") await objectives();
      else if (next?.action === "steer") await steer();
      else if (next?.action === "resume") await action("resume");
    }
    async function summary() { context.ui.panel.open("mission.report"); }
    async function flow() {
      context.ui.panel.open("mission.activity");
    }
    context.ui.slot({ append: "session.composer.top", render: () => <Show when={state()?.mission && sessionID() === state()?.owner}>
      <LiveOutput context={context} state={state} now={now} mode={() => outputPreferences.mode} setMode={setOutputMode}
        watch={() => void guarded(watch)} activity={() => void guarded(flow)} report={() => void guarded(summary)} attention={() => void guarded(resolveAttention)}
        toggle={() => void guarded(() => action(state()?.mission?.status === "running" ? "pause" : "resume"))} />
    </Show> });
    context.ui.slot({ append: "session.panel", render: input => <Show when={input.name === "mission.report"}>
      <box flexGrow={1} height="100%" padding={1}>
        <box flexDirection="row" gap={2}><text fg={context.theme.text.base}><b>MISSION REPORT</b></text>
          <text fg={context.theme.text.muted} onMouseUp={input.toggleFullscreen}>[Fullscreen / Restore]</text>
          <text fg={context.theme.text.muted} onMouseUp={() => void guarded(async () => { await action("verify"); })}>[Compare current files]</text>
          <text fg={context.theme.text.muted} onMouseUp={input.close}>[Close]</text></box>
        <scrollbox flexGrow={1} focused={input.focused}><text fg={context.theme.text.base}>{state() ? completionText(state()!) : "Loading mission…"}</text></scrollbox>
      </box>
    </Show> });
    context.ui.slot({ append: "session.panel", render: input => <Show when={input.name === "mission.activity"}>
      <box flexGrow={1} height="100%" padding={1}>
        <box flexDirection="row" gap={2}>
          <text fg={context.theme.text.base}><b>MISSION ACTIVITY</b></text>
          <text fg={context.theme.text.muted} onMouseUp={input.toggleFullscreen}>[{input.presentation === "fullscreen" ? "Restore" : "Fullscreen"}]</text>
          <text fg={context.theme.text.muted} onMouseUp={input.close}>[Close]</text>
        </box>
        <scrollbox flexGrow={1} focused={input.focused}>
          <box gap={1}>
            <Show when={missionProgress(state(), now())} fallback={<text fg={context.theme.text.muted}>No mission yet. Use /mission to create one.</text>}>{view => <>
              <text fg={context.theme.text.base}><b>{view().heading}</b> · {view().count}{view().elapsed ? ` · ${view().elapsed}` : ""}</text>
              <text fg={context.theme.text.base}>{view().mission.task}</text>
              <text fg={context.theme.text.muted}>{view().detail}</text>
              <box flexDirection="row" gap={2}>
                <text fg={context.theme.text.base} onMouseUp={() => void guarded(watch)}>[Watch]</text>
                <Show when={!["done", "cancelled"].includes(view().mission.status)}>
                  <text fg={context.theme.text.base} onMouseUp={() => void guarded(() => action(view().mission.status === "running" ? "pause" : "resume"))}>[{view().mission.status === "running" ? "Pause" : view().mission.status === "ready" ? "Start" : "Resume"}]</text>
                </Show>
                <text fg={context.theme.text.base} onMouseUp={() => void guarded(objectives)}>[Objectives]</text>
              </box>
              <text fg={context.theme.text.base}><b>OBJECTIVES AND RESULTS</b></text>
              <For each={view().mission.objectives}>{item => <box marginBottom={1}>
                <text fg={context.theme.text.base} onMouseUp={() => void guarded(() => edit(item))}>{symbol(item.status)} {item.title} · {item.status} · {assignmentName(item)}</text>
                <text fg={context.theme.text.muted}>Done when: {item.criteria} · Attempts: {item.attempts}</text>
                <Show when={item.result}><text fg={context.theme.text.base}>{item.result}</text></Show>
              </box>}</For>
            </>}</Show>
            <text fg={context.theme.text.base}><b>WORKERS · click to inspect</b></text>
            <For each={state()?.workers.slice(-10)}>{worker => <box marginBottom={1} onMouseUp={() => void guarded(() => workerDetails(worker))}>
              <text fg={context.theme.text.base}>{symbol(worker.status)} {roleLabel(worker.role)} · {assignmentName(worker)} · {worker.status}</text>
              <text fg={context.theme.text.muted}>{worker.detail}{worker.updatedAt ? ` · ${duration(now() - Date.parse(worker.updatedAt))} since last event` : ""}</text>
              <Show when={worker.result ?? worker.preview}><text fg={context.theme.text.base}>{short(worker.result ?? worker.preview ?? "", 3000)}</text></Show>
            </box>}</For>
            <text fg={context.theme.text.base}><b>HISTORY</b></text>
            <For each={state()?.flow}>{event => <text fg={context.theme.text.muted} onMouseUp={() => void context.ui.dialog.alert({ title: "Recorded event", message: `${event.at}\n${event.text}` })}>{new Date(event.at).toLocaleTimeString()} · {flowLabel(event.text)}</text>}</For>
          </box>
        </scrollbox>
      </box>
    </Show> });
    context.ui.slot({ prepend: "sidebar.content", render: () => {
      const theme = context.theme;
      return <box gap={1}>
        <Show when={state()} fallback={<text fg={theme.text.muted}>Mission: {error() || "Connecting…"}</text>}>{s => <>
          <box>
            <text fg={theme.text.base} onMouseUp={() => void guarded(objectives)}><b>{s().mission?.status === "running" ? ["◐", "◓", "◑", "◒"][Math.floor(now() / 1500) % 4] + " " : ""}MISSION · {s().mission?.status ?? "Ready"}</b></text>
            <text fg={theme.text.muted}>{s().localOnly ? s().workers.some(w => w.oneOffCloud && ["waiting", "loading", "working"].includes(w.status)) ? "Local default · cloud consultation authorised" : "Local only · Hangar" : "Cloud enabled"}</text>
            <Show when={sessionID() !== s().owner}><text fg={theme.text.muted} onMouseUp={() => context.ui.router.navigate({ type: "session", sessionID: s().owner })}>[Open mission's controlling session]</text></Show>
            <Show when={s().mission}><text fg={theme.text.base}>{s().mission!.task}</text></Show>
            <Show when={missionProgress(s(), now())}>{view => <>
              <text fg={theme.text.muted}>{view().count}{view().elapsed ? ` · ${view().elapsed}` : ""}</text>
              <text fg={theme.text.base} onMouseUp={() => void guarded(flow)}>[Open activity / results]</text>
            </>}</Show>
            <Show when={nextMissionAction(s())}>{next => <text fg={theme.text.base} onMouseUp={() => void guarded(resolveAttention)}>[{next().label}]</text>}</Show>
            <Show when={s().mission?.reason}><text fg={theme.text.muted}>{short(s().mission!.reason!, 220)}</text></Show>
            <For each={s().mission?.objectives ?? []}>{item => <text fg={theme.text.base} onMouseUp={() => void guarded(() => edit(item))}>{symbol(item.status)} {item.title} · {assignmentName(item)}</text>}</For>
            <Show when={s().archives?.length}><text fg={theme.text.muted} onMouseUp={() => void guarded(history)}>[Mission history · {s().archives!.length}]</text></Show>
            <text fg={theme.text.muted} onMouseUp={() => void guarded(s().mission ? add : create)}>＋ {s().mission ? "Add objective" : "New mission"}</text>
          </box>
          <Show when={s().mission || s().wingman.enabled}><box>
            <text fg={theme.text.base} onMouseUp={() => void guarded(async () => { await action("wingman", { enabled: !s().wingman.enabled }); })}><b>WINGMAN · {s().wingman.enabled ? "On" : "Off"}</b></text>
            <text fg={theme.text.muted}>Thinker: {assignmentName({ assignment: s().assignments.expert })}</text>
            <text fg={theme.text.muted}>Consultations {s().wingman.consultations}/{s().maxConsultations}</text>
            <Show when={s().wingman.pending}><text fg={theme.text.muted}>Expert queued · waiting for turn end</text></Show>
          </box></Show>
          <Show when={s().workers.length}><box gap={1}>
            <box>
              <text fg={theme.text.base} onMouseUp={() => void guarded(workers)}><b>ACTIVE WORKERS · {activeWorkers().length}</b></text>
              <Show when={activeWorkers().length} fallback={<text fg={theme.text.muted}>No active workers</text>}>
                <For each={activeWorkers()}>{worker => <box onMouseUp={() => void guarded(() => workerDetails(worker))}>
                  <text fg={theme.text.base}>{symbol(worker.status)} {roleLabel(worker.role)} · {assignmentName(worker)}</text>
                  <text fg={theme.text.muted}>{worker.status} · {short(worker.detail)}</text>
                  <Show when={worker.updatedAt}><text fg={theme.text.muted}>{duration(now() - Date.parse(worker.updatedAt!))} since last event · click to watch</text></Show>
                </box>}</For>
              </Show>
            </box>
            <Show when={pastWorkers().length}><box>
              <text fg={theme.text.muted} onMouseUp={() => setHistoryOpen(value => !value)}>{historyOpen() ? "▾" : "▸"} {historyLabel()}</text>
              <Show when={historyOpen()}>
                <For each={pastWorkers().slice(-8).toReversed()}>{worker => <box onMouseUp={() => void guarded(() => workerDetails(worker))}>
                  <text fg={theme.text.muted}>{symbol(worker.status)} {roleLabel(worker.role)} · {assignmentName(worker)} · {worker.status}</text>
                  <text fg={theme.text.muted}>{short(worker.detail)}</text>
                </box>}</For>
                <text fg={theme.text.base} onMouseUp={() => void guarded(workers)}>[All workers and history · {s().workers.length}]</text>
              </Show>
            </box></Show>
          </box></Show>
          <Show when={s().mission || s().wingman.enabled}><box onMouseUp={() => void guarded(flow)}>
            <text fg={theme.text.base}><b>FLOW SO FAR</b></text>
            <For each={s().flow.slice(-5)}>{event => <text fg={theme.text.muted}>{short(flowLabel(event.text))}</text>}</For>
          </box></Show>
          <text fg={theme.text.muted} onMouseUp={() => void guarded(settings)}>[Settings] · /mission · /wingman</text>
        </>}</Show>
      </box>;
    } });
    context.keymap.layer(() => ({ mode: "global", commands: [
      { id: "hangar.mission", title: "Mission: create or manage objectives", description: "Start a task or manage objectives; /mission-help explains reviews, models and Wingman", group: "Hangar", palette: true, slash: { name: "mission", arguments: true }, run: input => guarded(async () => {
        await refresh();
        const text = input?.trim() ?? "";
        if (text === "pause after") await action("pause", { boundary: true });
        else if (["start", "resume", "pause", "cancel"].includes(text)) await action(text);
        else if (text === "steer" || text.startsWith("steer ")) await steer(text.slice(6));
        else if (text === "verify") { await action("verify"); await summary(); }
        else if (text === "clear") await clear();
        else if (text === "history") await history();
        else if (text === "settings") await settings();
        else if (text === "output" || text.startsWith("output ")) await outputMode(text.slice(7).trim());
        else if (text === "help") await help();
        else if (["flow", "activity", "status"].includes(text)) await flow();
        else if (text === "summary") await summary();
        else if (text === "watch") await watch();
        else if (text === "workers") await workers();
        else if (text === "add") await add();
        else if (!text) await objectives();
        else await create(text);
      }) },
      { id: "hangar.wingman", title: "Wingman: local expert assistance", description: "on / off / consult <question>; choose worker and thinker models in /mission-settings", group: "Hangar", palette: true, slash: { name: "wingman", arguments: true }, run: input => guarded(async () => {
        await refresh(); const text = input?.trim() ?? "";
        if (text === "on" || text === "off") await action("wingman", { enabled: text === "on" });
        else if (text === "consult" || text.startsWith("consult ")) await action("consult", { question: text.slice(8).trim() || undefined });
        else await settings();
      }) },
      { id: "hangar.mission.boundary", title: "Mission: pause after this objective", description: "Finish the current objective, then stop before starting another worker", group: "Hangar", palette: true, slash: { name: "mission-pause-after" }, run: () => guarded(() => action("pause", { boundary: true })) },
      { id: "hangar.mission.verify", title: "Mission: compare files with completion snapshot", description: "Read-only snapshot comparison; does not rerun tests or reviews", group: "Hangar", palette: true, slash: { name: "mission-verify" }, run: () => guarded(async () => { await action("verify"); await summary(); }) },
      { id: "hangar.mission.steer", title: "Mission: give instructions and resume", group: "Hangar", palette: true, slash: { name: "mission-steer", arguments: true }, run: input => guarded(() => steer(input)) },
      { id: "hangar.mission.clear", title: "Mission: clear display and archive results", description: "Finished/cancelled only; keeps files, reports and worker sessions", group: "Hangar", palette: true, slash: { name: "mission-clear" }, run: () => guarded(clear) },
      { id: "hangar.mission.history", title: "Mission: archived missions and results", description: "Open retained completion reports, evidence and native worker sessions", group: "Hangar", palette: true, slash: { name: "mission-history" }, run: () => guarded(history) },
      { id: "hangar.mission.summary", title: "Mission: completion and evidence report", description: "Results, criterion evidence, checks/reviews and outstanding items", group: "Hangar", palette: true, slash: { name: "mission-summary" }, run: () => guarded(summary) },
      { id: "hangar.mission.output", title: "Mission: live output display", description: "Choose Compact, Verbose or Hidden; display-only, does not pause or change execution", group: "Hangar", palette: true, slash: { name: "mission-output", arguments: true }, run: input => guarded(() => outputMode(input?.trim())) },
      { id: "hangar.mission.help", title: "Mission: help with reviews, models and Wingman", description: "How to add a review, configure models, use Wingman and follow progress", group: "Hangar", palette: true, slash: { name: "mission-help" }, run: () => guarded(help) },
      { id: "hangar.mission.add", title: "Mission: add an objective", description: "Add work or a review; enable Checks / reviews in /mission-settings first for review objectives", group: "Hangar", palette: true, slash: { name: "mission-add" }, run: () => guarded(async () => { await refresh(); await add(); }) },
      { id: "hangar.mission.flow", title: "Mission: show flow", group: "Hangar", palette: true, run: () => guarded(flow) },
      { id: "hangar.mission.workers", title: "Mission: active workers and history", description: "Inspect active workers or any past attempt, including interrupted sessions", group: "Hangar", palette: true, slash: { name: "mission-workers" }, run: () => guarded(workers) },
      { id: "hangar.mission.watch", title: "Mission: watch active worker", group: "Hangar", palette: true, run: () => guarded(watch) },
      { id: "hangar.mission.settings", title: "Mission: model assignments and policy", description: "Set worker, thinker and helper models; enable reviews and adjust limits (also /mission settings)", group: "Hangar", palette: true, slash: { name: "mission-settings", aliases: ["wingman-settings"] }, run: () => guarded(settings) },
      { id: "hangar.mission.pause", title: "Mission: pause active work", group: "Hangar", palette: true, run: () => guarded(() => action("pause")) },
      { id: "hangar.mission.consult", title: "Wingman: consult thinker now", group: "Hangar", palette: true, run: () => guarded(() => action("consult")) },
    ] }));
    const timer = setInterval(() => { setNow(Date.now()); void refresh(); }, 1500);
    void refresh();
    return () => { disposed = true; clearInterval(timer); };
  },
});
