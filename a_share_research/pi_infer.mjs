// One no-tool completion through pi's provider runtime: no agent session, tools, skills or context files.
// Ported from a-stock-tracker scripts/automation_model_pi.mjs.
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const request = JSON.parse(await readFile(process.argv[2], "utf8"));
process.env.PI_OFFLINE = "1"; // use the installed catalog, never discover extra models
const base = process.env.PI_RUNTIME;
const load = name => import(pathToFileURL(join(base, "dist/core", name)).href);
const { ModelRuntime } = await load("model-runtime.js");
const runtime = await ModelRuntime.create({
  authPath: join(process.env.PI_AGENT_DIR, "auth.json"),
  modelsPath: null,
  allowModelNetwork: false,
  refreshOnCreate: false,
});
let model = runtime.getModel(request.provider, request.model);
if (!model && process.env.PI_EXTENSION) {
  const { loadExtensions } = await load("extensions/loader.js");
  const loaded = await loadExtensions([process.env.PI_EXTENSION], base);
  if (loaded.errors.length || loaded.extensions.some(e => e.tools.size || e.handlers.size)) {
    throw new Error("Provider extension must not register tools or event handlers");
  }
  for (const entry of loaded.runtime.pendingProviderRegistrations) {
    if (entry.name === request.provider) runtime.registerProvider(entry.name, entry.config);
  }
  model = runtime.getModel(request.provider, request.model);
}
if (!model) throw new Error("Configured pi model not found");
const response = await runtime.completeSimple(model, {
  messages: [{ role: "user", content: request.prompt, timestamp: Date.now() }],
  tools: [],
}, {
  maxTokens: request.max_tokens,
  transport: "sse",
  maxRetries: 0,
  reasoning: "medium",
  sessionId: request.session_id, // pi only sends prompt_cache_key with a sessionId
  signal: AbortSignal.timeout(request.timeout * 1000),
});
const text = response.content.filter(c => c.type === "text").map(c => c.text).join("");
console.log(JSON.stringify({
  text,
  stop_reason: response.stopReason,
  tool_calls: response.content.filter(c => c.type === "toolCall").length,
  provider: response.provider,
  model: response.model,
  usage: response.usage,
}));
