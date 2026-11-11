from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3] / "a_share_research/policy/v0/guide"
RESEARCH_ROOT = ROOT / "a-stock-research"
QA_ROOT = ROOT / "a-stock-qa"
RESEARCH = (RESEARCH_ROOT / "SKILL.md").read_text(encoding="utf-8")
QA_SKILL = (QA_ROOT / "SKILL.md").read_text(encoding="utf-8")
RUBRIC = (QA_ROOT / "references/rubrics/a-stock-research.md").read_text(
    encoding="utf-8"
)
EXECUTION = (RESEARCH_ROOT / "references/research-execution-flow.md").read_text(
    encoding="utf-8"
)
DATA_CACHE = (RESEARCH_ROOT / "references/data-cache-contract.md").read_text(
    encoding="utf-8"
)
REPORT = (RESEARCH_ROOT / "references/report-contract.md").read_text(encoding="utf-8")
CYCLE = (RESEARCH_ROOT / "references/cycle-assessment.md").read_text(encoding="utf-8")
MONITOR_VALUATION = (
    ROOT / "a-stock-monitor/references/valuation-and-annual-review.md"
).read_text(encoding="utf-8")


def test_decision_changing_claims_require_traceable_evidence() -> None:
    assert "关键论据逐项附证据" in REPORT
    assert "披露事实、计算结果和分析假设" in REPORT
    assert "撤回依赖它的确定性结论" in REPORT
    assert "关键论据须逐项标明" in RUBRIC
    assert "只写“正式材料”或统一来源列表" in RUBRIC
    assert "正文说明摘要未覆盖分部数据却仍称已核验" in RUBRIC
    assert "只检查正文中的记录完整性与内部一致性" in RUBRIC
    assert "不推定摘要必定缺少分部数据" in RUBRIC
    assert "流程QA通过不等于外部事实已验证" in RUBRIC
    assert "重跑 `score-fundamentals`" in REPORT
    assert "同步 decision-v1 与正文" in REPORT
    assert "未取得有效重算结果时不沿用旧评级/矩阵" in REPORT


def test_qa_read_only_delivery_contract() -> None:
    for field in (
        "process_verdict",
        "data_status",
        "decision_status",
        "input_sha256",
        "checks",
        "reason",
        "name",
        "result",
        "note",
    ):
        assert f"`{field}`" in QA_SKILL
    assert "`verdict:" not in QA_SKILL
    assert "未确认具体写入时跳过" in EXECUTION
    assert "不得因未写缓存而跳过 QA" in EXECUTION


def test_qa_checklist_failure_cannot_pass_as_manual_scoring() -> None:
    section = RUBRIC.split("### 6.", 1)[1].split("### 7.", 1)[0]
    passing = section.split("**PASS 信号**", 1)[1].split("**FAIL 信号**", 1)[0]
    failing = section.split("**FAIL 信号**", 1)[1].split("**SKIP 条件**", 1)[0]
    assert "停止基本面评分" in passing and "not_formed" in passing
    assert "纯人工评分" not in passing
    assert "纯人工评分" in failing
    bank = RUBRIC.split("### 16.", 1)[1].split("### 17.", 1)[0]
    assert "complete=false" in bank and "missing_inputs" in bank
    assert "三项至少一项可核验" not in bank


def test_qa_is_runtime_independent() -> None:
    assert "Python" not in QA_SKILL.split("---", 2)[1]
    assert "a_share_research.policy.v0" not in QA_SKILL
    assert "research rubric" in QA_SKILL
    assert "monitoring report" not in QA_SKILL
    assert "a-stock-monitor report" not in QA_SKILL


def test_qa_verdict_is_bound_to_an_immutable_report_snapshot() -> None:
    for contract in (
        "完整报告正文的不可变快照",
        "QA 前后核对同一快照的 SHA-256",
        "内容漂移",
        "宿主独立调用",
        "报告修订后旧 verdict 立即失效",
        "新快照重新执行",
    ):
        assert contract in QA_SKILL
    assert "报告生成者自行模拟检查不算独立 QA" in QA_SKILL


def test_held_stock_route_hard_stops_research_work() -> None:
    holdings_pos = RESEARCH.index("a-stock-cache holdings <股票代码> --active-only")
    stop_pos = RESEARCH.index("确认已持仓后立即终止本 Skill")
    fetch_pos = RESEARCH.index("a-stock-fetch fetch <股票代码>")

    assert holdings_pos < stop_pos < fetch_pos
    assert "改用 `a-stock-monitor`" in RESEARCH
    assert "账本不可用时可继续公开研究" in RESEARCH
    assert "不得声称新仓或给账户级动作" in RESEARCH


