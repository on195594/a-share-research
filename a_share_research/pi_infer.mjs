// One native completion; the Python worker owns tool execution and conversation state.
// Ported from a-stock-tracker scripts/automation_model_pi.mjs.
import { readFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const preflight = process.argv[2] === "--check";
const request = preflight
  ? { provider: process.env.MODEL_PROVIDER, model: process.env.MODEL_ID, timeout: 20 }
  : JSON.parse(await readFile(process.argv[2], "utf8"));
const result = { pi_version: null, parameters: null, node_version: process.version,
  model_source: null, extension: null };

async function readStore(path) {
  let content;
  try {
    content = await readFile(path, "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return {}; // Pi also accepts built-in catalogs and environment credentials.
    throw new Error(`Cannot read ${path}; restore access: ${error.message}`);
  }
  try {
    const data = content ? JSON.parse(content.replace(/^\uFEFF/, "")) : {};
    if (!data || typeof data !== "object" || Array.isArray(data)) throw new TypeError("Expected a JSON object");
    return data;
  } catch (error) {
    // JSON parse errors may echo credential text. Keep the error type and location, never that text.
    const location = error.message.match(/position \d+(?: \(line \d+ column \d+\))?/);
    throw new Error(`Invalid ${path}; restore valid JSON object: ${error.name}${location ? ` at ${location[0]}` : ""}`);
  }
}

try {
  if (!request.provider || !request.model) throw new Error("Set MODEL_PROVIDER and MODEL_ID in the worker environment");
  if (!process.env.PI_AGENT_DIR) throw new Error("Set PI_AGENT_DIR to the existing pi agent directory");
  process.env.PI_OFFLINE = "1"; // use the installed catalog, never discover extra models
  // Managed pi installs swap releases on upgrade; follow current-version unless PI_RUNTIME pins one.
  const install = join(process.env.PI_AGENT_DIR, "install");
  let base = process.env.PI_RUNTIME;
  if (!base) {
    let release;
    try {
      release = (await readFile(join(install, "current-version"), "utf8")).trim();
    } catch (error) {
      throw new Error(`Pi install/current-version is unreadable at ${install}; restore the managed release or set PI_RUNTIME: ${error.message}`);
    }
    if (!release || release.includes("/") || release.includes("\\") || release === "..") {
      throw new Error("Invalid pi install/current-version; restore the managed release or set PI_RUNTIME");
    }
    base = join(install, "releases", release, "node_modules/@earendil-works/pi-coding-agent");
  }
  try {
    result.pi_version = JSON.parse(await readFile(join(base, "package.json"), "utf8")).version;
    if (!result.pi_version) throw new Error("Missing version");
  } catch (error) {
    throw new Error(`Pi runtime unavailable at ${base}; restore the managed release or correct PI_RUNTIME: ${error.message}`);
  }
  const storePath = join(process.env.PI_AGENT_DIR, "models-store.json");
  const store = await readStore(storePath);
  const load = async name => {
    try {
      return await import(pathToFileURL(join(base, "dist/core", name)).href);
    } catch (error) {
      throw new Error(`Pi module ${name} could not load; check Node compatibility and restore the runtime at ${base}: ${error.message}`);
    }
  };
  const { ModelRuntime } = await load("model-runtime.js");
  const { FileModelsStore, InMemoryCodingAgentModelsStore } = await load("models-store.js");
  let credentials;
  const modelsStore = preflight ? new InMemoryCodingAgentModelsStore() : new FileModelsStore(storePath);
  if (preflight) {
    // A health check must not create lock files or refresh persisted credentials/catalogs.
    const { AuthStorage } = await load("auth-storage.js");
    const auth = await readStore(join(process.env.PI_AGENT_DIR, "auth.json"));
    credentials = AuthStorage.inMemory(auth);
    for (const [provider, entry] of Object.entries(store)) await modelsStore.write(provider, entry);
  }
  const runtime = await ModelRuntime.create({
    credentials,
    authPath: join(process.env.PI_AGENT_DIR, "auth.json"),
    modelsPath: null,
    modelsStore,
    allowModelNetwork: false,
    refreshOnCreate: false,
  });
  const refreshed = await runtime.refresh({ providers: [request.provider], allowNetwork: false,
    signal: AbortSignal.timeout(request.timeout * 1000) });
  if (refreshed.aborted) throw new Error("Model catalog refresh timed out");
  if (refreshed.errors.has(request.provider)) throw refreshed.errors.get(request.provider);
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
  if (!model) throw new Error("Configured pi model not found; check MODEL_PROVIDER, MODEL_ID, models-store.json and PI_EXTENSION");
  if (preflight) {
    await runtime.refreshProviderAvailability(request.provider, AbortSignal.timeout(request.timeout * 1000));
    if (!runtime.hasConfiguredAuth(request.provider)) {
      throw new Error(`Provider ${request.provider} has no configured credentials; check auth.json or the provider's environment credentials`);
    }
    Object.assign(result, { ready: true, provider: model.provider, model: model.id });
  } else {
    result.parameters = {
      maxTokens: request.max_tokens,
      transport: "sse",
      maxRetries: 0,
      reasoning: "high",
      sessionId: request.session_id, // pi only sends prompt_cache_key with a sessionId
      timeout: request.timeout,
    };
    const { timeout, ...options } = result.parameters;
    const response = await runtime.completeSimple(model, {
      messages: request.messages || [{ role: "user", content: request.prompt, timestamp: Date.now() }],
      tools: request.tools || [],
    }, {
      ...options,
      signal: AbortSignal.timeout(timeout * 1000),
    });
    const text = response.content.filter(c => c.type === "text").map(c => c.text).join("");
    Object.assign(result, {
      text,
      stop_reason: response.stopReason,
      error: response.stopReason === "error" ? (response.errorMessage || "pi model returned an error without details") : undefined,
      tool_calls: response.content.filter(c => c.type === "toolCall").length,
      provider: response.provider,
      model: response.model,
      usage: response.usage,
      message: response,
    });
  }
} catch (error) {
  result.error = `${error.name}: ${error.message}`;
  process.exitCode = 1;
}
console.log(JSON.stringify(result));
