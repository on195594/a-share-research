// One no-tool completion through pi's provider runtime: no agent session, tools, skills or context files.
// Ported from a-stock-tracker scripts/automation_model_pi.mjs.
import { readFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const request = JSON.parse(await readFile(process.argv[2], "utf8"));
const result = { pi_version: null, parameters: null, node_version: process.version,
  model_source: null, extension: null };
try {
  process.env.PI_OFFLINE = "1"; // use the installed catalog, never discover extra models
  // Managed pi installs swap releases on upgrade; follow current-version unless PI_RUNTIME pins one.
  const install = join(process.env.PI_AGENT_DIR, "install");
  const base = process.env.PI_RUNTIME || join(install, "releases",
    (await readFile(join(install, "current-version"), "utf8")).trim(), "node_modules/@earendil-works/pi-coding-agent");
  result.pi_version = JSON.parse(await readFile(join(base, "package.json"), "utf8")).version;
  const load = name => import(pathToFileURL(join(base, "dist/core", name)).href);
  const { ModelRuntime } = await load("model-runtime.js");
  const runtime = await ModelRuntime.create({
    authPath: join(process.env.PI_AGENT_DIR, "auth.json"),
    modelsPath: null,
    allowModelNetwork: false,
    refreshOnCreate: false,
  });
  let model = runtime.getModel(request.provider, request.model);
  if (model) result.model_source = "catalog";
  if (!model && process.env.PI_EXTENSION) {
    const { loadExtensions } = await load("extensions/loader.js");
    const loaded = await loadExtensions([process.env.PI_EXTENSION], base);
    const extension = loaded.extensions[0];
    if (extension) {
      result.extension = { path: extension.resolvedPath,
        sha256: createHash("sha256").update(await readFile(extension.resolvedPath)).digest("hex") };
    }
    if (loaded.errors.length) {
      throw new Error(`Provider extension failed to load: ${loaded.errors[0].error}`);
    }
    if (loaded.extensions.some(e => e.tools.size || e.handlers.size)) {
      throw new Error("Provider extension must not register tools or event handlers");
    }
    for (const entry of loaded.runtime.pendingProviderRegistrations) {
      if (entry.name === request.provider) runtime.registerProvider(entry.name, entry.config);
    }
    model = runtime.getModel(request.provider, request.model);
    if (model) result.model_source = "extension";
  }
  if (!model) throw new Error("Configured pi model not found");
  result.parameters = {
    maxTokens: request.max_tokens,
    transport: "sse",
    maxRetries: 0,
    reasoning: "medium",
    sessionId: request.session_id, // pi only sends prompt_cache_key with a sessionId
    timeout: request.timeout,
  };
  const { timeout, ...options } = result.parameters;
  const response = await runtime.completeSimple(model, {
    messages: [{ role: "user", content: request.prompt, timestamp: Date.now() }],
    tools: [],
  }, {
    ...options,
    signal: AbortSignal.timeout(timeout * 1000),
  });
  const text = response.content.filter(c => c.type === "text").map(c => c.text).join("");
  Object.assign(result, {
    text,
    stop_reason: response.stopReason,
    tool_calls: response.content.filter(c => c.type === "toolCall").length,
    provider: response.provider,
    model: response.model,
    usage: response.usage,
  });
} catch (error) {
  result.error = `${error.name}: ${error.message}`;
  process.exitCode = 1;
}
console.log(JSON.stringify(result));