def test_qa_separates_process_data_and_decision_status() -> None:
    for marker in ("Process verdict", "Data status", "Decision status"):
        assert marker in QA_SKILL
    assert "三者不得互相冒充" in QA_SKILL
    assert "不证明事实真实、数据完整、外部来源有效" in QA_SKILL
    assert "评分已校准或建议可交易" in QA_SKILL


def test_research_routing_and_checklist_rubric_match_authoritative_references() -> None:
    bank = (RESEARCH_ROOT / "references/frameworks/B.md").read_text(encoding="utf-8")

    assert "商业银行→B" in RUBRIC
    assert "元器件/精密电子制造→A" in RUBRIC
    assert "保险/券商/证券→不适用量化框架" in RUBRIC
    assert "所有 A-F 量化框架" in RUBRIC
    assert "B/D/E 框架不需要 checklist" not in RUBRIC
    assert "不得套用 B 框架" in bank
    assert "保险/券商/证券" in EXECUTION
    assert "静态PE（`pe_static`" in DATA_CACHE
    assert "`pe_ttm` 暂时保留为同值的 deprecated 兼容别名" in DATA_CACHE
    assert "research-execution-flow.md" in RESEARCH
    assert "data-cache-contract.md" in RESEARCH


def test_investment_review_repairs_are_owned_by_execution_framework_and_rubric() -> (
    None
):
    resource = (RESEARCH_ROOT / "references/frameworks/C.md").read_text(
        encoding="utf-8"
    )

    assert "综合得分参考阈值" not in RESEARCH
    assert "可用资金 × 该股仓位建议比例" not in RESEARCH
    assert "唯一操作出口" in EXECUTION
    # 2026-11-04 spec: stance is the backbone, the matrix only a reference, sizes from position-size.
    assert "position-size" in EXECUTION and "不单独否决" in EXECUTION
    assert "立场为现在分批，却仅因矩阵或 incomplete 整体写成“默认等待”" in RUBRIC
    assert (
        "incomplete 路径终止仓位建议" not in RUBRIC
        and "incomplete 不构成跳过理由" in RUBRIC
    )
    assert "估值冲突" in resource
    assert "AISC" in resource
    assert "最高只能给格档" in resource
    assert "估值算术与报告期一致性" in RUBRIC
    assert "唯一决策出口与仓位范围" in RUBRIC
    assert "C资源估值冲突与成本证据" in RUBRIC
    assert "关键择时数据缺失或估值冲突明确进入 `incomplete`" in RUBRIC


def test_second_investment_review_fail_closed_contract_has_reference_owners() -> None:
    assert "不得用10年分位替代5年分位" in EXECUTION
    assert "最新中报/季报冲突门" in DATA_CACHE
    assert "B框架三项核心数据全缺" in DATA_CACHE
    assert "亏损 Biotech" in EXECUTION and "定性路线" in EXECUTION
    assert "估值项进入 `incomplete`" in EXECUTION
    assert "评分上限仍为10/15" not in EXECUTION
    assert "估值项记0/15" in CYCLE
    assert "缺总资产或已确认风险快照时不给股数" in EXECUTION
    assert "industry_status=stale_cache" in DATA_CACHE
    assert "关键估值能力与最新财报核验" in RUBRIC
    assert "正式来源身份核验" in RUBRIC
    assert "银行核心数据完整性" in RUBRIC
    assert "COMPLIANT` 只表示该文本满足流程 rubric" in QA_SKILL

    # These deterministic details are deliberately absent from thin entrypoints.
    for detail in (
        "最新中报/季报冲突门",
        "industry_status=stale_cache",
        "估值项记0/15",
    ):
        assert detail not in RESEARCH


