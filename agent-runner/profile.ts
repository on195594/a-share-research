import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import type { ThinkingLevel } from "@earendil-works/pi-agent-core";

export const comparisons = ["q20_select", "q20_rank", "s10_select"] as const;
export type Comparison = typeof comparisons[number];
export type Stage = "explore" | "compose" | "repair" | "review";
export type Role = "author" | "reviewer";
export interface ModelConfig {
  provider: string;
  model: string;
  reasoning: ThinkingLevel;
  max_tokens: number;
}

const limitNames = [
  "model_calls", "monthly_model_calls", "run_seconds", "model_call_seconds",
  "acquisition_seconds", "acquisition_rounds", "fetch_calls", "explore_tool_calls",
  "repair_extra_tool_calls", "draft_call_reserve", "no_progress_rounds", "review_rounds",
  "review_format_corrections", "full_rewrites_per_invalid_patch",
  "comparison_attempts_per_task", "monthly_batch_model_calls",
] as const;

export interface Profile {
  roles: Record<Role, ModelConfig>;
  role_for: Record<Stage | Comparison, Role>;
  limits: Record<typeof limitNames[number], number>;
  sha256: string;
}

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

export async function readProfile(path: string): Promise<Profile> {
  const bytes = await readFile(path);
  const profile = JSON.parse(bytes.toString("utf8"));
  for (const role of ["author", "reviewer"]) {
    const cfg = profile.roles?.[role];
    if (!cfg || typeof cfg.provider !== "string" || !cfg.provider || typeof cfg.model !== "string" || !cfg.model
        || !["off", "minimal", "low", "medium", "high", "xhigh", "max"].includes(cfg.reasoning)
        || !Number.isSafeInteger(cfg.max_tokens) || cfg.max_tokens <= 0) {
      throw new RunnerError(2, `Invalid profile roles.${role}`);
    }
  }
  for (const step of ["explore", "compose", "review", "repair", ...comparisons]) {
    if (!["author", "reviewer"].includes(profile.role_for?.[step])) {
      throw new RunnerError(2, `Invalid profile role_for.${step}`);
    }
  }
  for (const key of limitNames) {
    const minimum = key === "review_format_corrections" || key === "full_rewrites_per_invalid_patch" ? 0 : 1;
    if (!Number.isSafeInteger(profile.limits?.[key]) || profile.limits[key] < minimum) {
      throw new RunnerError(2, `Invalid profile limits.${key}`);
    }
  }
  return { ...profile, sha256: sha256(bytes) };
}
