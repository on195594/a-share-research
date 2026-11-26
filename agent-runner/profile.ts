import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import type * as Schema from "@earendil-works/pi-schema";

export const comparisons = ["q20_select", "q20_rank", "s10_select"] as const;
export type Comparison = typeof comparisons[number];
export type Stage = "explore" | "compose" | "repair" | "review";
export type Role = "author" | "reviewer";
const steps = ["explore", "compose", "repair", "review", ...comparisons] as const;
const countLimits = [
  "model_calls", "monthly_model_calls", "run_seconds", "model_call_seconds",
  "acquisition_seconds", "acquisition_rounds", "fetch_calls", "explore_tool_calls",
  "repair_extra_tool_calls", "draft_call_reserve", "no_progress_rounds", "review_rounds",
  "comparison_attempts_per_task", "monthly_batch_model_calls",
] as const;
const optionalLimits = ["review_format_corrections", "full_rewrites_per_invalid_patch"] as const;

export const safeInt = (s: typeof Schema, minimum = Number.MIN_SAFE_INTEGER) =>
  s.int({ minimum, maximum: Number.MAX_SAFE_INTEGER });
const fields = <const K extends string, T>(keys: readonly K[], value: Schema.Schema<T>) =>
  Object.fromEntries(keys.map(key => [key, value])) as Record<K, Schema.Schema<T>>;

function profileSchema(s: typeof Schema) {
  const role = s.obj({
    provider: s.str({ minLength: 1 }), model: s.str({ minLength: 1 }),
    reasoning: s.oneOf(["off", "minimal", "low", "medium", "high", "xhigh", "max"]), max_tokens: safeInt(s, 1),
  });
  return s.obj({
    roles: s.obj({ author: role, reviewer: role }),
    role_for: s.obj(fields(steps, s.oneOf(["author", "reviewer"]))),
    limits: s.obj({ ...fields(countLimits, safeInt(s, 1)), ...fields(optionalLimits, safeInt(s, 0)) }),
    rationale: s.obj(fields([...countLimits, ...optionalLimits], s.str({ minLength: 1 }))),
  });
}
type ProfileData = Schema.Static<ReturnType<typeof profileSchema>>;
export type ModelConfig = ProfileData["roles"][Role];
export type Profile = ProfileData & { sha256: string };

export class RunnerError extends Error {
  code: number;
  constructor(code: number, message: string) {
    super(message);
    this.code = code;
  }
}

export function sha256(bytes: string | Buffer): string {
  return createHash("sha256").update(bytes).digest("hex");
}

export async function readProfile(path: string, s: typeof Schema): Promise<Profile> {
  const bytes = await readFile(path);
  const result = s.compile(profileSchema(s)).validate(JSON.parse(bytes.toString("utf8")));
  if (!result.ok) {
    const { path: at, message } = result.errors[0];
    throw new RunnerError(2, `Invalid profile ${at || "/"}: ${message}`);
  }
  return { ...result.value, sha256: sha256(bytes) };
}
