import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3] / "a_share_research/policy/v0/guide" / "a-stock-monitor"
SKILL = (ROOT / "SKILL.md").read_text(encoding="utf-8")
DAILY_MONITORING = (ROOT / "references" / "daily-monitoring-and-l3.md").read_text(
    encoding="utf-8"
)
VALUATION_REVIEW = (ROOT / "references" / "valuation-and-annual-review.md").read_text(
    encoding="utf-8"
)
DATA_OPERATIONS = (ROOT / "references" / "data-operations.md").read_text(
    encoding="utf-8"
)
CD_ACCUMULATION = (ROOT / "references" / "step3.5-cd-accumulation.md").read_text(
    encoding="utf-8"
)
TIER_RULES = (ROOT / "references" / "step4-tier-system.md").read_text(encoding="utf-8")
PORTFOLIO_RISK = (ROOT / "references" / "portfolio-risk.md").read_text(encoding="utf-8")
DECISIONS = json.loads(
    (ROOT / "references" / "decision-table.json").read_text(encoding="utf-8")
)


def test_followup_and_account_conflict_contracts_are_explicit():
    assert "账户、证据和用户目标未变时，追问不改变行动结论" in SKILL
    assert "发现前次错误应明确纠正并解释依据" in SKILL
    assert "不把旧账本风险汇总当作当前账户结果" in SKILL
    assert "缺口逐项说明缺什么、由系统还是用户补" in SKILL
    assert "相较上次的关键变化" in SKILL
    assert "已提供材料不重复索取" in SKILL
    assert "仅比较本会话或用户提供的上次结论，不额外检索历史" in SKILL


def test_reconciliation_preserves_events_and_authorization():
    assert "用户确认、仅供本次分析的持仓" in DATA_OPERATIONS
    assert "尚未同步的正式账本" in DATA_OPERATIONS
    assert "不把卖出回款重复加到截图现金" in DATA_OPERATIONS
    assert "取得具体写入授权" in DATA_OPERATIONS
    assert "避免重复成交或重复分红" in DATA_OPERATIONS
    assert "position-return <代码> <快照现价>" in DATA_OPERATIONS
    assert "合计一致不能证明事件未重复" in DATA_OPERATIONS
    assert "暂缓该笔写入" in DATA_OPERATIONS
    assert "不否认已成功记账" in DATA_OPERATIONS
    assert "--account-scope" in DATA_OPERATIONS
    assert "本次及存储都缺范围才请求补充" in PORTFOLIO_RISK
    assert "不从政策文件反推用户确认" in PORTFOLIO_RISK
    assert "不在示例命令中覆盖个人预算" in PORTFOLIO_RISK
    assert "--max-position-risk-pct 2" not in PORTFOLIO_RISK


def test_level2_checklist_semantics_remain_owned_after_main_is_thinned():
    # Deterministic checklist rows must not be duplicated in the entrypoint.
    assert not re.search(r"^\| C\d{2} \|", SKILL, flags=re.MULTILINE)

    # Preserve the substantive C01-C10 coverage at its authoritative owners.
    for contract, owner in (
        ("单股当前净值回撤贡献", PORTFOLIO_RISK),
        ("framework/framework_confident", PORTFOLIO_RISK),
        ("alerts <代码>", DAILY_MONITORING),
        ("明显弱于所属行业指数 ≥2pts", DAILY_MONITORING),
        ("本轮 `monitor-snapshot`", DAILY_MONITORING),
        ("L3 全量核查强制要求", DAILY_MONITORING),
        ("不得直接清仓", DAILY_MONITORING),
        ("各框架触发线", VALUATION_REVIEW),
        ("技术卖出候选", VALUATION_REVIEW),
        ("校验交易所整手/零股申报约束", DATA_OPERATIONS),
    ):
        assert contract in owner


