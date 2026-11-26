import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, open, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { createInterface } from "node:readline";
import { PassThrough } from "node:stream";
import { after, test } from "node:test";
import { setImmediate } from "node:timers/promises";
import { pathToFileURL } from "node:url";
import type { StreamFn } from "@earendil-works/pi-agent-core";
import type { AssistantMessage, JsonObject, Message } from "@earendil-works/pi-ai";
import type { McpClient } from "@earendil-works/pi-mcp";
import type * as McpTesting from "@earendil-works/pi-mcp/testing";
import { runBatch } from "../batch.ts";
import { withWorker } from "../main.ts";
import { readProfile, sha256 } from "../profile.ts";
import type { Profile } from "../profile.ts";
import { runResearch } from "../research.ts";
import { createRuntime, loadPi, resolveModels } from "../runtime.ts";

const pi = await loadPi();
const mcpTesting: typeof McpTesting = await import(pathToFileURL(join(dirname(pi.base), "pi-mcp/dist/testing/index.js")).href);
const dir = await mkdtemp(join(tmpdir(), "agent-runner-"));
after(() => rm(dir, { recursive: true, force: true }));
const profilePath = join(dir, "profile.json");
const config = { provider: "openai", model: "gpt-4o", reasoning: "medium", max_tokens: 4000 };
const limits = { model_calls: 30, monthly_model_calls: 21, run_seconds: 60, model_call_seconds: 10,
  acquisition_seconds: 45, acquisition_rounds: 24, fetch_calls: 48, explore_tool_calls: 120,
  repair_extra_tool_calls: 48, draft_call_reserve: 4, no_progress_rounds: 2, review_rounds: 3,
  review_format_corrections: 1, full_rewrites_per_invalid_patch: 1, comparison_attempts_per_task: 2,
  monthly_batch_model_calls: 636 };
await writeFile(profilePath, JSON.stringify({
  roles: { author: config, reviewer: { ...config, model: "gpt-4o-mini" } },
  role_for: { explore: "author", compose: "author", repair: "author", review: "reviewer",
    q20_select: "author", q20_rank: "author", s10_select: "author" },
  limits, rationale: Object.fromEntries(Object.keys(limits).map(key => [key, "test limit"])),
}));
const profile = await readProfile(profilePath, pi.schema);
const runtime = await createRuntime({ ...pi, agentDir: dir }, true);
const models = await resolveModels(pi, runtime, profile, false);
after(() => runtime.settleCredentialWrites());
const usage = { input: 7, output: 3, totalTokens: 10, cacheRead: 0, cacheWrite: 0,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
const controls = ["submit_draft", "submit_patch", "review_decide", "record_call"];
const evidence = ["calculate", "macro", "peers"];
const review = { status: "review", binding: { version: 1 }, draft: { title: "bound draft" },
  system_prompt: "reviewer instructions", prompt: "review material program value" };
const repair = { status: "repair", problems: ["resolve this issue"], indexed_draft: { "answer[0]": "original" },
  system_prompt: "repair instructions", prompt: "resolve this issue answer[0] refreshed material program value" };
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
    const error = "error" in turn ? turn.error : undefined;
    const stopReason = Array.isArray(turn) ? "toolUse" : error === undefined ? "stop" : "error";
    const message: AssistantMessage = { ...pi.ai.fauxAssistantMessage(Array.isArray(turn)
      ? turn.map((name, i) => pi.ai.fauxToolCall(name, {}, { id: `c${requests.length}-${i}` }))
      : "text" in turn ? turn.text : [], { stopReason, errorMessage: error }),
      api: model.api, provider: model.provider, model: model.id, usage };
    const events = pi.ai.createAssistantMessageEventStream();
    if ("waitForAbort" in turn) {
      options!.signal!.addEventListener("abort", () => events.push({ type: "error", reason: "aborted",
        error: { ...message, stopReason: "aborted" } }), { once: true });
    } else if (stopReason === "error") {
      events.push({ type: "error", reason: stopReason, error: message });
    } else {
      events.push({ type: "done", reason: stopReason, message });
    }
    return events;
  };
}

type WorkerOptions = {
  replay?: boolean; auditError?: boolean; budget?: number; remainingMs?: number; memory?: boolean; context?: JsonObject;
  profile?: Profile;
};

