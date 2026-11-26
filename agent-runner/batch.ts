import type { AgentTool, StreamFn } from "@earendil-works/pi-agent-core";
import type { McpClient } from "@earendil-works/pi-mcp";
import { auditedStream, sessionId } from "./calls.ts";
import { comparisons, RunnerError, safeInt } from "./profile.ts";
import type { Profile } from "./profile.ts";
import type { Models, Pi } from "./runtime.ts";

// The worker hands out one comparison at a time and owns its wording, checks and stored result.
const submitTools = ["submit_selection", "submit_ranking"] as const;

function receipts(s: Pi["schema"], batchId: number, profileSha256: string) {
  return {
    next: s.compile(s.union([
      s.obj({ status: s.lit("done") }, { open: true }),
      s.obj({
        status: s.lit("task"), batch_id: s.lit(batchId), task: s.oneOf(comparisons), tool: s.oneOf(submitTools),
        max_attempts: safeInt(s, 1), system_prompt: s.str({ minLength: 1 }), prompt: s.str({ minLength: 1 }),
        profile_sha256: s.lit(profileSha256), remaining_ms: s.num(),
      }, { open: true }),
    ])),
    call: s.compile(s.obj({ attempt: safeInt(s, 1) }, { open: true })),
  };
}

export async function runBatch(client: McpClient, pi: Pi, models: Models, profile: Profile, stream: StreamFn,
                               batchId: number) {
  const valid = receipts(pi.schema, batchId, profile.sha256);
  const limits = profile.limits;
  const catalog = await client.listTools();
  for (;;) {
    const reply = await client.callTool("next_comparison", {});
    const task = reply.structuredContent;
    if (reply.isError || !valid.next.check(task)) throw new RunnerError(1, "Invalid comparison task");
    if (task.status === "done") return;
    const declared = catalog.find(tool => tool.name === task.tool);
    if (!declared) throw new RunnerError(1, `Missing ${task.tool}`);
    if (task.max_attempts > limits.comparison_attempts_per_task) {
      throw new RunnerError(4, "Comparison attempt limit reached");
    }
    const role = models[profile.role_for[task.task]];
    const deadline = performance.now() + task.remaining_ms;
    // Each attempt is exactly one provider request, so the worker's per-task and batch budgets count attempts.
    let callsLeft = task.max_attempts;
    let sealed = false, tried = false;
    let fatal: RunnerError | undefined;

    function fail(error: unknown) {
      if (!(error instanceof RunnerError)) console.error(error);
      fatal = error instanceof RunnerError ? error : new RunnerError(1, "Comparison MCP or agent failed");
      sealed = true;
      agent.abort();
    }

    const submit: AgentTool = {
      name: declared.name, label: declared.name, description: declared.description ?? "",
      parameters: declared.inputSchema, constrainedSampling: { type: "json_schema", strict: "prefer" },
      async execute(_id, args, signal) {
        try {
          const result = await client.callTool(declared.name, args as Record<string, unknown>, { signal });
          const content = pi.mcp.toLlmContent(result);
          // A rejected submission names its problems; the model may correct them in the next attempt.
          if (result.isError) return { content, details: null, isError: true };
          sealed = true;
          return { content, details: null, terminate: true };
        } catch (error) {
          fail(error);
          return { content: [], details: null, terminate: true };
        }
      },
    };

    const agent = new pi.core.Agent({
      initialState: { model: role.model, thinkingLevel: role.config.reasoning },
      toolExecution: "sequential",
      async beforeToolCall() {
        if (sealed || tried) return { block: true, reason: "本轮已提交，后续调用已关闭。", terminate: sealed };
        tried = true;
        return undefined;
      },
      finishTurn({ toolResults, message }) {
        if (sealed || message.stopReason === "error" || message.stopReason === "aborted") return { action: "end" };
        tried = false;
        if (callsLeft <= 0 || performance.now() >= deadline) return { action: "end" };
        // The loop polls steering right after this hook and appends the message to the next turn.
        if (!toolResults.length) agent.steer({ role: "user", content: `请用 ${task.tool} 提交本轮结果。`, timestamp: Date.now() });
        return { action: "continue" };
      },
      streamFn(_model, context, options = {}) {
        const timeoutMs = Math.min(limits.model_call_seconds * 1000, deadline - performance.now());
        if (callsLeft <= 0 || timeoutMs <= 0) {
          fail(new RunnerError(4, "Comparison attempt or time limit reached"));
          throw fatal;
        }
        callsLeft--;
        return auditedStream({
          pi, stream, role, context, options, timeoutMs, abort: () => agent.abort(),
        }, async call => {
          const receipt = await client.callTool("record_call", { step: task.task, ...call });
          if (receipt.isError || !valid.call.check(receipt.structuredContent)) {
            throw new RunnerError(1, "record_call failed");
          }
        }, fail, { available: () => 0, consume: () => {} });
      },
    });
    // A fresh transcript per task: the worker's instructions and the one submit tool lead as a cacheable prefix.
    const declaredTools = [submit].map(pi.ai.toToolDeclaration);
    agent.state.tools = [submit];
    const system = pi.ai.createInitialSystemMessage(task.system_prompt, declaredTools);
    agent.state.messages = system ? [system] : [];
    agent.sessionId = sessionId(task.system_prompt, declaredTools);
    await agent.prompt(task.prompt);
    // An audited model error ends this task only; the worker decides whether the batch goes on.
    if (fatal?.code === 3) continue;
    if (fatal) throw fatal;
    const last = agent.state.messages.findLast(m => m.role === "assistant");
    if (!last || last.stopReason === "error" || last.stopReason === "aborted") throw new RunnerError(3, "pi_runtime");
  }
}