def test_generic_single_holding_request_has_bounded_delivery_contract():
    assert "### Level 1：默认快照" in SKILL
    assert "a-stock-cache monitor-snapshot --json" in SKILL
    assert "默认使用系统保存的最新账户总资产" in SKILL
    assert "干净快速路径" in SKILL and "立即停止" in SKILL
    assert (
        "只加载与 runtime 升级原因、上述账户差异、已有研究提醒或用户明确请求相符的资料"
        in SKILL
    )
    assert "默认不启动研究子代理" in SKILL
    assert "a-stock-qa` 尚无 monitor rubric" in SKILL
    assert "监控任务不得调用它" in SKILL


def test_level1_failure_snapshot_and_context_contract_is_bounded():
    for marker in (
        "有效 JSON 后退出非零",
        "阻断、复核候选、触发、到期、异常或冲突",
        "`escalations` 指定的持仓和证据",
        "缺少 required 数据",
        "冻结相关交易与风险变更",
    ):
        assert marker in SKILL


def test_daily_monitoring_has_a_hard_tool_call_budget():
    for marker in (
        "Level 1 固定为一次 `monitor-snapshot`",
        "不得额外调用 `holdings`、Wiki、Web 或子代理",
        "每个相关主题只取一个主来源",
        "仅失败、不完整、过期或冲突时允许一次 fallback",
        "默认不启动研究子代理",
    ):
        assert marker in SKILL


def test_decision_table_rule_ids_and_priorities_are_unique():
    rules = DECISIONS["rules"]
    assert DECISIONS["schema_version"] == 1
    assert len({rule["id"] for rule in rules}) == len(rules)
    assert len({rule["priority"] for rule in rules}) == len(rules)


def test_trade_proposals_require_confirmation_and_executable_sizing():
    trade_effects = {"propose_trade", "allow_trade_proposal"}
    for rule in DECISIONS["rules"]:
        assert rule["effect"] != "execute_trade"
        if rule["effect"] in trade_effects:
            assert rule["requires_user_confirmation"] is True
            assert rule["requires_executable_sizing"] is True


def test_financial_deterioration_is_review_not_direct_trade():
    rule = next(rule for rule in DECISIONS["rules"] if rule["id"] == "D03")
    assert rule["effect"] == "open_red_alert_and_framework_review"
    assert rule["action_source"] == "none"


def test_roe_warning_and_red_review_are_separate_rules():
    by_id = {rule["id"]: rule for rule in DECISIONS["rules"]}
    assert by_id["D03Y"]["condition"] == "roe_yoy_drop_gt_3pts_two_quarters"
    assert by_id["D03Y"]["effect"] == "open_yellow_alert"
    assert "roe_yoy_drop_gt_5pts_two_quarters" in by_id["D03"]["condition"]


def test_add_risk_requires_portfolio_risk_preconditions():
    by_id = {rule["id"]: rule for rule in DECISIONS["rules"]}
    required = {
        "portfolio_value_known",
        "single_name_risk_within_budget",
        "portfolio_risk_within_budget",
        "not_in_drawdown_breach_state",
    }
    assert required <= set(by_id["D09"]["preconditions"])


def test_portfolio_risk_names_drawdown_and_fails_closed_on_cost_drift():
    assert "单股当前净值回撤贡献" in PORTFOLIO_RISK
    assert "它不是买入本金亏损，也不是现价距离止损线的“安全垫”" in PORTFOLIO_RISK
    assert "成本比例派生的止损/Tier只可展示来源，不得授权对应交易" in PORTFOLIO_RISK
    assert "账户盈亏成本来源、规则参考成本来源及对账状态" in PORTFOLIO_RISK


def test_thesis_break_outranks_stops_tiers_and_accumulation():
    priorities = {rule["id"]: rule["priority"] for rule in DECISIONS["rules"]}
    assert priorities["D01"] > priorities["D04"] > priorities["D07"] > priorities["D09"]


def test_unvalidated_mechanical_thresholds_are_marked_heuristic():
    by_id = {rule["id"]: rule for rule in DECISIONS["rules"]}
    for rule_id in ("D04", "D05", "D06", "D07", "D08", "D09"):
        assert by_id[rule_id]["heuristic"] is True


