import { Plugin } from "@opencode/plugin/tui";
import { createSignal, For, Show } from "solid-js";
import type { Assignment, Objective, Profile, Role, State, Worker } from "./model.ts";
import { missionRPC } from "./rpc.ts";
import { assignmentName, duration, flowLabel, missionProgress, primaryWorker, recordedAssignment, roleLabel, working } from "./progress.ts";

const symbol = (status: string) => ({ done: "✓", running: "●", working: "●", loading: "◐", waiting: "○", blocked: "!", failed: "!", skipped: "–", interrupted: "■" }[status] ?? "○");
const short = (text: string, limit = 140) => text.length > limit ? text.slice(0, limit) + "…" : text;
export default Plugin.define({
  id: "hangar.mission.ui",
  setup(context) {
    const [state, setState] = createSignal<State>();
    const [error, setError] = createSignal("");
    const [now, setNow] = createSignal(Date.now());
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
    async function pickAssignment(current: Assignment): Promise<Assignment | undefined> {
      const executor = await context.ui.dialog.select({ title: "Executor", current: current.executor,
        options: [{ title: "OpenCode / Hangar or connected provider", value: "opencode" },
          ...(!state()?.localOnly ? [{ title: "Codex agent", value: "codex" }, { title: "Claude Code agent", value: "claude" }] : [])] });
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
          { title: "Change executor / model", value: "assignment" }, { title: "Edit title and completion criteria", value: "edit" },
          { title: "Move objective", value: "move" }, { title: "Skip objective", value: "skip" }] : [])] });
      if (!choice) return;
      if (choice === "details") { await context.ui.dialog.alert({ title: item.title, message: `Done when: ${item.criteria}\n${assignmentName(item)}\n${recordedAssignment(item)}\nAttempts: ${item.attempts}\n\n${item.result ?? "No result yet"}` }); return; }
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
        "START: /mission <task> creates and starts a mission. /mission opens its objectives. /mission pause pauses; /mission resume continues.",
        "ADD A REVIEW: /mission settings → enable Checks / reviews. Then /mission add → enter a title and completion criteria → choose Review (read-only). Resume to run it. Findings create a worker fix followed by another review; approval finishes it. Repeated findings, attempt limits or blockers pause execution.",
        "MODELS: /mission settings → worker, thinker or helper → choose the executor and model. Worker implements/fixes; thinker plans, reviews and advises Wingman; helper locates files. Defaults are local Hangar roles. Change their underlying model targets in Hangar.",
        "EXISTING OBJECTIVES: /mission → select an unfinished objective → Change executor / model. Default model changes apply to new objectives; completed history stays intact.",
        "WINGMAN: /wingman on enables assistance. /wingman consult <question> asks the configured thinker. /wingman off disables it. Model defaults and limits are shared with Mission in this workspace.",
        "CLOUD: disabled by default. Enable Cloud delegation in settings to select connected providers or the installed Codex / Claude Code agents for objectives. Normal-chat cloud question consultations ask for consent each time.",
        "PROGRESS: /mission activity shows results and flow; /mission watch opens the active worker. The sidebar also opens objectives and workers.",
        "SHORTCUTS: /mission-help, /mission-settings (also /wingman-settings), /mission-add. Settings and objective edits pause active mission work. New missions include checks/review only when enabled; enabling them does not add objectives to an existing mission.",
      ].join("\n\n") });
    }
    async function objectives() {
      const mission = state()?.mission;
      if (!mission) return create();
      const choice = await context.ui.dialog.select({ title: `Mission · ${mission.status}`, options: [
        ...mission.objectives.map(o => ({ title: `${symbol(o.status)} ${o.title}`, description: assignmentName(o), value: o.id })),
        { title: "+ Add objective", description: "Add research, implementation, checks or a review (when enabled)", value: "add" },
        { title: "Models and settings", description: "Worker, thinker, helper, review policy and execution limits", value: "settings" },
        { title: "Help: reviews, models and Wingman", value: "help" },
        { title: mission.status === "running" ? "Pause mission" : "Start / resume mission", value: "run" },
        { title: "Cancel mission", value: "cancel" }] });
      if (choice === "add") await add();
      else if (choice === "settings") await settings();
      else if (choice === "help") await help();
      else if (choice === "run") await action(mission.status === "running" ? "pause" : "resume");
      else if (choice === "cancel") await action("cancel");
      else { const item = mission.objectives.find(o => o.id === choice); if (item) await edit(item); }
    }
    async function settings() {
      const s = await action("status");
      const choice = await context.ui.dialog.select({ title: "Mission settings", options: [
        ...(["helper", "worker", "expert"] as Role[]).map(role => ({ title: `${roleLabel(role)}: ${assignmentName({ assignment: s.assignments[role] })}`, value: role })),
        { title: `Helper support: ${s.helper ? "on" : "off"}`, value: "helper-toggle" },
        { title: `Technical question offers: ${s.advice.enabled ? "on" : "off"}`, value: "offers" },
        { title: `Cloud delegation: ${s.localOnly ? "disabled" : "enabled"}`, value: "cloud" },
        { title: "Attempt, consultation and time limits", value: "limits" },
        { title: `Checks / reviews: ${s.allowChecksReview ? "enabled" : "disabled"}`, value: "checks" }] });
      if (!choice) return;
      await paused();
      if (["helper", "worker", "expert"].includes(choice)) {
        const role = choice as Role, assignment = await pickAssignment(s.assignments[role]);
        if (assignment) await action("assign", { role, assignment });
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
      } else if (choice === "checks") {
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
    async function watch() {
      const s = state();
      const worker = primaryWorker(s) ?? s?.workers.findLast(w => s.mission?.objectives.some(o => o.id === w.objectiveID));
      if (!worker) { await context.ui.dialog.alert({ title: "Mission", message: "No worker has started yet. Start or resume the mission to begin." }); return; }
      if (worker.sessionID && !worker.sessionID.includes(":")) context.ui.router.navigate({ type: "session", sessionID: worker.sessionID });
      else await workerDetails(worker);
    }
    async function flow() {
      context.ui.panel.open("mission.activity");
    }
    context.ui.slot({ append: "session.composer.top", render: () => <Show when={state()?.mission && sessionID() === state()?.owner}>
      <Show when={missionProgress(state(), now())}>{view => <box paddingLeft={1} paddingRight={1} marginBottom={1}>
        <text fg={context.theme.text.base}><b>MISSION · {view().heading}</b> · {view().count}{view().elapsed ? ` · ${view().elapsed}` : ""}</text>
        <text fg={context.theme.text.muted}>{short(view().mission.task, 160)}</text>
        <text fg={view().worker?.attention || view().mission.status === "paused" ? context.theme.text.feedback.warning.base : context.theme.text.base}>{short(view().detail, 220)}</text>
        <Show when={view().mission.status === "running"}>
          <text fg={context.theme.text.muted}>{view().freshness}{view().quiet ? " · No new activity; open the worker for details" : ""}</text>
          <Show when={view().preview}><text fg={context.theme.text.muted}>Latest output (partial): {short(view().preview!, 160)}</text></Show>
        </Show>
        <box flexDirection="row" gap={2}>
          <text fg={context.theme.text.base} onMouseUp={() => void guarded(flow)}>[Activity]</text>
          <text fg={context.theme.text.base} onMouseUp={() => void guarded(watch)}>[Watch]</text>
          <Show when={!["done", "cancelled"].includes(view().mission.status)}>
            <text fg={context.theme.text.base} onMouseUp={() => void guarded(() => action(view().mission.status === "running" ? "pause" : "resume"))}>[{view().mission.status === "running" ? "Pause" : view().mission.status === "ready" ? "Start" : "Resume"}]</text>
          </Show>
          <text fg={context.theme.text.muted} onMouseUp={() => context.ui.router.navigate({ type: "session", sessionID: state()!.owner })}>[Main]</text>
        </box>
      </box>}</Show>
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
            <text fg={theme.text.base} onMouseUp={() => void guarded(objectives)}><b>MISSION · {s().mission?.status ?? "Ready"}</b></text>
            <text fg={theme.text.muted}>{s().localOnly ? s().workers.some(w => w.oneOffCloud && ["waiting", "loading", "working"].includes(w.status)) ? "Local default · cloud consultation authorised" : "Local only · Hangar" : "Cloud enabled"}</text>
            <Show when={sessionID() !== s().owner}><text fg={theme.text.muted} onMouseUp={() => context.ui.router.navigate({ type: "session", sessionID: s().owner })}>[Open mission's controlling session]</text></Show>
            <Show when={s().mission}><text fg={theme.text.base}>{s().mission!.task}</text></Show>
            <Show when={missionProgress(s(), now())}>{view => <>
              <text fg={theme.text.muted}>{view().count}{view().elapsed ? ` · ${view().elapsed}` : ""}</text>
              <text fg={theme.text.base} onMouseUp={() => void guarded(flow)}>[Open activity / results]</text>
            </>}</Show>
            <Show when={s().mission?.reason}><text fg={theme.text.muted}>{short(s().mission!.reason!, 220)}</text></Show>
            <For each={s().mission?.objectives ?? []}>{item => <text fg={theme.text.base} onMouseUp={() => void guarded(() => edit(item))}>{symbol(item.status)} {item.title} · {assignmentName(item)}</text>}</For>
            <text fg={theme.text.muted} onMouseUp={() => void guarded(s().mission ? add : create)}>＋ {s().mission ? "Add objective" : "New mission"}</text>
          </box>
          <box>
            <text fg={theme.text.base} onMouseUp={() => void guarded(async () => { await action("wingman", { enabled: !s().wingman.enabled }); })}><b>WINGMAN · {s().wingman.enabled ? "On" : "Off"}</b></text>
            <text fg={theme.text.muted}>Thinker: {assignmentName({ assignment: s().assignments.expert })}</text>
            <text fg={theme.text.muted}>Consultations {s().wingman.consultations}/{s().maxConsultations}</text>
            <Show when={s().wingman.pending}><text fg={theme.text.muted}>Expert queued · waiting for turn end</text></Show>
          </box>
          <Show when={s().workers.length}><box>
            <text fg={theme.text.base}><b>WORKERS</b></text>
            <For each={s().workers.slice(-5)}>{worker => <box onMouseUp={() => void guarded(() => workerDetails(worker))}>
              <text fg={theme.text.base}>{symbol(worker.status)} {roleLabel(worker.role)} · {assignmentName(worker)}</text>
              <text fg={theme.text.muted}>{worker.status} · {short(worker.detail)}</text>
              <Show when={worker.updatedAt && working(worker)}><text fg={theme.text.muted}>{duration(now() - Date.parse(worker.updatedAt!))} since last event · click to watch</text></Show>
            </box>}</For>
          </box></Show>
          <box onMouseUp={() => void guarded(flow)}>
            <text fg={theme.text.base}><b>FLOW SO FAR</b></text>
            <For each={s().flow.slice(-5)}>{event => <text fg={theme.text.muted}>{short(flowLabel(event.text))}</text>}</For>
          </box>
          <text fg={theme.text.muted} onMouseUp={() => void guarded(settings)}>[Settings] · /mission · /wingman</text>
        </>}</Show>
      </box>;
    } });
    context.keymap.layer(() => ({ mode: "global", commands: [
      { id: "hangar.mission", title: "Mission: create or manage objectives", description: "Start a task or manage objectives; /mission-help explains reviews, models and Wingman", group: "Hangar", palette: true, slash: { name: "mission", arguments: true }, run: input => guarded(async () => {
        await refresh();
        const text = input?.trim() ?? "";
        if (["start", "resume", "pause", "cancel"].includes(text)) await action(text);
        else if (text === "settings") await settings();
        else if (text === "help") await help();
        else if (["flow", "activity", "status"].includes(text)) await flow();
        else if (text === "watch") await watch();
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
      { id: "hangar.mission.help", title: "Mission: help with reviews, models and Wingman", description: "How to add a review, configure models, use Wingman and follow progress", group: "Hangar", palette: true, slash: { name: "mission-help" }, run: () => guarded(help) },
      { id: "hangar.mission.add", title: "Mission: add an objective", description: "Add work or a review; enable Checks / reviews in /mission-settings first for review objectives", group: "Hangar", palette: true, slash: { name: "mission-add" }, run: () => guarded(async () => { await refresh(); await add(); }) },
      { id: "hangar.mission.flow", title: "Mission: show flow", group: "Hangar", palette: true, run: () => guarded(flow) },
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
