import type * as Schema from "@earendil-works/pi-schema";
import { RunnerError, safeInt } from "./profile.ts";

// The worker owns all prompt wording; the runner only forwards it to the role chosen by the profile.
export function runContextSchema(s: typeof Schema, runId: number, profileSha256: string) {
  return s.obj({
    run_id: s.lit(runId), mode: s.oneOf(["run", "replay"]), effective_model_call_limit: safeInt(s),
    remaining_ms: s.num(), profile_sha256: s.lit(profileSha256),
    system_prompt: s.str({ minLength: 1 }), prompt: s.str({ minLength: 1 }), output_schema: s.str(),
  }, { open: true });
}
export type RunContext = Schema.Static<ReturnType<typeof runContextSchema>>;

export type Decision = { status: "review" | "repair"; system_prompt: string; prompt: string }
  | { status: "complete" | "failed" };

export function decisions(s: typeof Schema) {
  const receipt = s.compile(s.union([
    s.obj({ status: s.oneOf(["review", "repair"]), system_prompt: s.str({ minLength: 1 }),
      prompt: s.str({ minLength: 1 }) }, { open: true }),
    s.obj({ status: s.oneOf(["complete", "failed"]) }, { open: true }),
  ]));
  return (data: unknown): Decision => {
    if (!receipt.check(data)) throw new RunnerError(1, "Invalid worker control receipt");
    return data.status === "review" || data.status === "repair"
      ? { status: data.status, system_prompt: data.system_prompt, prompt: data.prompt } : { status: data.status };
  };
}