type RpcRequest = { id?: number; method: string; params?: any };

async function overPipe(respond: (request: RpcRequest) => Promise<object | undefined>,
                        work: (client: McpClient) => Promise<void>, events: string[]) {
  const input = new PassThrough(), output = new PassThrough();
  const server = (async () => {
    for await (const line of createInterface({ input: output })) {
      const response = await respond(JSON.parse(line));
      if (response) input.write(JSON.stringify(response) + "\n");
    }
    events.push("closed");
  })();
  try {
    await withWorker(pi, input, output, work, 60_000);
  } finally {
    await server;
  }
}

function worker(turns: Turn[], reply: (name: string) => object, options: WorkerOptions = {}) {
  const events: string[] = [], requests: Request[] = [], audits: JsonObject[] = [];
  const modelErrors: JsonObject[] = [];
  async function respond({ id, method, params }: RpcRequest) {
    if (id === undefined) return;
    let result;
    if (method === "initialize") {
      result = { protocolVersion: params.protocolVersion, capabilities: { tools: {}, resources: {} },
        serverInfo: { name: "test-worker", version: "1" } };
    } else if (method === "resources/read") {
      assert.equal(params.uri, "research://run/context");
      result = { contents: [{ uri: params.uri, text: JSON.stringify({ run_id: 1, question: "initial question",
        system_prompt: "author instructions", prompt: "initial question initial material", output_schema: "test report fields",
        remaining_ms: options.remainingMs ?? 60_000, effective_model_call_limit: options.budget ?? 30, mode: options.replay ? "replay" : "run",
        profile_sha256: (options.profile ?? profile).sha256, ...options.context }) }] };
    } else if (method === "tools/list") {
      result = { tools: [...controls, ...(options.replay ? [] : evidence)].map(name =>
        ({ name, description: name, inputSchema: { type: "object", properties: {} } })) };
    } else if (method === "research/acquisition_stop") {
      events.push(params.reason);
      result = {};
    } else if (method === "research/model_error") {
      modelErrors.push(params);
      events.push(method);
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
    return { jsonrpc: "2.0" as const, id, result };
  }
  const research = (client: McpClient) => runResearch(client, pi, models, options.profile ?? profile,
    fakeStream(turns, requests), 1);
  async function inMemory() {
    const pair = mcpTesting.createInMemoryTransportPair();
    let queue = Promise.resolve();
    pair.server.onMessage(message => {
      queue = queue.then(async () => {
        const response = await respond(message as Parameters<typeof respond>[0]);
        if (response) await pair.server.send(response);
      });
    });
    pair.server.onClose(() => events.push("closed"));
    await pair.server.start();
    const client = new pi.mcp.McpClient({ name: "a-share-agent-runner", version: "1", requestTimeoutMs: 60_000 });
    await client.connect(pair.client);
    try {
      await research(client);
    } finally {
      await client.close();
      await queue;
    }
  }
  return { events, requests, audits, modelErrors, run: options.memory ? inMemory : () => overPipe(respond, research, events) };
}

test("the profile schema accepts the shipped profile and rejects unsafe or undeclared values", async () => {
  const shipped = await readProfile("a_share_research/model_profile.json", pi.schema);
  assert.ok(shipped.rationale.model_calls);
  const base = JSON.parse(await readFile(profilePath, "utf8"));
  const variant = join(dir, "variant.json");
  const read = async (edit: (p: any) => void) => {
    const copy = structuredClone(base);
    edit(copy);
    await writeFile(variant, JSON.stringify(copy));
    try {
      return await readProfile(variant, pi.schema);
    } finally {
      await rm(variant);
    }
  };
  assert.equal((await read(p => { p.limits.review_format_corrections = 0; })).limits.review_format_corrections, 0);
  for (const edit of [
    (p: any) => { p.limits.model_calls = Number.MAX_SAFE_INTEGER + 1; },
    (p: any) => { p.limits.fetch_calls = 0; },
    (p: any) => { p.roles.author.max_tokens = 1.5; },
    (p: any) => { p.roles.reviewer.reasoning = "extreme"; },
    (p: any) => { p.role_for.review = "owner"; },
    (p: any) => { p.rationale = { ...p.rationale, model_calls: "" }; },
    (p: any) => { delete p.rationale; },
    (p: any) => { delete p.rationale.fetch_calls; },
    (p: any) => { p.rationale.unknown_limit = "undeclared"; },
    (p: any) => { p.extra = true; },
  ]) await assert.rejects(read(edit), { code: 2, message: /^Invalid profile \// });
});

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
  const [system, user] = w.requests[0].messages;
  assert.equal(system.role, "system");
  assert.equal(system.content, "author instructions");
  assert.deepEqual(system.toolsAdded?.map(t => t.name).sort(), full);
  assert.deepEqual(user, { role: "user", content: [{ type: "text", text: "initial question initial material" }],
    timestamp: user.timestamp });
  // Later rounds extend the same prefix; the exhausted evidence tools leave through one declared delta.
  for (const [i, request] of w.requests.slice(1, 4).entries()) {
    assert.deepEqual(request.messages.slice(0, w.requests[i].messages.length), w.requests[i].messages);
  }
  const deltas = w.requests[3].messages.filter(m => m.role === "system").slice(1);
  assert.deepEqual(deltas.map(m => [m.content, m.toolsAdded, m.toolsRemoved?.map(t => t.name).sort()]),
    [["", undefined, evidence.slice().sort()]]);
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
  for (const [i, input] of [[2, review], [4, repair], [6, review]] as const) {
    const messages = w.requests[i].messages;
    assert.deepEqual(messages.map(m => m.role), ["system", "user"]);
    assert.equal(messages[0].role === "system" && messages[0].content, input.system_prompt);
    assert.deepEqual(messages[0].role === "system" && messages[0].toolsAdded?.map(t => t.name).sort(), w.requests[i].tools);
    assert.deepEqual(messages[1].content, [{ type: "text", text: input.prompt }]);
    assert.doesNotMatch(JSON.stringify(messages), /author-only tool output|initial material|author instructions/);
  }
  // A correction is steered into the next turn, after the loadout change it causes is declared.
  const correction = (i: number) => w.requests[i].messages.slice(w.requests[i - 1].messages.length + 1);
  assert.deepEqual(correction(3).map(m => [m.role, m.content]),
    [["toolResult", w.requests[3].messages.at(-2)!.content], ["user", "请用 review_decide 提交完整、有效的本轮决议。"]]);
  const rewrite = correction(5);
  assert.deepEqual(rewrite.map(m => m.role), ["toolResult", "system", "user"]);
  assert.deepEqual(rewrite[1].role === "system" && [rewrite[1].toolsAdded?.map(t => t.name), rewrite[1].toolsRemoved?.map(t => t.name)],
    [["submit_draft"], ["submit_patch"]]);
  assert.equal(rewrite[2].content, "补丁无效，请用 submit_draft 提交一份完整修订稿。\ntest report fields");
  const repairInput = JSON.stringify(w.requests[4].messages);
  for (const text of ["resolve this issue", "answer[0]", "refreshed material", "program value"]) assert.ok(repairInput.includes(text));
  assert.deepEqual(w.events.filter(x => !["record_call", "audit settled"].includes(x)),
    ["peers", "calculate", "submit_draft", "review_decide", "review_decide", "submit_patch", "submit_draft", "review_decide", "closed"]);
  assert.deepEqual(w.events.slice(-4), ["record_call", "audit settled", "review_decide", "closed"]);
});

test("a batch admits one submission attempt and no evidence fetch past its limit", async () => {
  const limited = join(dir, "fetch-limit.json");
  const copy = JSON.parse(await readFile(profilePath, "utf8"));
  copy.limits.fetch_calls = 1;
  await writeFile(limited, JSON.stringify(copy));
  const oneFetch = await readProfile(limited, pi.schema);
  await rm(limited);
  let drafts = 0;
  const w = worker([["macro", "peers", "calculate", "submit_draft", "submit_draft"], ["submit_draft"], ["review_decide"]],
    name => {
      if (name === "submit_draft") return drafts++ ? review : rejected;
      if (name === "review_decide") return done;
      return { result: "evidence", charged: true, evidence_delta: 1 };
    }, { profile: oneFetch, memory: true });
  await w.run();
  assert.deepEqual(w.events.filter(x => !["record_call", "audit settled"].includes(x)),
    ["macro", "fetch_limit", "calculate", "submit_draft", "submit_draft", "review_decide", "closed"]);
  const results = w.requests[1].messages.filter(m => m.role === "toolResult");
  assert.deepEqual(results.map(m => [m.toolName, m.isError, m.content]), [
    ["macro", false, [{ type: "text", text: JSON.stringify({ result: "evidence", charged: true, evidence_delta: 1 }) }]],
    ["peers", true, [{ type: "text", text: "本项取证已到上限，请用已有资料完成提交。" }]],
    ["calculate", false, [{ type: "text", text: JSON.stringify({ result: "evidence", charged: true, evidence_delta: 1 }) }]],
    ["submit_draft", true, [{ type: "text", text: JSON.stringify(rejected) }]],
    ["submit_draft", true, [{ type: "text", text: "该次提交已处理，本批后续调用已关闭。" }]],
  ]);
  assert.deepEqual(w.requests.map(r => r.tools), [[...evidence, "submit_draft"].sort(), ["calculate", "submit_draft"],
    ["review_decide"]]);
});

test("replay submits the frozen draft and stops after the configured review correction", async () => {
  const w = worker([["submit_draft"], ["review_decide"], ["review_decide"]], name => name === "submit_draft" ? review : rejected, { replay: true });
  await w.run();
  assert.deepEqual(w.audits.map(a => a.step), ["compose", "review", "review"]);
  assert.deepEqual(w.requests.map(r => r.tools), [["submit_draft"], ["review_decide"], ["review_decide"]]);
});

test("the in-memory MCP transport carries the same worker protocol as the stdio pipe", async () => {
  const runs = [];
  for (const memory of [false, true]) {
    const w = worker([["submit_draft"], ["review_decide"]], name => name === "submit_draft" ? review : done,
      { replay: true, memory });
    await w.run();
    runs.push({ events: w.events, steps: w.audits.map(a => a.step), tools: w.requests.map(r => r.tools) });
  }
  assert.deepEqual(runs[1], runs[0]);
  assert.deepEqual(runs[0].events, ["record_call", "audit settled", "submit_draft", "record_call", "audit settled",
    "review_decide", "closed"]);
});

test("each provider attempt is charged to the call budget and audited before a transient error retries", async () => {
  const turns = (): Turn[] => [{ error: "Connection closed." }, ["submit_draft"], ["review_decide"]];
  const reply = (name: string) => name === "submit_draft" ? review : done;
  const attempts = (w: ReturnType<typeof worker>) =>
    w.audits.map(a => [a.step, a.error_kind, ((a.identity as JsonObject).parameters as JsonObject).attempt]);
  const transient = worker(turns(), reply, { budget: 3, replay: true, memory: true });
  await transient.run();
  assert.equal(transient.requests.length, 3);
  assert.deepEqual(transient.requests[1].messages, transient.requests[0].messages);
  assert.deepEqual(attempts(transient), [["compose", "provider_error", 1], ["compose", null, 2], ["review", null, 1]]);
  assert.deepEqual(transient.audits.map(a => a.usage), [usage, usage, usage]);
  assert.deepEqual(transient.audits.map(a => a.response === null), [true, false, false]);
  // The retry spent the call the required review needed.
  const spent = worker(turns(), reply, { budget: 2, replay: true, memory: true });
  await assert.rejects(spent.run, { code: 4 });
  assert.equal(spent.requests.length, 2);
  const unpaid = worker(turns(), reply, { budget: 1, memory: true });
  await assert.rejects(unpaid.run, { code: 3, message: "provider_error" });
  assert.equal(unpaid.requests.length, 1);
  assert.deepEqual(attempts(unpaid), [["explore", "provider_error", 1]]);
  const fatal = worker([{ error: "private vendor detail" }, ["submit_draft"]], () => done, { memory: true });
  await assert.rejects(fatal.run, { code: 3, message: "provider_error" });
  assert.equal(fatal.requests.length, 1);
  assert.deepEqual(attempts(fatal), [["explore", "provider_error", 1]]);
});

test("evidence retries spend only calls above the draft reserve", async () => {
  for (const [budget, attemptCount] of [[5, 1], [6, 2]]) {
    const w = worker([{ error: "Connection closed." }, { error: "Connection closed." }], () => done,
      { budget, memory: true });
    await assert.rejects(w.run, { code: 3, message: "provider_error" });
    assert.equal(w.requests.length, attemptCount);
    assert.ok(w.requests[0].tools.includes("macro"));
    assert.deepEqual(w.audits.map(audit => audit.error_kind), Array(attemptCount).fill("provider_error"));
  }
});

test("abort during retry backoff reports a terminal timeout after auditing the provider error", async () => {
  const w = worker([{ error: "Connection closed." }], () => done, { budget: 6, remainingMs: 200, memory: true });
  await assert.rejects(w.run, { code: 3, message: "timeout" });
  assert.equal(w.requests.length, 1);
  assert.deepEqual(w.audits.map(audit => audit.error_kind), ["provider_error"]);
  assert.deepEqual(w.modelErrors, [{ kind: "timeout" }]);
  assert.deepEqual(w.events, ["record_call", "audit settled", "research/model_error", "closed"]);
});

function auditedSessionId(audit: JsonObject) {
  return (audit.identity as { request_parameters: { session_id: string } }).request_parameters.session_id;
}

test("research session identity follows the initial system instructions and tools across runs", async () => {
  const ids = [];
  for (const [budget, system_prompt, prompt] of [
    [30, "author instructions", "first user question"],
    [30, "author instructions", "second user question"],
    [30, "different author instructions", "first user question"],
    [limits.draft_call_reserve, "author instructions", "first user question"],
  ] as const) {
    const w = worker([["submit_draft"]], () => done, { budget, context: { system_prompt, prompt }, memory: true });
    await w.run();
    ids.push(auditedSessionId(w.audits[0]));
  }
  assert.match(ids[0], /^[a-f0-9]{32}$/);
  assert.equal(ids[0], ids[1]);
  assert.notEqual(ids[0], ids[2]);
  assert.notEqual(ids[0], ids[3]);
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
  assert.equal(w.requests.length, 1);
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

test("every worker prompt must carry non-empty instructions", async () => {
  const context = worker([], () => done, { context: { system_prompt: "" }, memory: true });
  await assert.rejects(context.run, { code: 2 });
  assert.equal(context.requests.length, 0);
  const receipt = worker([["submit_draft"]], () => ({ ...review, system_prompt: undefined }), { memory: true });
  await assert.rejects(receipt.run, { code: 1 });
  assert.deepEqual(receipt.events, ["record_call", "audit settled", "submit_draft", "closed"]);
});

const comparisonTools = ["next_comparison", "record_call", "submit_selection", "submit_ranking"];
const comparisonTasks = ["q20_select", "q20_rank", "s10_select"].map(task => ({
  status: "task", batch_id: 1, task, tool: task === "q20_rank" ? "submit_ranking" : "submit_selection",
  system_prompt: `${task} instructions`, prompt: `${task} candidates`, profile_sha256: profile.sha256,
  max_attempts: 2, remaining_ms: 60_000,
}));

function batchWorker(turns: Turn[], { tasks = comparisonTasks as object[], rejectFirstSubmit = false, auditError = false } = {}) {
  const events: string[] = [], requests: Request[] = [], audits: JsonObject[] = [];
  let taskIndex = 0, attempt = 0, submissions = 0;
  async function respond({ id, method, params }: RpcRequest) {
    if (id === undefined) return;
    let result;
    if (method === "initialize") {
      result = { protocolVersion: params.protocolVersion, capabilities: { tools: {} },
        serverInfo: { name: "test-batch-worker", version: "1" } };
    } else if (method === "tools/list") {
      result = { tools: comparisonTools.map(name => ({ name, description: name,
        inputSchema: { type: "object", properties: {}, additionalProperties: false } })) };
    } else {
      assert.equal(method, "tools/call");
      const name = params.name;
      events.push(name);
      let data;
      if (name === "next_comparison") {
        attempt = 0;
        data = tasks[taskIndex++] ?? { status: "done" };
      } else if (name === "record_call") {
        await setImmediate();
        audits.push(params.arguments);
        events.push("audit settled");
        data = auditError ? rejected : { attempt: ++attempt };
      } else {
        assert.ok(comparisonTools.includes(name));
        data = ++submissions === 1 && rejectFirstSubmit ? rejected : { status: "accepted" };
      }
      result = { content: [{ type: "text", text: JSON.stringify(data) }], structuredContent: data, isError: "error" in data };
    }
    return { jsonrpc: "2.0", id, result };
  }
  const work = (client: McpClient) => runBatch(client, pi, models, profile, fakeStream(turns, requests), 1);
  return { events, requests, audits, run: () => overPipe(respond, work, events) };
}

test("batch runs each task with a fresh prefix, audits submissions and retries with feedback", async () => {
  const w = batchWorker([["submit_selection"], ["submit_selection"], { text: "ranking analysis" },
    ["submit_ranking"], ["submit_selection"]], { rejectFirstSubmit: true });
  await w.run();
  assert.deepEqual(w.events, [
    "next_comparison", "record_call", "audit settled", "submit_selection", "record_call", "audit settled", "submit_selection",
    "next_comparison", "record_call", "audit settled", "record_call", "audit settled", "submit_ranking",
    "next_comparison", "record_call", "audit settled", "submit_selection", "next_comparison", "closed",
  ]);
  for (const [i, task] of comparisonTasks.entries()) {
    const request = w.requests[[0, 2, 4][i]];
    assert.deepEqual(request.messages.map(message => message.role), ["system", "user"]);
    const [system, user] = request.messages;
    assert.equal(system.role, "system");
    assert.equal(system.content, task.system_prompt);
    assert.deepEqual(system.toolsAdded?.map(tool => [tool.name, tool.constrainedSampling]),
      [[task.tool, { type: "json_schema", strict: "prefer" }]]);
    assert.deepEqual(request.tools, [task.tool]);
    assert.deepEqual(user.content, [{ type: "text", text: task.prompt }]);
  }
  const feedback = w.requests[1].messages.at(-1)!;
  assert.equal(feedback.role, "toolResult");
  assert.equal(feedback.isError, true);
  assert.deepEqual(feedback.content, [{ type: "text", text: JSON.stringify(rejected) }]);
  const steering = w.requests[3].messages.at(-1)!;
  assert.equal(steering.role, "user");
  assert.equal(steering.content, `请用 ${comparisonTasks[1].tool} 提交本轮结果。`);
  assert.deepEqual(w.audits.map(audit => audit.step), ["q20_select", "q20_select", "q20_rank", "q20_rank", "s10_select"]);
});

test("batch session identity follows the initial system instructions and tools across tasks", async () => {
  const [select, rank, small] = comparisonTasks;
  const tasks = [select, { ...small, system_prompt: select.system_prompt },
    { ...select, system_prompt: "different selection instructions" },
    { ...rank, system_prompt: select.system_prompt, prompt: select.prompt }];
  const w = batchWorker(tasks.map(task => [task.tool]), { tasks });
  await w.run();
  const [first, differentPrompt, differentSystem, differentTool] = w.audits.map(auditedSessionId);
  assert.match(first, /^[a-f0-9]{32}$/);
  assert.equal(first, differentPrompt);
  assert.notEqual(first, differentSystem);
  assert.notEqual(first, differentTool);
});

test("batch audits a provider error and continues to the next task", async () => {
  const w = batchWorker([["submit_selection"], { error: "upstream failure" }, ["submit_selection"]]);
  await w.run();
  assert.deepEqual(w.events, [
    "next_comparison", "record_call", "audit settled", "submit_selection",
    "next_comparison", "record_call", "audit settled",
    "next_comparison", "record_call", "audit settled", "submit_selection", "next_comparison", "closed",
  ]);
  const rank = w.audits[1];
  assert.deepEqual([rank.step, rank.error_kind, rank.stop_reason, rank.response], ["q20_rank", "provider_error", "error", null]);
  assert.doesNotMatch(JSON.stringify(rank), /upstream failure/);
});

test("batch stops with a protocol error on audit rejection or an invalid task", async () => {
  const rejected = batchWorker([["submit_selection"]], { auditError: true });
  await assert.rejects(rejected.run, { code: 1, message: "record_call failed" });
  assert.deepEqual(rejected.events, ["next_comparison", "record_call", "audit settled", "closed"]);

  const invalid = batchWorker([], { tasks: [{ ...comparisonTasks[0], system_prompt: "" }] });
  await assert.rejects(invalid.run, { code: 1, message: "Invalid comparison task" });
  assert.deepEqual(invalid.events, ["next_comparison", "closed"]);
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
