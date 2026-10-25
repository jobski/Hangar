import type { Plugin } from "@opencode/plugin/tui";
import { useTerminalDimensions } from "@opentui/solid";
import { createMemo, createSignal, onCleanup, Show } from "solid-js";
import type { State } from "./model.ts";
import { duration, missionProgress, nextMissionAction, primaryWorker } from "./progress.ts";

export type OutputMode = "compact" | "verbose" | "hidden";
const FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
const MAX_OUTPUT = 32768;

export function displayOutput(text: string): string {
  return text.replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g, "")
    .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "").replace(/\r\n?/g, "\n")
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "")
    .replace(/(Bearer\s+)[\w.+\/-]+/gi, "$1[redacted]")
    .replace(/((?:api[_-]?key|access[_-]?token|password|secret)\s*[=:]\s*)["']?[^\s"',;]+/gi, "$1[redacted]")
    .slice(-MAX_OUTPUT);
}

export function LiveOutput(props: {
  context: Plugin.Context; state: () => State | undefined; now: () => number;
  mode: () => OutputMode; setMode: (mode: OutputMode) => void;
  watch: () => void; activity: () => void; report: () => void; attention: () => void; toggle: () => void;
}) {
  const context = props.context, theme = context.theme, dimensions = useTerminalDimensions();
  const view = () => missionProgress(props.state(), props.now());
  const worker = () => primaryWorker(props.state());
  const [pulse, setPulse] = createSignal(0);
  const [capture, setCapture] = createSignal({ sessionID: "", command: "", output: "", status: "", updatedAt: 0 });
  const [captureError, setCaptureError] = createSignal("");
  const [following, setFollowing] = createSignal(true);
  let scroll: { stickyScroll: boolean; scrollTo(value: number): void } | undefined, disposed = false, busy = false;
  let selected = "", shellID = "", cursor: number | undefined;
  let lastSync = 0, lastPoll = 0;
  const location = () => context.data.session.get(props.state()?.owner ?? "")?.location ?? context.location;
  const active = () => view()?.mission.status === "running" && !worker()?.attention;
  const indicator = () => active() ? FRAMES[pulse() % FRAMES.length] : view()?.mission.status === "done" ? "✓" : "■";
  const nativeText = createMemo(() => {
    const sessionID = worker()?.sessionID;
    if (!sessionID || sessionID.includes(":")) return "";
    const chunks: string[] = [];
    for (const message of context.data.session.message.list(sessionID).slice(-10)) {
      if (message.type !== "assistant") continue;
      for (const part of message.content) {
        if (part.type === "text") chunks.push(part.text.slice(-8000));
        if (part.type !== "tool" || part.state.status === "streaming") continue;
        const input = part.state.input;
        const command = input.command ?? input.code ?? input.filePath ?? input.path ?? input.pattern;
        chunks.push(`[${part.name} · ${part.state.status}]${typeof command === "string" ? `\n$ ${command.slice(0, 4000)}` : ""}`);
        if (part.state.status === "completed" || part.state.status === "error") {
          for (const content of part.state.content ?? []) if (content.type === "text") chunks.push(content.text.slice(-8000));
          if (part.state.status === "error") chunks.push(JSON.stringify(part.state.error));
        }
      }
    }
    return displayOutput(chunks.join("\n\n"));
  });
  const output = createMemo(() => {
    const w = worker(), live = capture();
    const base = nativeText() || w?.preview || w?.result || "";
    const streaming = live.sessionID === w?.sessionID && live.command
      ? `\n\n[command · ${live.status}]\n$ ${live.command}\n${live.output || "Waiting for command output…"}` : "";
    return displayOutput(base + streaming) || "No terminal output yet. Loading and model generation may be quiet; the status above shows the current activity.";
  });
  async function poll() {
    if (busy || disposed || props.mode() === "hidden" || Date.now() - lastPoll < 1000) return;
    lastPoll = Date.now();
    const w = worker(), sessionID = w?.sessionID;
    if (!sessionID || sessionID.includes(":")) return;
    busy = true;
    try {
      if (selected !== sessionID) {
        selected = sessionID; shellID = ""; cursor = undefined; lastSync = 0;
        setCapture({ sessionID, command: "", output: "", status: "", updatedAt: 0 }); setCaptureError("");
      }
      if (Date.now() - lastSync > 2500) {
        await Promise.all([context.data.session.message.sync(sessionID), context.data.shell.sync(location())]);
        lastSync = Date.now();
      }
      if (disposed || sessionID !== worker()?.sessionID) return;
      const shell = context.data.shell.list(location()).filter(s => s.metadata.sessionID === sessionID)
        .sort((a, b) => b.time.started - a.time.started)[0];
      if (shell && shell.id !== shellID) {
        shellID = shell.id; cursor = undefined;
        setCapture({ sessionID, command: shell.command, output: "", status: shell.status, updatedAt: 0 });
      }
      if (!shellID) return;
      if (cursor === undefined) {
        const page = await context.client.shell.output({ id: shellID, location: location(), cursor: Number.MAX_SAFE_INTEGER });
        cursor = Math.max(0, page.data.size - MAX_OUTPUT);
      }
      const page = await context.client.shell.output({ id: shellID, location: location(), cursor, limit: MAX_OUTPUT });
      if (disposed || sessionID !== worker()?.sessionID) return;
      cursor = page.data.cursor;
      setCapture(previous => ({ ...previous, status: shell?.status ?? "finished",
        output: (previous.output + page.data.output).slice(-MAX_OUTPUT), updatedAt: page.data.output ? Date.now() : previous.updatedAt }));
      setCaptureError("");
    } catch { if (!disposed) setCaptureError("Live command capture unavailable; native worker output is still available."); }
    finally { busy = false; }
  }
  const timer = setInterval(() => { setPulse(value => value + 1); void poll(); }, 400);
  onCleanup(() => { disposed = true; clearInterval(timer); });
  void poll();
  const jump = () => { setFollowing(true); if (scroll) { scroll.stickyScroll = true; scroll.scrollTo(Infinity); } };
  const verboseHeight = () => Math.max(3, Math.min(10, Math.floor(dimensions().height * 0.25)));
  const compactText = () => output().split("\n").slice(-3).join("\n");
  return <Show when={view()}>{progress => <box paddingLeft={1} paddingRight={1} marginBottom={1}>
    <box flexDirection="row" gap={1}>
      <text fg={theme.text.base}><b>{indicator()} MISSION · {progress().heading}</b> · {progress().count}{progress().elapsed ? ` · ${progress().elapsed}` : ""}</text>
    </box>
    <box flexDirection="row" gap={2}>
      <text fg={theme.text.muted} onMouseUp={() => props.setMode("compact")}>[{props.mode() === "compact" ? "● " : ""}Compact]</text>
      <text fg={theme.text.muted} onMouseUp={() => props.setMode("verbose")}>[{props.mode() === "verbose" ? "● " : ""}Verbose]</text>
      <text fg={theme.text.muted} onMouseUp={() => props.setMode("hidden")}>[{props.mode() === "hidden" ? "● " : ""}Hide]</text>
      <text fg={theme.text.base} onMouseUp={props.watch}>[Open worker]</text>
      <text fg={theme.text.base} onMouseUp={props.activity}>[Activity]</text>
      <text fg={theme.text.base} onMouseUp={props.report}>[Report]</text>
      <Show when={!["done", "cancelled"].includes(progress().mission.status)}>
        <text fg={theme.text.base} onMouseUp={props.toggle}>[{progress().mission.status === "running" ? "Pause" : "Resume"}]</text>
      </Show>
    </box>
    <Show when={nextMissionAction(props.state())}>{next => <text fg={theme.text.base} onMouseUp={props.attention}>[{next().label}]</text>}</Show>
    <Show when={props.mode() !== "hidden"}>
      <Show when={progress().mission.status === "done"}><text fg={theme.text.base}>Mission finished · results, checks and outstanding items: [Report]</text></Show>
      <text fg={worker()?.attention || progress().mission.status === "paused" ? theme.text.feedback.warning.base : theme.text.base}>{progress().detail.slice(0, 240)}</text>
      <text fg={theme.text.muted}>{progress().freshness}{progress().quiet ? " · No new worker events; inspect the worker if needed" : ""}{capture().sessionID === worker()?.sessionID && capture().updatedAt ? ` · command output ${duration(props.now() - capture().updatedAt)} ago` : ""}</text>
      <Show when={props.mode() === "verbose"} fallback={<box height={3} overflow="hidden"><text fg={theme.text.muted}>{compactText()}</text></box>}>
        <scrollbox ref={value => { scroll = value; }} height={verboseHeight()} stickyScroll={following()} stickyStart="bottom"
          onMouseScroll={event => { if (event.scroll?.direction === "up") setFollowing(false); }}>
          <text fg={theme.text.muted}>{output()}</text>
        </scrollbox>
        <text fg={theme.text.muted} onMouseUp={jump}>[{following() ? "Following output" : "Scroll paused"} · Jump to latest]</text>
      </Show>
      <Show when={captureError()}><text fg={theme.text.muted}>{captureError()}</text></Show>
    </Show>
  </box>}</Show>;
}
