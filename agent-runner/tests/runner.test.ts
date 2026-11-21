import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, open, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { PassThrough, Readable } from "node:stream";
import { after, test } from "node:test";
import { setImmediate } from "node:timers/promises";
import type { StreamFn } from "@earendil-works/pi-agent-core";
import type { AssistantMessage, JsonObject, Message } from "@earendil-works/pi-ai";
import { withWorker } from "../main.ts";
import { readProfile, sha256 } from "../profile.ts";
import { runResearch } from "../research.ts";
import { createRuntime, loadPi, resolveModels } from "../runtime.ts";
import { runSingle } from "../single.ts";

const pi = await loadPi();
const dir = await mkdtemp(join(tmpdir(), "agent-runner-"));
after(() => rm(dir, { recursive: true, force: true }));
const profilePath = join(dir, "profile.json");
const config = { provider: "openai", model: "gpt-4o", reasoning: "medium", max_tokens: 4000 };
await writeFile(profilePath, JSON.stringify({
  roles: { author: config, reviewer: { ...config, model: "gpt-4o-mini" } },
  role_for: { explore: "author", compose: "author", repair: "author", review: "reviewer",
    q20_select: "author", q20_rank: "author", s10_select: "author" },
  limits: { model_calls: 30, monthly_model_calls: 21, run_seconds: 60, model_call_seconds: 10,
    acquisition_seconds: 45, acquisition_rounds: 24, fetch_calls: 48, explore_tool_calls: 120,
    repair_extra_tool_calls: 48, draft_call_reserve: 4, no_progress_rounds: 2, review_rounds: 3,
    review_format_corrections: 1, full_rewrites_per_invalid_patch: 1, comparison_attempts_per_task: 2,
    monthly_batch_model_calls: 636 },
}));
const profile = await readProfile(profilePath);
const runtime = await createRuntime({ ...pi, agentDir: dir }, true);
const models = await resolveModels(pi, runtime, profile, false);
after(() => runtime.settleCredentialWrites());
const usage = { input: 7, output: 3, totalTokens: 10, cacheRead: 0, cacheWrite: 0,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
const controls = ["submit_draft", "submit_patch", "review_decide", "record_call"];
const evidence = ["calculate", "macro", "peers"];
const review = { status: "review", binding: { version: 1 }, draft: { title: "bound draft" },
  material_block: "review material", program_valuation: "program value" };
const repair = { status: "repair", problems: ["resolve this issue"], indexed_draft: { "answer[0]": "original" },
  material_block: "refreshed material", program_valuation: "program value" };
const done = { status: "complete", problems: [] };
const rejected = { error: "field rejected" };

interface Request { model: string; messages: Message[]; tools: string[] }
type Turn = string[] | { text: string } | { error: string } | { waitForAbort: true };

function fakeStream(turns: Turn[], requests: Request[]): StreamFn {
  return (model, context, options) => {
    const turn = turns[requests.length];
    requests.push({ model: model.id, messages: structuredClone(context.messages),
      tools: pi.ai.getCurrentTools(context.messages).map(t => t.name).sort() });
    if (!turn) throw Error("Unexpected extra model request");
    const error = !Array.isArray(turn) && "error" in turn;
    const content: AssistantMessage["content"] = Array.isArray(turn)
      ? turn.map((name, i) => ({ type: "toolCall", name, arguments: {}, id: `c${requests.length}-${i}` }))
      : "text" in turn ? [{ type: "text", text: turn.text }] : [];
    const message: AssistantMessage = { role: "assistant", api: model.api, provider: model.provider, model: model.id,
      content, usage, stopReason: error ? "error" : Array.isArray(turn) ? "toolUse" : "stop", timestamp: Date.now() };
    const events = pi.ai.createAssistantMessageEventStream();
    if ("waitForAbort" in turn) {
      options!.signal!.addEventListener("abort", () => {
        message.stopReason = "aborted";
        events.push({ type: "error", reason: "aborted", error: message });
      }, { once: true });
    } else if (error) {
      message.errorMessage = turn.error;
      events.push({ type: "error", reason: "error", error: message });
    } else {
      events.push({ type: "start", partial: message });
      events.push({ type: "done", reason: Array.isArray(turn) ? "toolUse" : "stop", message });
    }
    return events;
  };
}

function worker(turns: Turn[], reply: (name: string) => object, options: { replay?: boolean; auditError?: boolean; budget?: number; remainingMs?: number } = {}) {
  const input = new PassThrough(), output = new PassThrough();
  const events: string[] = [], requests: Request[] = [], audits: JsonObject[] = [];
  const server = (async () => {
    for await (const line of createInterface({ input: output })) {
      const { id, method, params } = JSON.parse(line);
      if (id === undefined) continue;
      let result;
      if (method === "initialize") {
        result = { protocolVersion: params.protocolVersion, capabilities: { tools: {}, resources: {} },
          serverInfo: { name: "test-worker", version: "1" } };
      } else if (method === "resources/read") {
        assert.equal(params.uri, "research://run/context");
        result = { contents: [{ uri: params.uri, text: JSON.stringify({ run_id: 1, question: "initial question",
          rules: "test rules", output_schema: "test report fields", material_block: "initial material",
          remaining_ms: options.remainingMs ?? 60_000, effective_model_call_limit: options.budget ?? 30, mode: options.replay ? "replay" : "run",
          profile_sha256: profile.sha256 }) }] };
      } else if (method === "tools/list") {
        result = { tools: [...controls, ...(options.replay ? [] : evidence)].map(name =>
          ({ name, description: name, inputSchema: { type: "object", properties: {} } })) };
      } else if (method === "research/acquisition_stop") {
        events.push(params.reason);
        result = {};
      } else {
        assert.equal(method, "tools/call");
        const name = params.name;
        events.push(name);
        let data;
        if (name === "record_call") {
          await setImmediate();
          audits.push(params.arguments);
          events.push("audit settled");
          data = options.auditError ? rejected : { call_id: audits.length };
        } else {
          data = reply(name);
        }
        result = { content: [{ type: "text", text: JSON.stringify(data) }], structuredContent: data, isError: "error" in data };
      }
      input.write(JSON.stringify({ jsonrpc: "2.0", id, result }) + "\n");
    }
    events.push("closed");
  })();
  async function run() {
    try {
      await withWorker(pi, input, output, client => runResearch(client, pi, models, profile, fakeStream(turns, requests), 1));
    } finally {
      await server;
    }
  }
  return { events, requests, audits, run };
}

test("whole evidence rounds change the tools declared to the model and executed by the worker", async () => {
  let reads = 0;
  const w = worker([["macro", "calculate"], ["macro", "calculate"], ["macro", "calculate"],
    ["macro", "submit_draft"], ["review_decide"]], name => {
    if (name === "submit_draft") return review;
    if (name === "review_decide") return done;
    return { result: "evidence", charged: true, evidence_delta: ++reads === 2 ? 1 : 0 };
  });
  await w.run();
  const full = [...evidence, "submit_draft"].sort();
  assert.deepEqual(w.requests.map(r => r.tools), [full, full, full, ["submit_draft"], ["review_decide"]]);
  assert.equal(reads, 6);
  assert.equal(w.events.filter(e => e === "no_new_evidence").length, 1);
  for (const [i, audit] of w.audits.entries()) {
    assert.deepEqual(audit.messages, JSON.parse(JSON.stringify(w.requests[i].messages)));
    assert.deepEqual((audit.tools as JsonObject[]).map(t => t.name).sort(), w.requests[i].tools);
    assert.deepEqual(audit.usage, usage);
  }
});

test("nested review seals mixed batches; repair and the next review start with fresh material", async () => {
  let decisions = 0;
  const w = worker([
    ["peers"], ["calculate", "submit_draft", "macro"], ["review_decide"], ["review_decide", "review_decide"],
    ["submit_patch"], ["submit_draft", "calculate"], ["review_decide"],
  ], name => {
    if (name === "submit_draft") return review;
    if (name === "submit_patch") return rejected;
    if (name === "review_decide") return [rejected, repair, done][decisions++];
    return { result: "author-only tool output", evidence_delta: 1, charged: true };
  });
  await w.run();
  const full = [...evidence, "submit_draft"].sort();
  assert.deepEqual(w.requests.map(r => r.tools), [full, full, ["review_decide"], ["review_decide"],
    ["calculate", "macro", "submit_patch"], ["calculate", "macro", "submit_draft"], ["review_decide"]]);
  assert.deepEqual(w.audits.map(a => a.step), ["explore", "explore", "review", "review", "repair", "repair", "review"]);
  assert.deepEqual(w.requests.map(r => r.model), w.audits.map(a => a.step === "review" ? "gpt-4o-mini" : "gpt-4o"));
  for (const i of [2, 4, 6]) {
    const messages = w.requests[i].messages;
    assert.deepEqual(messages.map(m => m.role), ["system", "user"]);
    assert.doesNotMatch(JSON.stringify(messages), /author-only tool output|initial material/);
  }
  const repairInput = JSON.stringify(w.requests[4].messages);
  for (const text of ["resolve this issue", "answer[0]", "refreshed material", "program value"]) assert.ok(repairInput.includes(text));
  assert.deepEqual(w.events.filter(x => !["record_call", "audit settled"].includes(x)),
    ["peers", "calculate", "submit_draft", "review_decide", "review_decide", "submit_patch", "submit_draft", "review_decide", "closed"]);
  assert.deepEqual(w.events.slice(-4), ["record_call", "audit settled", "review_decide", "closed"]);
});

test("replay submits the frozen draft and stops after the configured review correction", async () => {
  const w = worker([["submit_draft"], ["review_decide"], ["review_decide"]], name => name === "submit_draft" ? review : rejected, { replay: true });
  await w.run();
  assert.deepEqual(w.audits.map(a => a.step), ["compose", "review", "review"]);
  assert.deepEqual(w.requests.map(r => r.tools), [["submit_draft"], ["review_decide"], ["review_decide"]]);
});

test("audit rejection and provider errors end the process path before any tool side effect", async () => {
  const audit = worker([["submit_draft"]], () => done, { auditError: true });
  await assert.rejects(audit.run, { code: 1 });
  assert.deepEqual(audit.events, ["record_call", "audit settled", "closed"]);
  const provider = worker([{ error: "private vendor detail" }], () => done);
  await assert.rejects(provider.run, { code: 3, message: "provider_error" });
  assert.deepEqual(provider.events, audit.events);
  assert.equal(provider.audits[0].error_kind, "provider_error");
  assert.equal(provider.audits[0].response, null);
  assert.doesNotMatch(JSON.stringify(provider.audits), /private vendor detail/);
});

test("the run deadline aborts the active model and records its final usage", async () => {
  const w = worker([{ waitForAbort: true }], () => done, { remainingMs: 80 });
  await assert.rejects(w.run, { code: 3, message: "timeout" });
  assert.equal(w.audits[0].stop_reason, "aborted");
  assert.deepEqual(w.audits[0].usage, usage);
  assert.deepEqual(w.events, ["record_call", "audit settled", "closed"]);
});

test("the effective budget reserves submission and distinguishes an unreviewed draft from a failed check", async () => {
  const insufficient = worker([["submit_draft"]], () => review, { budget: 1 });
  await assert.rejects(insufficient.run, { code: 4 });
  assert.deepEqual(insufficient.requests.map(r => r.tools), [["submit_draft"]]);
  const mechanical = worker([["submit_draft"]], () => repair, { budget: 1 });
  await mechanical.run();
  assert.deepEqual(mechanical.requests.map(r => r.tools), [["submit_draft"]]);
});

test("single returns one completion or closed error per line with fresh, tool-free requests", async () => {
  const lines = [{ task: "q20_rank", attempt: 1, prompt: "first" }, { task: "s10_select", attempt: 1, prompt: "second" }];
  const output = new PassThrough();
  const requests: Request[] = [];
  await runSingle(Readable.from(lines.map(line => JSON.stringify(line) + "\n")), output, pi, models, profile,
    fakeStream([{ error: "private upstream text" }, { text: "selection" }], requests));
  const [rank, select] = output.read().toString().trim().split("\n").map(JSON.parse);
  assert.equal(rank.error_kind, "provider_error");
  assert.equal(select.text, "selection");
  assert.equal(select.identity.prompt_sha256, sha256("second"));
  assert.deepEqual(select.usage, usage);
  assert.deepEqual(requests.map(r => [r.tools, r.messages.length]), [[[], 1], [[], 1]]);
});

test("offline CLI preflights both roles without writing the auth or model store", async () => {
  const extension = join(dir, "provider.mjs");
  await writeFile(extension, `export default pi => pi.registerProvider("fixture", {
    api: "openai-completions", baseUrl: "https://fixture.invalid", apiKey: "OFFLINE_KEY",
    models: [{ id: "offline-review", name: "Offline review", reasoning: false, input: ["text"],
      contextWindow: 4096, maxTokens: 512, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
  });`);
  const checkProfile = join(dir, "check-profile.json");
  const checkConfig = JSON.parse(await readFile(profilePath, "utf8"));
  checkConfig.roles.reviewer = { ...config, provider: "fixture", model: "offline-review" };
  await writeFile(checkProfile, JSON.stringify(checkConfig));
  const stdout = await open(join(dir, "check.stdout"), "w");
  const stderr = await open(join(dir, "check.stderr"), "w");
  try {
    const check = spawnSync(process.execPath, ["agent-runner/main.ts", "--check", "--profile", checkProfile], {
      stdio: ["ignore", stdout.fd, stderr.fd],
      env: { PATH: process.env.PATH, PI_RUNTIME: pi.base, PI_AGENT_DIR: dir, PI_EXTENSION: extension,
        OPENAI_API_KEY: "offline-test", OFFLINE_KEY: "offline-test" },
    });
    assert.ifError(check.error);
    assert.equal(check.status, 0, await readFile(join(dir, "check.stderr"), "utf8"));
    const result = JSON.parse(await readFile(join(dir, "check.stdout"), "utf8"));
    assert.equal(result.ready, true);
    assert.deepEqual([result.model, result.review.model], [models.author.model.id, "offline-review"]);
    assert.equal(result.review.extension.sha256, sha256(await readFile(extension)));
    assert.deepEqual(await readdir(dir), ["check-profile.json", "check.stderr", "check.stdout", "profile.json", "provider.mjs"]);
  } finally {
    await stdout.close();
    await stderr.close();
  }
});
