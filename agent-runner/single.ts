import { createInterface } from "node:readline";
import type { Readable, Writable } from "node:stream";
import type { StreamFn } from "@earendil-works/pi-agent-core";
import { complete, sessionId } from "./calls.ts";
import { comparisons, RunnerError, sha256 } from "./profile.ts";
import type { Profile, Comparison } from "./profile.ts";
import type { Pi, Models } from "./runtime.ts";

export async function runSingle(input: Readable, output: Writable, pi: Pi, models: Models, profile: Profile, stream: StreamFn) {
  const attempts = new Map<Comparison, number>();
  for await (const line of createInterface({ input, crlfDelay: Infinity })) {
    const { task, attempt, prompt } = JSON.parse(line);
    if (!comparisons.includes(task) || !Number.isSafeInteger(attempt) || attempt < 1 || typeof prompt !== "string") {
      throw new RunnerError(2, "Expected a comparison task, positive attempt and prompt");
    }
    const limit = profile.limits.comparison_attempts_per_task;
    const count = (attempts.get(task) ?? 0) + 1;
    if (count > limit || attempt > limit) throw new RunnerError(4, "Comparison attempt limit reached");
    attempts.set(task, count);
    const controller = new AbortController();
    const result = await complete({
      pi, stream, role: models[profile.role_for[task as Comparison]], single: true,
      context: pi.ai.normalizeContext({ messages: [{ role: "user", content: prompt, timestamp: Date.now() }], tools: [] }),
      options: { sessionId: sessionId(prompt), signal: controller.signal },
      timeoutMs: profile.limits.model_call_seconds * 1000, abort: () => controller.abort(),
    });
    const { response, error_kind, usage, seconds } = result;
    const identity = { ...result.identity, prompt_sha256: sha256(prompt) };
    const completion = response
      ? { text: response.content.filter(b => b.type === "text").map(b => b.text).join("") }
      : { error_kind };
    output.write(JSON.stringify({ task, attempt, ...completion, identity, usage, seconds }) + "\n");
  }
}
