import { spawn } from "node:child_process";
import type { Assignment, Profile, Worker } from "./model.ts";
import { clip, readOnly } from "./model.ts";
import type { Result } from "./controller.ts";

export async function processRun(command: string, args: string[], cwd: string, input: string, signal: AbortSignal,
  progress?: (line: string) => void): Promise<string> {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd, stdio: ["pipe", "pipe", "pipe"], detached: process.platform !== "win32" });
    let stdout = "", stderr = "", pending = "", failure: Error | undefined;
    const stop = () => {
      try { if (process.platform === "win32") child.kill("SIGTERM"); else process.kill(-child.pid!, "SIGTERM"); } catch {}
    };
    const abort = () => { failure = new Error("Agent interrupted or timed out"); stop(); };
    const kill = setInterval(() => { if (failure) { try { if (process.platform === "win32") child.kill("SIGKILL"); else process.kill(-child.pid!, "SIGKILL"); } catch {} } }, 3000);
    signal.addEventListener("abort", abort, { once: true });
    child.stdout.on("data", data => {
      const text = data.toString(); stdout += text; pending += text;
      const lines = pending.split("\n"); pending = lines.pop()!;
      lines.forEach(line => progress?.(line));
      if (stdout.length > 8 * 1024 * 1024) { failure = new Error("Agent output exceeded 8 MiB"); stop(); }
    });
    child.stderr.on("data", data => { stderr = (stderr + data.toString()).slice(-12000); });
    child.once("error", error => { failure = error; });
    child.stdin.on("error", () => {});
    child.stdin.end(input);
    child.once("close", code => {
      clearInterval(kill); signal.removeEventListener("abort", abort);
      if (failure || code !== 0) reject(failure ?? new Error(`${command} exited ${code}: ${clip(stderr || stdout, 2000)}`));
      else resolve(stdout);
    });
    if (signal.aborted) abort();
  });
}

export async function externalAgent(assignment: Assignment, profile: Profile, prompt: string, cwd: string,
  signal: AbortSignal, worker: Worker, progress: () => Promise<void>): Promise<{ result: Result; patch?: string }> {
  const editing = !readOnly(profile);
  const instructions = `Follow repository instructions. Do not commit, publish, send messages or modify configuration.\nYou are delegated one objective. You have read-only tools. ${editing ? "Propose the implementation as a complete git-style text patch between MISSION_PATCH_BEGIN and MISSION_PATCH_END. The host will check permissions and apply it. Do not run checks or claim the patch was applied." : "Inspect and advise without modifying files or running checks. Commands are limited to reading project files."}\nEnd with MISSION_RESULT: followed by a JSON object with status (done, blocked, approve or changes) and summary.\n${prompt}`;
  let answer = "", agentFailure = "";
  const line = (value: string) => {
    try {
      const event = JSON.parse(value);
      if (typeof event.thread_id === "string") worker.sessionID = `codex:${event.thread_id}`;
      if (typeof event.session_id === "string") worker.sessionID = `claude:${event.session_id}`;
      if (event.type === "item.completed" && event.item?.type === "agent_message") answer += event.item.text + "\n";
      if (event.type === "result") {
        if (event.is_error) agentFailure = event.result || "Claude Code failed";
        answer = event.result ?? answer;
      }
      if (event.type === "turn.failed" || event.type === "error") agentFailure = JSON.stringify(event.error ?? event);
      worker.detail = String(event.type ?? "Agent working");
      worker.updatedAt = new Date().toISOString();
      if (answer) worker.preview = answer.slice(-12000);
      void progress().catch(() => {});
    } catch { /* Plain output remains available in the final transcript. */ }
  };
  const args = assignment.executor === "codex"
    ? ["exec", "--json", "--sandbox", "read-only", "--config", 'approval_policy="never"', "--config", 'web_search="disabled"', ...(assignment.model === "default" ? [] : ["--model", assignment.model]), "-"]
    : ["-p", "--verbose", "--output-format", "stream-json", "--permission-mode", "default", "--permission-prompts", "none", "--tools", "Read,Glob,Grep", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', ...(assignment.model === "default" ? [] : ["--model", assignment.model])];
  const raw = await processRun(assignment.executor === "codex" ? "codex" : "claude", args, cwd, instructions, signal, line);
  worker.transcript = answer || raw;
  if (agentFailure) throw new Error(clip(agentFailure, 2000));
  if (!answer.trim()) throw new Error(`No agent answer: ${clip(raw, 1000)}`);
  const match = [...answer.matchAll(/^MISSION_RESULT:\s*(\{.*\})\s*$/gm)].at(-1);
  if (!match) throw new Error("Agent returned no structured MISSION_RESULT; transcript retained in worker detail");
  const result = JSON.parse(match[1]) as Result;
  if (!["done", "blocked", "approve", "changes"].includes(result.status) || typeof result.summary !== "string") throw new Error("Invalid agent result");
  const patch = answer.match(/MISSION_PATCH_BEGIN\r?\n([\s\S]*?)\r?\nMISSION_PATCH_END/)?.[1];
  if (editing && result.status === "done" && !patch?.trim()) throw new Error("Implementation agent returned no patch");
  return { result, patch: patch ? patch + "\n" : undefined };
}

export function patchPaths(patch: string): string[] {
  if (/^(?:GIT binary patch|rename |copy |.*mode 1(?:20000|60000))/m.test(patch)) throw new Error("Only regular text-file patches are supported");
  const headers = [...patch.matchAll(/^diff --git a\/([^\s]+) b\/([^\s]+)$/gm)];
  if (!headers.length || headers.length !== (patch.match(/^diff --git /gm)?.length ?? 0)) throw new Error("Patch needs unquoted git-style paths without spaces");
  return [...new Set(headers.flatMap(m => [m[1], m[2]]))].map(path => {
    if (path.startsWith("/") || path.split("/").includes("..") || /[\\\x00-\x1f]/.test(path)) throw new Error("Unsafe patch path");
    return path;
  });
}
