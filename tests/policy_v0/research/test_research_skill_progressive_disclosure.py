from pathlib import Path


ROOT = Path(__file__).resolve().parents[3] / "a_share_research/policy/v0/guide" / "a-stock-research"
MAIN = ROOT / "SKILL.md"
REFERENCES = {
    "data_cache": ROOT / "references" / "data-cache-contract.md",
    "execution": ROOT / "references" / "research-execution-flow.md",
    "cycle": ROOT / "references" / "cycle-assessment.md",
    "timing": ROOT / "references" / "timing-adjustments.md",
    "report": ROOT / "references" / "report-contract.md",
}


def test_research_skill_routes_progressive_disclosure_references() -> None:
    main = MAIN.read_text(encoding="utf-8")

    for path in REFERENCES.values():
        assert path.is_file()
        assert f"](references/{path.name})" in main

    cycle = REFERENCES["cycle"].read_text(encoding="utf-8")
    assert "decision-v1" in cycle
    assert "cycle_stage" in cycle
    assert "周期位置[阶段=" not in cycle
    timing = REFERENCES["timing"].read_text(encoding="utf-8")
    assert "预期差分析" in timing
    assert "除权/除息贴权检查" in timing
    assert "市场风格适配" in timing
    report = REFERENCES["report"].read_text(encoding="utf-8")
    assert "标准评估卡" in report
    assert "操作建议禁止条款" in report
    assert "默认输出五段决策卡" in report
    assert "用户明确要求详细审计时" in report
    assert "P3：not_applicable" in report


def test_analysis_hit_stops_before_full_research_flow() -> None:
    main = MAIN.read_text(encoding="utf-8")
    analysis_hit = main.split("`ANALYSIS_HIT`", 1)[1].split("##", 1)[0]

    assert "直接展示当日缓存并停止" in analysis_hit
    assert "research-execution-flow.md" not in analysis_hit
    assert "`FUNDAMENTALS_HIT` 或 `FULL_MISS`" in main
    assert "research-execution-flow.md" in main


def test_research_skill_keeps_boundaries_but_not_deterministic_policy_in_main() -> None:
    main = MAIN.read_text(encoding="utf-8")
    execution = REFERENCES["execution"].read_text(encoding="utf-8")
    data_cache = REFERENCES["data_cache"].read_text(encoding="utf-8")

    for marker in (
        "--confirm-write",
        "a-stock-fetch fetch <股票代码>",
        "`fetch` 失败即停止",
        "fail-closed",
        "incomplete/not_formed",
        "a-stock-qa",
        "不得从自然语言标签重建",
    ):
        assert marker in main

    # Detailed gates, formulae, and schema live below the entrypoint.
    for detail in (
        "最新中报/季报冲突门",
        "industry_status=stale_cache",
        "raw = 估值分",
        "## 第三步：择时评分",
        "FUNDAMENTALS_HIT 最小数据卡",
        "legacy_field_absent",
    ):
        assert detail not in main
    assert "最新中报/季报冲突门" in data_cache
    assert "industry_status=stale_cache" in data_cache
    assert "raw = 估值分" in execution
    assert "唯一操作出口" in execution


def test_research_skill_bounds_first_pass_source_work_and_qa_handoff() -> None:
    main = MAIN.read_text(encoding="utf-8")

    for marker in (
        "默认最多一批、最多四个独立查询",
        "不得探测重复 Provider",
        "达到已校验结论或 fail-closed 结论后停止",
        "不可变快照",
        "正文改变必须重新 QA",
    ):
        assert marker in main
    assert "只对失败、缺失、过期或冲突项做一次有理由的 fallback" in main


def test_research_routes_scale_electronics_manufacturing_to_a() -> None:
    main = MAIN.read_text(encoding="utf-8")
    execution = REFERENCES["execution"].read_text(encoding="utf-8")
    framework_f = (ROOT / "references/frameworks/F.md").read_text(encoding="utf-8")

    assert "research-execution-flow.md" in main
    assert "EMS/ODM/电子组装/连接器/线束/声学器件/元器件/精密电子制造" in execution
    rows = [
        line.split("|") for line in execution.splitlines() if "| 通用科技制造 |" in line
    ]
    assert len(rows) == 1
    assert rows[0][3].strip() == "A框架"
    assert "[A](frameworks/A.md)" in execution
    assert (REFERENCES["execution"].parent / "frameworks/A.md").is_file()
    assert "盈利公司使用真实TTM PEG" in framework_f
    assert "现金跑道/稀释" in framework_f
    assert "EMS/ODM" not in main


def test_a_framework_inputs_and_external_risk_contract_match_qa() -> None:
    main = MAIN.read_text(encoding="utf-8")
    execution = REFERENCES["execution"].read_text(encoding="utf-8")
    framework_a = (ROOT / "references/frameworks/A.md").read_text(encoding="utf-8")
    rubric = (
        ROOT.parent / "a-stock-qa/references/rubrics/a-stock-research.md"
    ).read_text(encoding="utf-8")

    assert "research-execution-flow.md" in main
    assert "异动、治理/政策与外部集中风险" in execution
    assert "gross_margin_stable" in framework_a
    assert "补充指标 JSON" in framework_a
    assert "source_provenance" in framework_a
    assert "外部集中风险覆盖" in rubric
    assert "外部集中风险[" not in framework_a
    assert "gross_margin_stable" not in main


def test_reference_scores_cannot_reintroduce_a_second_trade_decision() -> None:
    execution = REFERENCES["execution"].read_text(encoding="utf-8")
    assert "异动只影响上述择时参考评分" in execution
    assert "不单独把有效 Research 立场改为等待" in execution
    assert "双轨矩阵仅供参考" in execution
    assert "股数只取 `position-size`" in execution
    for obsolete in (
        "建议等待回踩确认",
        "仓位由两者共同决定",
        "积极买入2/3仓",
        "| 买入1/3仓",
        "时机较差，等待",
    ):
        assert obsolete not in execution