def test_skill_has_no_known_invalid_split_or_legacy_sale_path():
    corpus = SKILL + TIER_RULES + VALUATION_REVIEW
    assert "167股（剩333）" not in corpus
    assert "update-return <代码> <实际回报%>" not in corpus
    assert "references/decision-table.json" in SKILL


def test_all_routed_reference_files_exist():
    paths = set(re.findall(r"`(references/[^`]+)`", SKILL))
    for relative in paths:
        assert (ROOT / relative).exists(), relative


def test_phase5_routes_are_unique_and_keep_fail_closed_boundaries_in_main():
    for name in (
        "daily-monitoring-and-l3.md",
        "valuation-and-annual-review.md",
        "step3.5-cd-accumulation.md",
        "step4-tier-system.md",
        "data-operations.md",
        "portfolio-risk.md",
        "decision-table.json",
    ):
        assert SKILL.count(f"references/{name}") == 1

    for marker in (
        "--confirm-write",
        "动作冲突只服从",
        "数据缺失、过期、冲突、不可交易或 runtime 失败时 fail-closed",
        "不得补造触发、股数或成交",
        "建议、授权、申报与成交必须分开",
    ):
        assert marker in SKILL

    # Thresholds and state machines belong to runtime/library/references, not main.
    for detail in ("≥3%", "成本 × 1.25", "候选信号 → 待确认 → 已确认触发"):
        assert detail not in SKILL


def test_cd_accumulation_keeps_cumulative_cap_while_allowing_one_lot():
    assert "max(100股, initial_shares 的1/3)" in CD_ACCUMULATION
    assert "累计增持总量仍不得超过 `initial_shares`" in CD_ACCUMULATION

    def single_op_cap(initial_shares: int, cumulative_additions: int) -> int:
        return min(max(100, initial_shares / 3), initial_shares - cumulative_additions)

    assert single_op_cap(100, 0) == 100
    assert single_op_cap(200, 0) == 100
    assert single_op_cap(200, 100) == 100
    assert single_op_cap(200, 200) == 0


def test_tier1_rebuild_explicitly_resets_only_tier1():
    assert "tier-update <代码> tier1 pending" in TIER_RULES
    assert "Tier2/Tier3 状态保持不变" in TIER_RULES


def test_monitor_has_no_ambiguous_direct_sell_shortcuts():
    assert "净利增速转负｜监管重大转向" not in SKILL
    assert "重大利空公告时立即清仓" not in TIER_RULES
    assert "3pts=黄色预警入口" in VALUATION_REVIEW
    assert "5pts=红色框架复核入口" in VALUATION_REVIEW
    assert "两级条件都不是独立卖出授权" in VALUATION_REVIEW


def test_missing_account_inputs_still_require_one_holding_snapshot():
    assert "缺少总资产时仍运行一次 `a-stock-cache monitor-snapshot --json`" in SKILL
    assert "政策要求范围但本次及存储均未确认时" in SKILL
    assert "不得以 `holdings`、fetcher 报价或手工风险求和替代快照" in SKILL
    assert "不授予新增或加仓权限" in SKILL
    assert "不调用快照" not in SKILL


def test_asset_observations_reuse_and_update_without_repeated_requests():
    assert "不每次索要资金截图" in SKILL
    assert "account-snapshot.json" in SKILL
    assert "不以记录时间补造" in SKILL
    for marker in (
        "### 账户资产快照",
        "2026-11-06 用户授权",
        "备份原记录后更新并读回核对",
        "不代表成交、持仓或风险限额修改",
        "已知旧截图不覆盖更新的资产观察",
    ):
        assert marker in DATA_OPERATIONS


