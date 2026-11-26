import type { StreamFn } from "@earendil-works/pi-agent-core";
import type {
  AssistantMessage, SimpleStreamOptions, TranscriptContext,
} from "@earendil-works/pi-ai";
import { RunnerError, sha256 } from "./profile.ts";
import type { Pi, ResolvedRole } from "./runtime.ts";

export type ErrorKind = "timeout" | "provider_error" | "pi_runtime" | "incomplete:length" | "incomplete"
  | "empty_output" | "invalid_output";

function failure(message: AssistantMessage, role: ResolvedRole): ErrorKind | null {
  const stop = message.stopReason;
  if (stop === "aborted") return "timeout";
  if (stop === "error") return "provider_error";
  if (stop === "length") return "incomplete:length";
  if (stop !== "stop" && stop !== "toolUse") return "incomplete";
  if (message.provider !== role.config.provider || message.model !== role.config.model) return "invalid_output";
  const hasTools = message.content.some(block => block.type === "toolCall");
  const hasText = message.content.some(block => block.type === "text" && block.text.trim());
  if (!hasTools && !hasText) return "empty_output";
  if (hasTools !== (stop === "toolUse")) return "invalid_output";
  return null;
}

interface Invocation {
  pi: Pi;
  stream: StreamFn;
  role: ResolvedRole;
  context: TranscriptContext;
  options: SimpleStreamOptions;
  timeoutMs: number;
  abort: () => void;
}

// Each retry is another provider request: the caller grants how many it can pay for and is charged before each one.
export interface RetryBudget {
  available: () => number;
  consume: () => void;
}

// Only the reviewed fields cross the audit boundary. Provider error text may contain credentials.
async function attempt({ pi, stream, role, context, options }: Invocation, number: number, timeout: number) {
  const { provider, model, reasoning, max_tokens } = role.config;
  const sessionId = options.sessionId!;
  const parameters = { maxTokens: max_tokens, reasoning, transport: "sse" as const, maxRetries: 0, sessionId };
  let response: AssistantMessage | null = null;
  let error_kind: ErrorKind | null;
  const started = performance.now();
  try {
    const events = await stream(role.model, context, {
      ...options, ...parameters, reasoning: reasoning === "off" ? undefined : reasoning,
    });
    // Draining keeps the event stream from buffering every delta until result() is read.
    for await (const event of events) {
      if (event.type === "done" || event.type === "error") break;
    }
    response = await events.result();
    error_kind = options.signal?.aborted ? "timeout" : failure(response, role);
  } catch {
    error_kind = options.signal?.aborted ? "timeout" : "pi_runtime";
  }
  const stop_reason = response?.stopReason ?? null;
  const call = {
    response: error_kind ? null : response, error_kind, stop_reason, usage: response?.usage ?? null,
    seconds: (performance.now() - started) / 1000,
    identity: {
      requested: { provider, model },
      actual: response?.provider === provider && response.model === model ? { provider, model } : null,
      pi_version: pi.version, node_version: process.version, model_source: role.model_source, extension: role.extension,
      request_parameters: { max_tokens, reasoning, session_id: sessionId, timeout },
      parameters: { ...parameters, timeout, attempt: number }, stop_reason,
    },
  };
  return { call, message: response };
}

export type Call = Awaited<ReturnType<typeof attempt>>["call"];
const ended = new Error("attempt ended without a message");

// Transient provider failures restart the request inside this call's timeout, at most twice and only while the
// budget pays for it; aborts and other errors never retry. Every attempt reaches `record` before the next starts.
export async function complete(request: Invocation, record?: (call: Call) => Promise<void>, budget?: RetryBudget) {
  const { pi, options, timeoutMs, abort } = request;
  const maxRetries = Math.min(2, budget?.available() ?? 0);
  const policy = { enabled: maxRetries > 0, maxRetries, baseDelayMs: 1000, maxAgentDelayMs: 15_000 };
  let attempts = 0;
  let last: Call | undefined;
  const timer = setTimeout(abort, timeoutMs);
  try {
    await pi.ai.retryAssistantCall(async () => {
      const { call, message } = await attempt(request, ++attempts, timeoutMs / 1000);
      last = call;
      await record?.(call);
      return message ?? Promise.reject(ended);
    }, policy, options.signal, { onRetryAttemptStart: () => budget?.consume() });
  } catch (error) {
    if (error !== ended) throw error;
  } finally {
    clearTimeout(timer);
  }
  return last!;
}

type AuditPayload = Call & {
  messages: TranscriptContext["messages"];
  tools: ReturnType<Pi["ai"]["getCurrentTools"]>;
};

// The runner is headless: an attempt may fail mid-stream and be retried, so the agent only sees the final message,
// and only after the worker acknowledges every attempt against this exact transcript.
export function auditedStream(request: Invocation, record: (payload: AuditPayload) => Promise<void>,
                             fail: (error: unknown) => void, budget: RetryBudget,
                             onTerminalError?: (kind: ErrorKind) => Promise<void>) {
  const { pi, context, role, options } = request;
  const messages = structuredClone(context.messages);
  const tools = pi.ai.getCurrentTools(messages);
  const output = pi.ai.createAssistantMessageEventStream();
  async function deliver() {
    const call = await complete(request, each => record({ ...each, messages, tools }), budget);
    const kind: ErrorKind | null = options.signal?.aborted ? "timeout" : call.error_kind;
    if (!call.response || kind) {
      const resolved = kind ?? "invalid_output";
      if (resolved !== call.error_kind) await onTerminalError?.(resolved);
      throw new RunnerError(3, resolved);
    }
    output.push({ type: "start", partial: call.response });
    output.push({ type: "done", reason: call.response.stopReason as "stop" | "toolUse", message: call.response });
  }
  void deliver().catch(error => {
    fail(error);
    const message: AssistantMessage = {
      role: "assistant", content: [], api: role.model.api, provider: role.model.provider, model: role.model.id,
      usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
      stopReason: "error", timestamp: Date.now(),
    };
    output.push({ type: "error", reason: "error", error: message });
  });
  return output;
}

export function sessionId(systemPrompt: string, tools: unknown[]) {
  return sha256(JSON.stringify({ system: systemPrompt, tools })).slice(0, 32);
}
