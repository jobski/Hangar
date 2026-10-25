import { createHash } from "node:crypto";
import type { Plugin } from "@opencode/plugin";
import type { Controller } from "./controller.ts";
import { processRun } from "./agents.ts";
import { clip, local, type Assignment } from "./model.ts";

export const QUESTION_TOOL = "ask_technical_question";
export const QUESTION_INSTRUCTIONS = "When you would otherwise stop for user input because of unresolved technical uncertainty, use ask_technical_question to offer a local thinker consultation and available agent backups. Ask only when the decision materially blocks progress; resolve routine choices yourself. Use the ordinary question tool for user preferences, requirements, spending, destructive actions and approvals. An adviser cannot answer those on the user's behalf or grant permission. Do not repeat a declined consultation offer.";

export async function questionOffers(ctx: Plugin.Context, controller: Controller, directory: string,
  isManaged: (sessionID: string) => boolean, resume: (sessionID: string) => void, lifetime: AbortSignal) {
  let cached: { until: number; codex: boolean; claude: boolean } | undefined;
  const answered = new Map<string, { content: string }>();
  const availability = async (parent: AbortSignal) => {
    if (cached && cached.until > Date.now()) return cached;
    const signal = AbortSignal.any([lifetime, parent, AbortSignal.timeout(8000)]);
    const [codex, claude] = await Promise.allSettled([
      processRun("codex", ["login", "status"], directory, "", signal),
      processRun("claude", ["auth", "status"], directory, "", signal),
    ]);
    let claudeReady = false;
    if (claude.status === "fulfilled") { try { claudeReady = JSON.parse(claude.value).loggedIn === true; } catch {} }
    parent.throwIfAborted(); lifetime.throwIfAborted();
    cached = { until: Date.now() + 60000, codex: codex.status === "fulfilled", claude: claudeReady };
    return cached;
  };
  await ctx.tool.transform(tools => {
    const questionTool = tools.list().find(tool => tool.name === "question");
    if (!questionTool) return;
    tools.add({ name: QUESTION_TOOL, options: { codemode: false },
      description: "Ask one blocking technical question, offering the local thinker first and signed-in Codex/Claude Code as optional backups. The user chooses whether to consult or answer. Never use for preferences, requirements or authorisation; use question for those. Advice is read-only and is not user approval.",
      input: { type: "object", properties: {
        question: { type: "string", minLength: 1, maxLength: 4000 },
        context: { type: "string", maxLength: 12000, description: "Focused context and attempts, shown to the user before they choose an adviser. The adviser also receives the current workspace diff and can read project files." },
        options: { type: "array", maxItems: 5, items: { type: "object", properties: {
          label: { type: "string" }, description: { type: "string" },
        }, required: ["label", "description"], additionalProperties: false }, description: "Choices the user can answer directly, if any" },
      }, required: ["question", "context"], additionalProperties: false },
      execute: async (input, context) => {
        if (isManaged(context.sessionID) || !controller.canOffer()) throw new Error("Technical question offers are available in normal chat outside missions");
        const session = await ctx.session.get({ sessionID: context.sessionID });
        if (session.parentID) throw new Error("Ask technical questions from the main conversation");
        const value = input as { question: string; context: string; options?: Array<{ label: string; description: string }> };
        const fingerprint = createHash("sha256").update(value.question.trim().toLowerCase().replace(/\s+/g, " ")).digest("hex");
        const key = `${context.sessionID}:${fingerprint}`;
        const previous = answered.get(key);
        if (previous) return previous;
        const declined = controller.state?.advice.declined[context.sessionID]?.includes(fingerprint);
        const agents = declined ? undefined : await availability(context.signal);
        context.signal.throwIfAborted();
        const candidates: Array<{ label: string; description: string; assignment: Assignment }> = declined ? [] : [
          { label: "Ask Thinker (local)", description: "Ask Hangar's thinker role. Wait for loading, then return advice to this worker.", assignment: local("thinker") },
          ...(agents?.codex ? [{ label: "Ask Codex (cloud)", description: "Send this question, context and workspace diff to your signed-in Codex agent for this consultation only.", assignment: { executor: "codex" as const, model: "default" } }] : []),
          ...(agents?.claude ? [{ label: "Ask Claude Code (cloud)", description: "Send this question, context and workspace diff to your signed-in Claude Code agent for this consultation only.", assignment: { executor: "claude" as const, model: "default" } }] : []),
        ];
        const options = (value.options ?? []).filter(option => !candidates.some(candidate => candidate.label === option.label) && option.label !== "Answer myself");
        const ask = (offer: boolean) => questionTool.execute({ questions: [{ header: "Technical decision",
          question: `${value.question}\n\nContext: ${value.context}\n\n${offer ? "Advisers can read project files and the current diff. Choosing a cloud adviser authorises this consultation only. You can also answer directly." : "Please give your own answer."}`,
          options: offer ? [...candidates.map(({ label, description }) => ({ label, description })), ...options,
            { label: "Answer myself", description: "Answer this question without consulting another model." }] : options,
        }] }, context);
        let response;
        try { response = await ask(!declined); }
        catch (error) {
          if (!context.signal.aborted && !lifetime.aborted) await controller.declineAdvice(context.sessionID, fingerprint);
          throw error;
        }
        const answers = (response.output as { answers?: string[][] } | undefined)?.answers?.[0] ?? [];
        const candidate = answers.length === 1 ? candidates.find(item => item.label === answers[0]) : undefined;
        if (!candidate) {
          await controller.declineAdvice(context.sessionID, fingerprint);
          if (answers[0] === "Answer myself") response = await ask(false);
          const content = typeof response.content === "string" ? response.content : response.content?.filter(part => part.type === "text").map(part => part.text).join("\n");
          const result = { content: content || "The user did not provide an answer. Do not infer a decision." };
          if (answered.size >= 100) answered.delete(answered.keys().next().value!);
          answered.set(key, result);
          return result;
        }
        context.signal.throwIfAborted();
        resume(context.sessionID);
        try {
          const advice = await controller.consultOnce(context.sessionID, candidate.assignment,
            `Question: ${value.question}\nContext shown to the user: ${value.context}\nCandidate choices: ${options.map(option => `${option.label}: ${option.description}`).join("\n")}`, context.signal);
          return { content: `Advice from ${candidate.label} (not a user answer or authorisation):\n${advice}\nContinue the current task using this advice where appropriate. Any remaining user preference or permission still requires the user.` };
        } catch (error) {
          if (context.signal.aborted || lifetime.aborted) throw error;
          return { content: `${candidate.label} consultation stopped: ${clip(error instanceof Error ? error.message : String(error), 1500)}. No backup was started. The user can explicitly choose another adviser or answer the question themselves.` };
        }
      } });
  });
}
