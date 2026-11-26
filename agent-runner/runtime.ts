import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import type * as Core from "@earendil-works/pi-agent-core";
import type * as Ai from "@earendil-works/pi-ai";
import type * as Mcp from "@earendil-works/pi-mcp";
import type * as CodingAgent from "@earendil-works/pi-coding-agent";
import type * as Schema from "@earendil-works/pi-schema";
import { RunnerError, sha256 } from "./profile.ts";
import type { ModelConfig, Profile, Role } from "./profile.ts";

const load = (path: string) => import(pathToFileURL(path).href);

export async function loadPi(agentDir = process.env.PI_AGENT_DIR || join(homedir(), ".pi/agent")) {
  process.env.PI_OFFLINE = "1";
  let base = process.env.PI_RUNTIME;
  if (!base) {
    const install = join(agentDir, "install");
    const version = (await readFile(join(install, "current-version"), "utf8")).trim();
    if (!version || /[/\\]/.test(version) || version === "..") throw new RunnerError(2, "Invalid pi current-version");
    base = join(install, "releases", version, "node_modules/@earendil-works/pi-coding-agent");
  }
  base = resolve(base);
  const sibling = (name: string) => load(join(dirname(base), name, "dist/index.js"));
  const codingAgent = await load(join(base, "dist/index.js")) as typeof CodingAgent;
  return {
    codingAgent,
    core: await sibling("pi-agent-core") as typeof Core,
    ai: await sibling("pi-ai") as typeof Ai,
    mcp: await sibling("pi-mcp") as typeof Mcp,
    schema: await sibling("pi-schema") as typeof Schema,
    version: codingAgent.VERSION,
    // Internal modules without a public export: the catalog file store, in-memory auth and the explicit extension loader.
    coding: (file: string) => load(join(base, "dist/core", file)),
    base, agentDir,
  };
}
export type Pi = Awaited<ReturnType<typeof loadPi>>;

async function readStore<T>(path: string): Promise<Record<string, T>> {
  let text;
  try {
    text = await readFile(path, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
    throw error;
  }
  const value = text ? JSON.parse(text.replace(/^\uFEFF/, "")) : {};
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new RunnerError(2, "Invalid pi JSON store");
  return value;
}

// ModelRuntime ignores modelsStorePath when modelsPath is null, so runs pass the locked file store explicitly:
// the host pi shares this agent dir and may rewrite the catalog cache while a run starts.
export async function createRuntime(pi: Pi, check: boolean): Promise<CodingAgent.ModelRuntime> {
  const storePath = join(pi.agentDir, "models-store.json");
  const authPath = join(pi.agentDir, "auth.json");
  const options = { modelsPath: null, refreshOnCreate: false, allowModelNetwork: false };
  if (!check) {
    const { FileModelsStore } = await pi.coding("models-store.js");
    return pi.codingAgent.ModelRuntime.create({ ...options, authPath, modelsStore: new FileModelsStore(storePath) });
  }
  // --check reads both stores into memory so it never creates auth.json, the catalog cache or their lock files.
  const modelsStore = new pi.ai.InMemoryModelsStore();
  for (const [provider, entry] of Object.entries(await readStore<Ai.ModelsStoreEntry>(storePath))) await modelsStore.write(provider, entry);
  const { AuthStorage } = await pi.coding("auth-storage.js");
  return pi.codingAgent.ModelRuntime.create({
    ...options, modelsStore, credentials: AuthStorage.inMemory(await readStore(authPath)),
  });
}

export interface ResolvedRole {
  config: ModelConfig;
  model: Ai.Model<Ai.Api>;
  model_source: "catalog" | "extension";
  extension: { path: string; sha256: string } | null;
}
export type Models = Record<Role, ResolvedRole>;

export async function resolveModels(pi: Pi, runtime: CodingAgent.ModelRuntime, profile: Profile, check: boolean): Promise<Models> {
  const models = {} as Models;
  const extensions = new Map<string, NonNullable<ResolvedRole["extension"]>>();
  for (const role of ["author", "reviewer"] as const) {
    const config = profile.roles[role];
    const { provider } = config;
    const signal = AbortSignal.timeout(check ? 20_000 : profile.limits.model_call_seconds * 1000);
    async function refresh() {
      const result = await runtime.refresh({ providers: [provider], allowNetwork: false, signal });
      if (result.aborted || result.errors.has(provider)) throw new RunnerError(2, `Cannot refresh ${role} model catalog`);
    }
    await refresh();
    let model = runtime.getModel(provider, config.model);
    if (!model && process.env.PI_EXTENSION) {
      const { loadExtensions } = await pi.coding("extensions/loader.js");
      const loaded = await loadExtensions([process.env.PI_EXTENSION], pi.base);
      if (loaded.errors.length || loaded.extensions.some((ext: { tools: Map<string, unknown> }) => ext.tools.size)) {
        throw new RunnerError(2, "Cannot load PI_EXTENSION as a provider-only extension");
      }
      for (const entry of loaded.runtime.pendingProviderRegistrations) {
        if (entry.name !== provider) continue;
        runtime.registerProvider(provider, entry.config);
        const path = loaded.extensions[0].resolvedPath;
        extensions.set(provider, { path, sha256: sha256(await readFile(path)) });
      }
      await refresh();
      model = runtime.getModel(provider, config.model);
    }
    if (!model) throw new RunnerError(2, `Cannot resolve roles.${role}.model in the pi catalog or PI_EXTENSION`);
    if (check && !await runtime.checkAuth(provider, { signal })) throw new RunnerError(2, `No configured credentials for ${role}`);
    const extension = extensions.get(provider) ?? null;
    models[role] = { config, model, model_source: extension ? "extension" : "catalog", extension };
  }
  return models;
}
