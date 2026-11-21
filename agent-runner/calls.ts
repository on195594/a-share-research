import type { StreamFn } from "@earendil-works/pi-agent-core";
import type {
  AssistantMessage, AssistantMessageEvent, SimpleStreamOptions, TranscriptContext,
} from "@earendil-works/pi-ai";
import { RunnerError, sha256 } from "./profile.ts";
import type { Pi, ResolvedRole } from "./runtime.ts";

export type ErrorKind = "timeout" | "provider_error" | "pi_runtime" | "incomplete:length" | "incomplete"
  | "empty_output" | "invalid_output";

function failure(message: AssistantMessage, role: ResolvedRole, single: boolean): ErrorKind | null {
  const stop = message.stopReason;
  if (stop === "aborted") return "timeout";
  if (stop === "error") return "provider_error";
  if (stop === "length") return "incomplete:length";
  if (stop !== "stop" && stop !== "toolUse") return "incomplete";
  if (message.provider !== role.config.provider || message.model !== role.config.model) return "invalid_output";
  const hasTools = message.content.some(block => block.type === "toolCall");
  const hasText = message.content.some(block => block.type === "text" && block.text.trim());
  if (!hasTools && !hasText) return "empty_output";
  if (hasTools !== (stop === "toolUse") || (single && hasTools)) return "invalid_output";
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
  single?: boolean;
  partial?: (event: AssistantMessageEvent) => void;
}

// Only the reviewed fields cross the audit boundary. Provider error text may contain credentials.
export async function complete({ pi, stream, role, context, options, timeoutMs, abort, single, partial }: Invocation) {
  const { provider, model, reasoning, max_tokens } = role.config;
  const sessionId = options.sessionId!;
  const parameters = { maxTokens: max_tokens, reasoning, transport: "sse" as const, maxRetries: 0, sessionId };
  let response: AssistantMessage | null = null;
  let error_kind: ErrorKind | null;
  const started = performance.now();
  const timer = setTimeout(abort, timeoutMs);
  try {
    const events = await stream(role.model, context, {
      ...options, ...parameters, reasoning: reasoning === "off" ? undefined : reasoning,
    });
    for await (const event of events) {
      if (event.type === "done" || event.type === "error") break;
      partial?.(event);
    }
    response = await events.result();
    error_kind = options.signal?.aborted ? "timeout" : failure(response, role, !!single);
  } catch {
    error_kind = options.signal?.aborted ? "timeout" : "pi_runtime";
  } finally {
    clearTimeout(timer);
  }
  const stop_reason = response?.stopReason ?? null;
  const timeout = timeoutMs / 1000;
  return {
    response: error_kind ? null : response, error_kind, stop_reason,
    usage: response?.usage ?? null, seconds: (performance.now() - started) / 1000,
    identity: {
      requested: { provider, model },
      actual: response?.provider === provider && response.model === model ? { provider, model } : null,
      pi_version: pi.version, node_version: process.version, model_source: role.model_source, extension: role.extension,
      request_parameters: { max_tokens, reasoning, session_id: sessionId, timeout },
      parameters: { ...parameters, timeout }, stop_reason,
    },
  };
}

type AuditPayload = Awaited<ReturnType<typeof complete>> & {
  messages: TranscriptContext["messages"];
  tools: ReturnType<Pi["ai"]["getCurrentTools"]>;
};

// Hold the terminal event until the worker acknowledges this exact transcript.
export function auditedStream(request: Invocation, record: (payload: AuditPayload) => Promise<void>,
                             fail: (error: unknown) => void) {
  const { pi, context, role } = request;
  const messages = structuredClone(context.messages);
  const tools = pi.ai.getCurrentTools(messages);
  const output = pi.ai.createAssistantMessageEventStream();
  async function deliver() {
    const call = await complete({ ...request, partial: event => output.push(event) });
    await record({ ...call, messages, tools });
    if (!call.response || call.error_kind) throw new RunnerError(3, call.error_kind ?? "invalid_output");
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

export function sessionId(prompt: string) {
  return sha256(Array.from(prompt.slice(0, 8192)).slice(0, 4096).join("")).slice(0, 32);
}
