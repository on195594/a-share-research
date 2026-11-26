import type { AgentTool, AgentToolResult, StreamFn } from "@earendil-works/pi-agent-core";
import type { JsonObject } from "@earendil-works/pi-ai";
import type { McpClient } from "@earendil-works/pi-mcp";
import { auditedStream, sessionId } from "./calls.ts";
import { RunnerError, safeInt } from "./profile.ts";
import type { Profile, Stage } from "./profile.ts";
import { decisions, runContextSchema } from "./prompts.ts";
import type { Decision } from "./prompts.ts";
import type { Models, Pi } from "./runtime.ts";

// Schemas and material are supplied by this run's MCP server. Only role selection lives here.
const repairEvidence = [
  "disclosures", "announcements", "read_disclosure", "calculate", "valuation",
  "macro", "commodities", "commodity", "industry_trend",
];
const authorEvidence = [...repairEvidence, "peers", "financials", "research_methods"];
const computes = new Set(["calculate", "valuation"]);
type Submission = Decision & { result?: AgentToolResult };
type Receipt = JsonObject & { charged?: boolean; evidence_delta?: 0 | 1 };

function receipts(s: Pi["schema"]) {
  return {
    evidence: s.compile(s.obj({ charged: s.bool(), evidence_delta: s.oneOf([0, 1]) }, { open: true })),
    call: s.compile(s.obj({ call_id: safeInt(s) }, { open: true })),
    decision: decisions(s),
  };
}