def test_post_write_risk_review_reuses_the_stored_asset_observation():
    post_write = DATA_OPERATIONS.split("每次写入后执行", 1)[1].split("###", 1)[0]
    assert "`$CACHE portfolio-risk`" in post_write
    assert "`$CACHE portfolio-risk --portfolio-value" not in post_write
    assert "仅本次明确覆盖总资产" in post_write and "同源" in post_write
    assert "不要单独附 `--portfolio-value-as-of`" in PORTFOLIO_RISK


def test_l3_cadence_and_quarterly_comparison_require_evidence():
    for marker in (
        "季报只能裁定 `quarterly`/`event` 行",
        "`semiannual`/`annual` 行仅作检查点",
        "记录方向并保持状态",
        "`unspecified` 行须用户通过 `a-stock-cache --confirm-write l3-update <id> <status> <as-of> <证据> [下次复核日期] --evidence-cadence <值>` 分类",
        "a-stock-fetch quarter <代码> --json",
        "yoy_same_period_end",
        "vs_year_start",
        "季节性对照",
        "不能单独据此把 L3 设为 `triggered` 或 `not_triggered`",
    ):
        assert marker in DAILY_MONITORING


def test_news_topics_and_pending_writes_are_explicit():
    assert "每条活动 L3 `core_driver`/`non_core`" in SKILL
    assert "行业或政策主题，加公司公告" in SKILL
    assert "待确认写入" in SKILL
    assert "完整、可复制命令" in SKILL
    assert "l3-update <id> <status> <as-of> <证据>" in SKILL
    assert "`alert-resolve`" in SKILL and "`alert-open` 续期" in SKILL
    assert "仅列出，不执行" in SKILL


def test_shared_research_service_waits_and_checks_latest_disclosure():
    monitor = (ROOT / "references/research-service.md").read_text(encoding="utf-8")
    research = (
        ROOT.parent / "a-stock-research/references/research-service.md"
    ).read_text(encoding="utf-8")
    assert monitor == research
    for marker in (
        "同一问题二选一",
        "轮询间隔至少 30 秒",
        "20 分钟仍未完成则停止等待",
        "不得同时提交研究又自行读披露抢先下结论",
        "证据齐备后只交付一次合并答案",
        "进度说明不得包含结论",
        "`announcement_searches` 的 `until`",
        "报告生成时间与当前日期比较",
        "财报季还须查官方披露列表",
        "引用旧报告用 `--parent-report-id`。",
    ):
        assert marker in monitor


def test_l3_cadence_commands_include_required_positionals():
    command = "a-stock-cache --confirm-write l3-update <id> <status> <as-of> <证据> [下次复核日期]"
    assert command + " --evidence-cadence <值>" in DAILY_MONITORING
    assert command + " [--evidence-cadence <值>]" in SKILL


def test_research_prompts_preserve_action_and_route_pending_cards():
    for marker in (
        "研究提醒（不影响动作状态）",
        "逐条转述 `review_prompts`",
        "不得从提醒推导卖出股数、比例或候选买入数量",
        "结束干净快速路径（`action_status=review_candidate`）",
        "--no-research",
    ):
        assert marker in SKILL
    assert "| `research_thesis_review`" in SKILL
    assert "report_id" in DAILY_MONITORING and "区间暂停解读" in DAILY_MONITORING
    assert "不进入 `escalations`" in VALUATION_REVIEW
    assert "3 个百分点" in VALUATION_REVIEW and "0.025%" in VALUATION_REVIEW

    assert "`review_prompts` 研究提醒" in SKILL
    assert "完成默认交付后立即停止" in SKILL
    assert "即使 `escalations` 为空，也须转述已有 `review_prompts`" in SKILL
    assert "无升级项时也可按下表读取，但不追加取证或行情调用" in SKILL
    assert "组合复盘/换仓比较" in SKILL
    for marker in (
        "`research.holdings/candidates`",
        "保守/中性/乐观年化回报",
        "风险占用",
    ):
        assert marker in PORTFOLIO_RISK
        assert marker not in SKILL
    for marker in ("Research 注", "参考加仓检查点", "情景未经记分"):
        assert marker in VALUATION_REVIEW
