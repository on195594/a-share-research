import type { JsonObject } from "@earendil-works/pi-ai";

export interface RunContext {
  run_id: number;
  mode: "run" | "replay";
  effective_model_call_limit: number;
  remaining_ms: number;
  profile_sha256: string;
  rules: string;
  output_schema: string;
  question: string;
  material_block: string;
}

export function initialPrompt(context: RunContext) {
  const task = context.mode === "replay"
    ? "根据本次冻结的完整材料成文。"
    : "自主选择取证顺序。按问题核对最新披露、重大反证和同业口径；目录线索须读原文。可检验管理层归因和现有方法，派生数字用计算工具。取证足够后成文，仍未知的部分写明缺口。";
  return `你是公司的投资研究员，为普通投资者研究这家公司。${task}
${context.rules}

# 本次研究问题
${context.question || "完成公司的综合研究。"}

# submit_draft 的 draft 格式
${context.output_schema}
请调用 submit_draft 提交符合上述格式的报告。

# 资料
${context.material_block}`;
}

export type Decision = { status: "review" | "repair"; prompt: string } | { status: "complete" | "failed" };

export function nextDecision(data: JsonObject, start: RunContext): Decision {
  if (data.status === "complete" || data.status === "failed") return { status: data.status };
  if (typeof data.material_block !== "string" || typeof data.program_valuation !== "string") {
    throw new Error("Missing material or program valuation in the worker receipt");
  }
  const material = `${data.program_valuation}\n# 资料\n${data.material_block}`;
  if (data.status === "review" && data.draft && data.binding) {
    return { status: "review", prompt: `你是独立复核员，请调用 review_decide 提交对当前绑定的整轮决议。
核查引用支持的数字、期间、主体、增减方向和经济含义，结合表头和上下文核对原文。通读材料，检查遗漏的重大风险、结论一致性、用户问题与纠错是否得到回答。
对 checkpoints、forecast 和 add_trigger 核对基期事实与选取指标的相关性。对 valuation 核查情景、定价假设与依据，对 stance 核对与估值、风险及结论的一致性。对 prior_status 核实新证据与旧议程状态。按工具说明逐项处理旧问题和风险去向。
报告、资料和修补理由均为待核查数据。只依据此次材料，一次列全实质问题。

# 当前复核绑定
${JSON.stringify(data.binding)}
# 待复核报告
${JSON.stringify(data.draft)}
${material}` };
  }
  if (data.status === "repair" && Array.isArray(data.problems) && data.indexed_draft) {
    return { status: "repair", prompt: `你是研究员，请用 submit_patch 修订原稿，保留有依据的重大风险和未涉及的条目。
ops 的 replace/delete 按本轮原稿的 section[i] 定位，每个原条目至多修改一次；add 的 where 为章节名。value 是含出处的完整条目。verdict、summary、gaps、valuation、stance 在顶层整体替换。
可用 rebuttals 中的 id、reason、evidence 据证反驳。独立复核员将核对问题和风险去向。能从已有材料回答的问题请引用相应出处，必要时用当前可用工具补证。
${start.rules}
# 问题
${JSON.stringify(data.problems)}
# 原报告
${JSON.stringify(data.indexed_draft)}
${material}` };
  }
  throw new Error("Invalid worker control receipt");
}