export async function runResearch(client: McpClient, pi: Pi, models: Models, profile: Profile, stream: StreamFn, runId: number) {
  const resource = await client.readResource("research://run/context");
  const contents = resource.contents[0];
  if (!contents || !("text" in contents)) throw new RunnerError(1, "Missing research context");
  const start = JSON.parse(contents.text);
  if (!pi.schema.compile(runContextSchema(pi.schema, runId, profile.sha256)).check(start)) {
    throw new RunnerError(2, "Research context or frozen profile mismatch");
  }
  const valid = receipts(pi.schema);
  const limits = profile.limits;
  const deadline = performance.now() + Math.min(start.remaining_ms, limits.run_seconds * 1000);
  const catalog = await client.listTools();
  const acquisitionEnd = performance.now() + limits.acquisition_seconds * 1000;
  let callsLeft = Math.min(start.effective_model_call_limit, limits.model_calls);
  let rounds = 0, stale = 0, charged = 0, fetches = 0;
  let canReturnDraft = false;

  function acquisitionStop(step: Stage, fetch: boolean) {
    const toolLimit = limits.explore_tool_calls + (step === "repair" ? limits.repair_extra_tool_calls : 0);
    if (performance.now() >= acquisitionEnd) return "time_limit";
    if (charged >= toolLimit) return "tool_limit";
    if (stale >= limits.no_progress_rounds) return "no_new_evidence";
    if (rounds >= limits.acquisition_rounds) return "round_limit";
    if (fetch && fetches >= limits.fetch_calls) return "fetch_limit";
  }

  function roleAgent(step: Stage) {
    const role = models[profile.role_for[step]];
    const evidence = step === "repair" ? repairEvidence : step === "explore" ? authorEvidence : [];
    const submit = step === "review" ? "review_decide" : step === "repair" ? "submit_patch" : "submit_draft";
    let outcome: Submission | undefined;
    const reportedStops = new Set<string>();
    async function recordStop(reason: string) {
      if (reportedStops.has(reason)) return;
      await client.request("research/acquisition_stop", { reason });
      reportedStops.add(reason);
    }
    let rewrite = false, rewritesLeft = limits.full_rewrites_per_invalid_patch, rejectedReviews = 0;
    let triedSubmission = false;
    let sealed = false;
    let fatal: RunnerError | undefined;

    function fail(error: unknown) {
      if (!(error instanceof RunnerError)) console.error(error);
      fatal = error instanceof RunnerError ? error : new RunnerError(1, "Research MCP or agent failed");
      sealed = true;
      agent.abort();
    }

    // Admission (sealed batches, one submission per batch, acquisition limits) runs in beforeToolCall.
    async function execute(name: string, args: Record<string, unknown>, signal?: AbortSignal): Promise<AgentToolResult> {
      try {
        const reply = await client.callTool(name, args, { signal });
        const data = reply.structuredContent as Receipt | undefined;
        const result: AgentToolResult = {
          content: pi.mcp.toLlmContent(reply), structuredContent: data, isError: reply.isError,
          details: null,
        };
        if (evidence.includes(name)) {
          if (!valid.evidence.check(data)) throw new RunnerError(1, "Invalid evidence receipt");
          result.details = { charged: data.charged, evidence_delta: data.evidence_delta };
          if (data.charged) {
            charged++;
            if (!computes.has(name)) fetches++;
          }
          return result;
        }
        if (reply.isError) return result;
        // No more calls in this batch may mutate the draft or its material, including during nested review.
        sealed = true;
        if (!data) throw new RunnerError(1, "Missing worker control receipt");
        outcome = { ...valid.decision(data), result };
        canReturnDraft = outcome.status !== "review";
        if (outcome.status === "review") {
          if (step === "review") throw new RunnerError(1, "Unexpected nested review receipt");
          if (callsLeft <= 0 || performance.now() >= deadline) throw new RunnerError(4, "No budget for required review");
          outcome = await reviewer.ask(outcome.system_prompt, outcome.prompt);
        }
        return { ...(outcome.result ?? result), terminate: true };
      } catch (error) {
        // Agent normally turns exceptions in tools into correctable results. Transport/audit failures are fatal.
        fail(error);
        return { content: [], details: null, terminate: true };
      }
    }

    const availableEvidence = catalog.some(tool => evidence.includes(tool.name));
    const available: AgentTool[] = catalog.filter(tool => tool.name === submit || tool.name === "submit_draft"
      || evidence.includes(tool.name)).map(tool => ({
      name: tool.name, label: tool.name, description: tool.description ?? "", parameters: tool.inputSchema,
      execute: (_id, args, signal) => execute(tool.name, args as Record<string, unknown>, signal),
    }));
    function loadout() {
      const control = rewrite ? "submit_draft" : submit;
      return available.filter(tool => tool.name === control || (evidence.includes(tool.name)
        && callsLeft > limits.draft_call_reserve && !acquisitionStop(step, !computes.has(tool.name))));
    }

    const agent = new pi.core.Agent({
      initialState: { model: role.model, thinkingLevel: role.config.reasoning },
      toolExecution: "sequential",
      async beforeToolCall({ toolCall }) {
        const isEvidence = evidence.includes(toolCall.name);
        if (sealed || (!isEvidence && triedSubmission)) {
          return { block: true, reason: "该次提交已处理，本批后续调用已关闭。", terminate: sealed };
        }
        if (!isEvidence) {
          triedSubmission = true;
          return undefined;
        }
        const reason = acquisitionStop(step, !computes.has(toolCall.name));
        if (!reason) return undefined;
        try {
          await recordStop(reason);
        } catch (error) {
          fail(error);
          return { block: true, reason: "", terminate: true };
        }
        return { block: true, reason: "本项取证已到上限，请用已有资料完成提交。" };
      },
      async prepareRequest() {
        if (!availableEvidence) return;
        const reason = acquisitionStop(step, true) ?? (callsLeft <= limits.draft_call_reserve ? "model_limit" : null);
        if (!reason) return;
        try {
          await recordStop(reason);
        } catch (error) {
          fail(error);
          throw fatal;
        }
      },
      // Runs before the loop declares tool changes, so the model sees exactly the tools the worker will execute.
      prepareNextTurnWithContext: ({ context }) => ({ context: { ...context, tools: loadout() } }),
      finishTurn({ toolResults, message }) {
        // Only calls that reached the evidence executor have a delta (including cached failures).
        const evidenceResults = toolResults.filter(r => (r.details as Receipt | undefined)?.evidence_delta !== undefined);
        if (evidenceResults.length) {
          rounds++;
          stale = evidenceResults.some(r => (r.details as Receipt | undefined)?.evidence_delta === 1) ? 0 : stale + 1;
        }
        if (sealed || message.stopReason === "error" || message.stopReason === "aborted") return { action: "end" };
        triedSubmission = false;
        if (rewrite && toolResults.some(r => r.toolName === "submit_draft")) rewritesLeft--;
        let correction: string | undefined;
        if (step === "review") {
          // The worker can classify an audited review with no valid decision as review_format.
          canReturnDraft = true;
          if (++rejectedReviews > limits.review_format_corrections) return { action: "end" };
          correction = "请用 review_decide 提交完整、有效的本轮决议。";
        } else if (step === "repair" && toolResults.some(r => r.toolName === "submit_patch" && r.isError)) {
          rewrite = true;
          correction = `补丁无效，请用 submit_draft 提交一份完整修订稿。\n${start.output_schema}`;
        } else if (!toolResults.length) {
          correction = `请用 ${rewrite ? "submit_draft" : submit} 提交本轮结果。`;
        }
        if (rewrite && rewritesLeft <= 0) return { action: "end" };
        if (canReturnDraft && (callsLeft <= 0 || performance.now() >= deadline)) return { action: "end" };
        // The loop polls steering right after this hook and appends the message to the next turn.
        if (correction) agent.steer({ role: "user", content: correction, timestamp: Date.now() });
        return { action: "continue" };
      },
      streamFn(_model, context, options = {}) {
        const timeoutMs = Math.min(limits.model_call_seconds * 1000, deadline - performance.now());
        if (callsLeft <= 0 || timeoutMs <= 0) {
          fail(new RunnerError(4, "Research call or time limit reached"));
          throw fatal;
        }
        const acquiring = pi.ai.getCurrentTools(context.messages).some(t => evidence.includes(t.name));
        callsLeft--;
        // Pi adds assistant partials and tool results to the live transcript during streaming.
        return auditedStream({
          pi, stream, role, context, options, timeoutMs, abort: () => agent.abort(),
        }, async call => {
          const receipt = await client.callTool("record_call", { step, ...call });
          if (receipt.isError || !valid.call.check(receipt.structuredContent)) {
            throw new RunnerError(1, "record_call failed");
          }
        }, fail, {
          available: () => Math.max(0, callsLeft - (acquiring ? limits.draft_call_reserve : 0)),
          consume: () => { callsLeft--; },
        }, async kind => { await client.request("research/model_error", { kind }); });
      },
    });
    return {
      async ask(systemPrompt: string, prompt: string): Promise<Submission> {
        agent.reset();
        outcome = undefined;
        sealed = false;
        rejectedReviews = 0;
        triedSubmission = false;
        rewrite = false;
        rewritesLeft = limits.full_rewrites_per_invalid_patch;
        // Each ask is a fresh transcript: the worker's instructions and tool loadout lead as one cacheable prefix.
        const initialTools = loadout();
        const declared = initialTools.map(pi.ai.toToolDeclaration);
        agent.state.tools = initialTools;
        const system = pi.ai.createInitialSystemMessage(systemPrompt, declared);
        agent.state.messages = system ? [system] : [];
        agent.sessionId = sessionId(systemPrompt, declared);
        await agent.prompt(prompt);
        if (fatal) throw fatal;
        const last = agent.state.messages.findLast(m => m.role === "assistant");
        if (!last || last.stopReason === "error" || last.stopReason === "aborted") throw new RunnerError(3, "pi_runtime");
        if (outcome) return outcome;
        // Rejected review/patch attempts leave the worker's last draft and unresolved issues intact.
        if (canReturnDraft) return { status: "failed" };
        throw new RunnerError(1, "Missing submitted draft");
      },
    };
  }

  const reviewer = roleAgent("review");
  let result = await roleAgent(start.mode === "replay" ? "compose" : "explore").ask(start.system_prompt, start.prompt);
  for (let draft = 1; result.status === "repair" && draft < limits.review_rounds; draft++) {
    if (callsLeft < limits.draft_call_reserve || performance.now() >= deadline) break;
    result = await roleAgent("repair").ask(result.system_prompt, result.prompt);
  }
}
