import type { JsonObject } from "@earendil-works/pi-ai";

// The worker owns all prompt wording; the runner only forwards it to the role chosen by the profile.
export interface RunContext {
  run_id: number;
  mode: "run" | "replay";
  effective_model_call_limit: number;
  remaining_ms: number;
  profile_sha256: string;
  prompt: string;
  output_schema: string;
}

export type Decision = { status: "review" | "repair"; prompt: string } | { status: "complete" | "failed" };

export function nextDecision(data: JsonObject): Decision {
  if (data.status === "complete" || data.status === "failed") return { status: data.status };
  if ((data.status === "review" || data.status === "repair") && typeof data.prompt === "string") {
    return { status: data.status, prompt: data.prompt };
  }
  throw new Error("Invalid worker control receipt");
}