def test_wuxi_retro_repairs_are_pinned_at_their_authoritative_layers() -> None:
    framework_a = (RESEARCH_ROOT / "references/frameworks/A.md").read_text(
        encoding="utf-8"
    )

    fetch_pos = RESEARCH.index("a-stock-fetch fetch <股票代码>")
    check_pos = RESEARCH.index("a-stock-cache check <股票代码>")
    assert fetch_pos < check_pos
    assert "必须按下列顺序串行执行，禁止并行" in DATA_CACHE
    for field in (
        "pe_static",
        "pe_percentile_5y",
        "ps_ttm",
        "ps_percentile_5y",
        "latest_report_snapshot",
    ):
        assert field in DATA_CACHE
        assert field not in RESEARCH
    for reference in (
        "references/frameworks/A.md",
        "references/frameworks/B.md",
        "references/frameworks/C.md",
        "references/frameworks/D.md",
        "references/frameworks/E.md",
        "references/frameworks/F.md",
        "references/frameworks/step8-graham.md",
    ):
        assert reference.removeprefix("references/") in EXECUTION
        assert reference not in RESEARCH

    assert "INVALID_RUN" in QA_SKILL
    assert "任意 Important 或 Critical 级 FAIL | NON_COMPLIANT" in RUBRIC
    assert "Advisory" in QA_SKILL and "不得改变 verdict" in QA_SKILL
    assert "references/rubrics/a-stock-research.md" in QA_SKILL
    assert "外部集中风险" in framework_a
    assert "外部集中风险覆盖" in RUBRIC
    assert "pe_percentile_5y" in RUBRIC
    assert "ps_percentile_5y" in RUBRIC
    assert "latest_report_snapshot" in RUBRIC


def test_research_and_monitor_share_current_valuation_capability_contract() -> None:
    for field in ("pe_percentile_5y", "ps_ttm", "ps_percentile_5y"):
        assert field in DATA_CACHE
        assert field in MONITOR_VALUATION
        assert field not in RESEARCH

    assert "fetcher.py只有近10年分位" not in MONITOR_VALUATION
    assert "F框架PS及其近5年分位：fetcher.py完全不提供" not in MONITOR_VALUATION
    assert "AKShare手动降级路径不提供同口径PS_TTM" in MONITOR_VALUATION
    assert "字段存在不代表当次必然可得" in MONITOR_VALUATION


def test_research_qa_and_report_contract_share_incomplete_semantics() -> None:
    assert "incomplete/not_formed" in RESEARCH
    for text in (RUBRIC, REPORT):
        assert "scoring_status=incomplete" in text
        assert "not_formed" in text
        assert "timing_status=incomplete" in text
    assert "PB/BPS" in RUBRIC and "仍必须输出配置评级" in RUBRIC
    assert "无合法原因" in RUBRIC
    assert "基本面小计、配置评级、时机评级、综合分和矩阵均标为 `not_formed`" in RUBRIC
    assert "P1" in REPORT and "保留完整的基本面小计和配置评级" in REPORT
    assert "动作上限降为“首批 1/3”" in REPORT and "不得整体默认等待" in REPORT


def test_fundamentals_hit_minimum_data_card_contract_is_pinned_in_report_owner() -> (
    None
):
    assert "FUNDAMENTALS_HIT 最小数据卡" in REPORT
    for field in (
        "price_change_5d",
        "dividend_yield",
        "dps",
        "latest_report_snapshot.report_period",
        "fields.revenue_yoy",
        "fields.net_profit_yoy",
        "valuation_compatibility",
        "pe_ttm_true",
        "peg_ttm",
        "data_completeness",
        "null_reasons",
        "nested",
    ):
        assert field in REPORT
        assert field not in RESEARCH


def test_legacy_valuation_compatibility_is_fail_closed_without_read_backfill() -> None:
    assert "legacy_field_absent" in DATA_CACHE
    assert "处理为不具备买入资格" in DATA_CACHE
    assert "读取不得回填" in DATA_CACHE
    assert "legacy_field_absent" not in RESEARCH


def test_price_check_requires_positive_provenance_not_absent_bad_wording() -> None:
    section = RUBRIC.split("### 1.", 1)[1].split("### 2.", 1)[0]
    passing = section.split("**PASS 信号**", 1)[1].split("**FAIL 信号**", 1)[0]
    assert "必须有正面记录" in passing
    assert "对应字段、来源与 as-of" in passing
    assert "对应字段、来源与 as-of" in REPORT
    assert "价格注明来自 `a-stock-fetch`/runtime" in REPORT
    assert "正文记录价格缺失及原因" in REPORT
    assert "正文明确记录价格缺失及原因" in passing
    assert "报告无" not in passing
    assert "仅未出现禁用措辞不构成 PASS" in section


def test_checklist_rubric_distinguishes_brief_and_detailed_reports() -> None:
    section = RUBRIC.split("### 6.", 1)[1].split("### 7.", 1)[0]
    assert "默认五段简版" in section
    assert "已展示的评分或评级可追溯至 runtime" in section
    assert "用户要求详细审计" in section
    assert "不得仅因五段简版未展开全部评分子项而判 FAIL" in section
    assert "详细审计附加要求" in REPORT


if __name__ == "__main__":
    tests_run = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            func()
            tests_run += 1
    print(f"qa contract assertions passed: {tests_run}")
