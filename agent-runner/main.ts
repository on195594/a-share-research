console.log = console.error;
import { parseArgs } from "node:util";
import type { Readable, Writable } from "node:stream";
import type { McpClient } from "@earendil-works/pi-mcp";
import { createRuntime, loadPi, resolveModels } from "./runtime.ts";
import type { Pi, ResolvedRole } from "./runtime.ts";
import { readProfile, RunnerError } from "./profile.ts";
import { runResearch } from "./research.ts";
import { runSingle } from "./single.ts";

export async function withWorker(pi: Pi, input: Readable, output: Writable, work: (client: McpClient) => Promise<void>) {
  const client = new pi.mcp.McpClient({ name: "a-share-agent-runner", version: "1", requestTimeoutMs: 300_000 });
  const transport = new pi.mcp.PipeTransport({
    closeTimeoutMs: 0,
    async open({ onStdout }) {
      const { promise: exited, resolve } = Promise.withResolvers<void>();
      input.on("data", onStdout);
      input.once("end", resolve);
      return { write: data => output.write(data), closeStdin: () => output.end(resolve), close: resolve, exited };
    },
  });
  try {
    try {
      await client.connect(transport);
    } catch {
      throw new RunnerError(1, "Cannot connect to the worker MCP server");
    }
    await work(client);
  } finally {
    try {
      await client.close();
    } finally {
      input.destroy();
    }
  }
}

function roleInfo(role: ResolvedRole) {
  const { provider, model, reasoning } = role.config;
  return { provider, model, reasoning, model_source: role.model_source, extension: role.extension };
}

async function main() {
  const [mode, ...argv] = process.argv.slice(2);
  const { values } = parseArgs({ args: argv, options: {
    profile: { type: "string" }, "run-id": { type: "string" }, "batch-id": { type: "string" }, replay: { type: "string" },
  } });
  const check = mode === "--check";
  const positiveId = (value?: string) => /^\d+$/.test(value ?? "") && Number.isSafeInteger(Number(value)) && Number(value) > 0;
  if (!values.profile || (!check && mode !== "single" && mode !== "run")
      || (mode === "single" && !positiveId(values["batch-id"])) || (mode === "run" && !positiveId(values["run-id"]))
      || (values.replay !== undefined && (mode !== "run" || !positiveId(values.replay)))) {
    throw new RunnerError(2, "Expected run --run-id N [--replay N], single --batch-id B or --check, with --profile PATH");
  }
  const profile = await readProfile(values.profile);
  const pi = await loadPi();
  async function work(client?: McpClient) {
    const runtime = await createRuntime(pi, check);
    let readiness;
    try {
      const models = await resolveModels(pi, runtime, profile, check);
      const stream = runtime.streamSimple.bind(runtime);
      if (client) {
        try {
          await runResearch(client, pi, models, profile, stream, Number(values["run-id"]));
        } catch (error) {
          throw error instanceof RunnerError ? error : new RunnerError(1, "Research MCP protocol failed");
        }
      } else if (check) {
        readiness = { ready: true, pi_version: pi.version, node_version: process.version,
          ...roleInfo(models.author), review: roleInfo(models.reviewer) };
      } else {
        await runSingle(process.stdin, process.stdout, pi, models, profile, stream);
      }
    } finally {
      await runtime.settleCredentialWrites();
    }
    if (readiness) process.stdout.write(JSON.stringify(readiness) + "\n");
  }
  if (mode === "run") await withWorker(pi, process.stdin, process.stdout, work);
  else await work();
}

if (import.meta.main) {
  try {
    await main();
  } catch (error) {
    const safe = error instanceof RunnerError ? error : new RunnerError(2, "Check runner arguments, pi installation and configuration files");
    process.exitCode = safe.code;
    if (process.argv[2] === "--check") process.stdout.write(JSON.stringify({ ready: false, error: safe.message }) + "\n");
    else console.error(safe.message);
  }
}
