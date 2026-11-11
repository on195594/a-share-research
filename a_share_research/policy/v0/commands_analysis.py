"""Analysis and fundamentals cache commands."""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from dataclasses import asdict

from a_share_research.policy.v0 import db, decision_contract, domain, risk_gates, store
from a_share_research.core.contracts import (
    CycleStage,
    CycleStageAssessment,
    EvidenceConfidence,
    FrameworkKey,
    RatingTier,
    SubjectiveAssessment,
    SubjectiveCategory,
)
from a_share_research.core import framework_scoring


def _risk_gate_state(
    fundamentals: dict,
) -> tuple[str, list[str], dict[str, dict]] | None:
    return risk_gates.aggregate_fundamentals_gates(fundamentals)


def _hard_gate_failed(gates: dict[str, dict]) -> bool:
    return any(
        name in {"regulatory_gate", "cash_flow_gate"}
        and value.get("status") not in {"clear", "not_applicable"}
        for name, value in gates.items()
    )


def _cash_gate_for_framework(
    data: dict, framework: str, industry: str | None
) -> dict | None:
    gate = data.get("cash_flow_gate")
    if not isinstance(gate, dict):
        return None
    if (
        framework == "B银行"
        or gate.get("reason_code") == "framework_required_for_capex_review"
        or "a_capex_review" in data
        or "cd_capex_review" in data
    ):
        return risk_gates.cash_flow_gate(
            {
                **gate,
                "framework": framework,
                "industry": industry,
                "total_market_cap": data.get("total_market_cap"),
                "a_capex_review": data.get("a_capex_review"),
                "cd_capex_review": data.get("cd_capex_review"),
            }
        )
    return gate


def _not_formed_score(
    code: str, framework: str, reasons: list[str], gates: dict[str, dict]
) -> dict:
    return {
        "framework": framework[0],
        "code": code,
        "scoring_status": "incomplete",
        "timing_status": "incomplete",
        "complete": False,
        "blocked": True,
        "action_eligible": False,
        "subtotal": None,
        "fundamentals_subtotal": "not_formed",
        "configuration_rating": "not_formed",
        "timing_rating": "not_formed",
        "total": "not_formed",
        "matrix": "not_formed",
        "reason_code": "risk_gate_incomplete",
        "risk_gate_reasons": reasons,
        "risk_gates": gates,
    }


def _decision_meta(data: dict, industry: str | None) -> dict:
    framework, confident = domain.infer_framework(industry)
    cash_gate = _cash_gate_for_framework(data, framework, industry)
    gate_data = {**data, "cash_flow_gate": cash_gate} if cash_gate is not None else data
    gate_state = _risk_gate_state(gate_data)
    gates = gate_state[2] if gate_state else {}
    reasons = gate_state[1] if gate_state else ["risk_gates:not_evaluated"]
    hard_failed = _hard_gate_failed(gates) if gates else True
    roe_status = gates.get("roe_structural_gate", {}).get("status")
    valuation = data.get("valuation_compatibility", {})
    payout_ratio = framework_scoring.calculate_payout_ratio(
        data.get("dps"), data.get("eps")
    )
    payout_band = (
        framework_scoring.classify_framework_rule(
            "C", "payout_ratio", payout_ratio
        ).band
        if payout_ratio is not None
        else framework_scoring.RuleBand.MISSING
    )
    uses_pb_percentile = framework == "B银行" or (
        framework == "C资源" and payout_band is framework_scoring.RuleBand.FAIL
    )
    if isinstance(valuation, dict) and not valuation.get("timing_eligible", False):
        reasons = [
            *reasons,
            f"valuation_compatibility:{valuation.get('reason_code', 'timing_ineligible')}",
        ]
    elif uses_pb_percentile and not valuation.get("pb_percentile_eligible", False):
        reasons = [*reasons, "valuation_compatibility:pb_percentile_ineligible"]
    timing_blocked = (
        hard_failed
        or roe_status not in {None, "clear"}
        or any(reason.startswith("valuation_compatibility:") for reason in reasons)
    )
    provenance = data.get("field_provenance", {})
    statuses = [
        value.get("status")
        for key, value in provenance.items()
        if key not in risk_gates.GATE_NAMES and isinstance(value, dict)
    ]
    complete_gates = sum(
        value.get("status") in {"clear", "not_applicable"} for value in gates.values()
    )
    return {
        "framework_candidate": framework,
        "framework_confident": confident,
        "scoring_status": "incomplete" if hard_failed else "not_evaluated",
        "timing_status": "incomplete" if timing_blocked else "not_evaluated",
        "action_eligible": False,
        "reason_codes": list(dict.fromkeys(reasons)),
        "data_completeness": {
            "top_level_present": sum(status == "ok" for status in statuses),
            "top_level_total": len(statuses),
            "risk_gates_complete": complete_gates,
            "risk_gates_total": len(risk_gates.GATE_NAMES),
        },
    }


def cmd_check(args: list[str]) -> None:
    """一次性检查分析结论+基本面缓存，输出状态码+内容"""
    if len(args) < 1:
        print("FULL_MISS")
        return
    code = args[0]
    today = domain.cst_today()

    # 优先检查今日分析结论
    with db.db_session() as conn:
        analysis_row = conn.execute(
            """SELECT result, decision_json, created_at, name, quote_price
               FROM analysis_results WHERE code=? AND date=?""",
            (code, today),
        ).fetchone()
        if analysis_row:
            result, decision_json, created_at, name, analysis_quote = analysis_row
            if decision_json is None:
                _print_legacy_analysis_human_only(code, created_at, result)
            else:
                try:
                    rendered = _render_stored_decision(decision_json, code)
                except decision_contract.DecisionContractError as exc:
                    print(f"ANALYSIS_INVALID {code} {exc}")
                else:
                    latest_quote = store.get_latest_quote_snapshot(
                        code, max_age=store.QUOTE_SNAPSHOT_MAX_AGE
                    )
                    signed_deviation = None
                    absolute_deviation = None
                    if latest_quote is not None and analysis_quote:
                        signed_deviation = (
                            latest_quote["price"] - analysis_quote
                        ) / analysis_quote
                        absolute_deviation = abs(signed_deviation)
                    if latest_quote is None or not analysis_quote:
                        print(
                            f"ANALYSIS_PRICE_STALE {code} 缺少新鲜行情或分析价格快照，需要重新分析"
                        )
                    elif (
                        absolute_deviation is not None
                        and signed_deviation is not None
                        and absolute_deviation
                        < domain.ANALYSIS_PRICE_INVALIDATION_THRESHOLD
                    ):
                        name_str = f"({name})" if name else ""
                        quote_note = (
                            f" 最新报价:{latest_quote['price']:.3f}({latest_quote['source']})"
                            f" 较分析快照:{signed_deviation * 100:+.2f}%"
                        )
                        print(
                            f"ANALYSIS_HIT {code}{name_str} "
                            f"[{domain.format_timestamp_cst(created_at)}]{quote_note}"
                        )
                        print(rendered)
                        return
                    else:
                        assert absolute_deviation is not None
                        print(
                            f"ANALYSIS_PRICE_STALE {code} 最新报价较分析快照偏离"
                            f"{absolute_deviation * 100:.2f}%（阈值3.00%），需要重新分析"
                        )

        # 再检查基本面缓存
        fund_row = conn.execute(
            "SELECT name, industry, data, updated_at, ttl_hours FROM stock_fundamentals WHERE code=?",
            (code,),
        ).fetchone()

    if fund_row:
        name, industry, data, updated_at, ttl_hours = fund_row
        if not domain.is_expired(updated_at, ttl_hours):
            result = store.add_market_indicators(json.loads(data))
            result["_cache_meta"] = {
                "code": code,
                "name": name,
                "industry": industry,
                "updated_at": domain.format_timestamp_cst(updated_at),
                "ttl_hours": ttl_hours,
            }
            result["_decision_meta"] = _decision_meta(result, industry)
            print(
                f"FUNDAMENTALS_HIT {code}({name}) [{industry}] 更新:{domain.format_timestamp_cst(updated_at)}"
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

    print("FULL_MISS")


def cmd_get(args: list[str]) -> None:
    """获取基本面缓存，未命中或过期返回 CACHE_MISS"""
    if len(args) < 1:
        print("CACHE_MISS")
        return
    code = args[0]
    with db.db_session() as conn:
        row = conn.execute(
            "SELECT name, industry, data, updated_at, ttl_hours FROM stock_fundamentals WHERE code=?",
            (code,),
        ).fetchone()
    if not row:
        print("CACHE_MISS")
        return
    name, industry, data, updated_at, ttl_hours = row
    if domain.is_expired(updated_at, ttl_hours):
        print("CACHE_MISS")
        return
    result = store.add_market_indicators(json.loads(data))
    result["_cache_meta"] = {
        "code": code,
        "name": name,
        "industry": industry,
        "updated_at": domain.format_timestamp_cst(updated_at),
        "ttl_hours": ttl_hours,
    }
    result["_decision_meta"] = _decision_meta(result, industry)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_set(args: list[str]) -> None:
    """② 写入基本面数据。TTL 未指定时按行业自动推断。"""
    if len(args) < 4:
        print("错误：需要参数 <代码> <名称> <行业> <JSON数据> [TTL]", file=sys.stderr)
        sys.exit(1)
    code, name, industry, data_str = args[0], args[1], args[2], args[3]
    # TTL：显式指定 > 行业推断
    ttl_hours = int(args[4]) if len(args) > 4 else domain.get_industry_ttl(industry)
    try:
        data = json.loads(data_str)
    except json.JSONDecodeError as e:
        print(f"JSON解析错误: {e}", file=sys.stderr)
        sys.exit(1)
    validation_error = store.validate_fundamentals_payload(data)
    if validation_error:
        print(f"基本面数据校验错误: {validation_error}", file=sys.stderr)
        sys.exit(1)
    with db.db_session() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO stock_fundamentals
               (code, name, industry, data, updated_at, ttl_hours)
               VALUES (?,?,?,?,?,?)""",
            (
                code,
                name,
                industry,
                json.dumps(data, ensure_ascii=False),
                domain.utc_now_iso(),
                ttl_hours,
            ),
        )
        conn.commit()
    print(f"已缓存 {name}({code}) 行业:{industry} TTL:{ttl_hours}h")


def cmd_regulatory_evidence(args: list[str]) -> None:
    """Merge official P0 evidence from stdin into the cached fundamentals for one code."""
    if len(args) != 1:
        print("错误：需要参数 <代码>；从 stdin 读取 P0 证据 JSON", file=sys.stderr)
        sys.exit(1)
    code = args[0]
    try:
        facts = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        print(f"JSON解析错误: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(facts, dict):
        print("错误：P0 证据必须是 JSON 对象", file=sys.stderr)
        sys.exit(1)
    with db.db_session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT data FROM stock_fundamentals WHERE code=?", (code,)
        ).fetchone()
        if row is None:
            conn.rollback()
            print(f"错误：{code} 无基本面缓存，先运行 a-stock-fetch fetch {code}", file=sys.stderr)
            sys.exit(1)
        data = json.loads(row[0])
        period = data.get("data_period")
        if facts.get("report_period") != period:
            conn.rollback()
            print(f"错误：P0 证据 report_period 须为当前年报期 {period}", file=sys.stderr)
            sys.exit(1)
        try:
            gate = risk_gates.regulatory_evidence_gate(facts, code)
        except ValueError as exc:
            conn.rollback()
            print(f"错误：{exc}", file=sys.stderr)
            sys.exit(1)
        store.apply_regulatory_gate(data, gate)
        validation_error = store.validate_fundamentals_payload(data)
        if validation_error:
            conn.rollback()
            print(f"P0 证据校验错误: {validation_error}", file=sys.stderr)
            sys.exit(1)
        # Keep updated_at: new P0 evidence must not make other fundamentals look fresh.
        conn.execute(
            "UPDATE stock_fundamentals SET data=? WHERE code=?",
            (json.dumps(data, ensure_ascii=False), code),
        )
        conn.commit()
    checks = "; ".join(
        f"{name}={check['status']}({check['reason_code']})"
        for name, check in gate["checks"].items()
    )
    print(f"{code} P0监管门={gate['status']}（{period}）| {checks}")


def cmd_get_analysis(args: list[str]) -> None:
    """获取今日分析结论缓存，未命中返回 CACHE_MISS"""
    if len(args) < 1:
        print("CACHE_MISS")
        return
    code = args[0]
    today = domain.cst_today()
    with db.db_session() as conn:
        row = conn.execute(
            """SELECT result, decision_json, created_at FROM analysis_results
               WHERE code=? AND date=?""",
            (code, today),
        ).fetchone()
    if not row:
        print("CACHE_MISS")
        return
    result, decision_json, created_at = row
    if decision_json is None:
        _print_legacy_analysis_human_only(code, created_at, result)
        return
    try:
        rendered = _render_stored_decision(decision_json, code)
    except decision_contract.DecisionContractError as exc:
        print(f"ANALYSIS_INVALID {code} {exc}")
        print("CACHE_MISS")
        return
    print(f"[缓存命中 {domain.format_timestamp_cst(created_at)}]\n{rendered}")


def _render_stored_decision(decision_json: str, expected_code: str) -> str:
    decision = decision_contract.loads_decision(decision_json)
    if decision["stock_code"] != expected_code:
        raise decision_contract.DecisionContractError(
            "stock_code does not match analysis row"
        )
    return decision_contract.render_decision_markdown(decision)


def _print_legacy_analysis_human_only(code: str, created_at: str, result: str) -> None:
    print(
        f"LEGACY_ANALYSIS_HUMAN_ONLY {code} [{domain.format_timestamp_cst(created_at)}]"
    )
    print(result)


def _lookup_cached_stock_name(conn: sqlite3.Connection, code: str) -> str | None:
    row = conn.execute(
        "SELECT name FROM stock_fundamentals WHERE code=?", (code,)
    ).fetchone()
    return row[0] if row else None


def _load_scoring_input(
    text: str,
) -> tuple[dict, list[SubjectiveAssessment], CycleStageAssessment | None]:
    try:
        value = json.loads(
            text,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"非有限数值 {token}")
            ),
        )
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("schema_version 必须为整数 1")
        metrics = value["metrics"]
        raw_assessments = value["subjective_assessments"]
        raw_cycle = value["cycle_stage"]
        if not isinstance(metrics, dict):
            raise ValueError("metrics 必须是对象")
        if not isinstance(raw_assessments, list):
            raise ValueError("subjective_assessments 必须是数组")
        assessments = []
        seen_categories = set()
        for item in raw_assessments:
            if not isinstance(item, dict):
                raise ValueError("subjective_assessments 元素必须是对象")
            evidence = item.get("evidence")
            if (
                not isinstance(evidence, list)
                or not evidence
                or any(
                    not isinstance(entry, str) or not entry.strip()
                    for entry in evidence
                )
            ):
                raise ValueError("subjective_assessments.evidence 必须是非空字符串数组")
            assessment = SubjectiveAssessment(
                category=SubjectiveCategory(item.get("category")),
                rating=RatingTier(item.get("rating")),
                evidence=evidence,
                confidence=EvidenceConfidence(item.get("confidence")),
            )
            if assessment.category in seen_categories:
                raise ValueError("subjective_assessments.category 不得重复")
            seen_categories.add(assessment.category)
            assessments.append(assessment)
        cycle_stage = None
        if raw_cycle is not None:
            if not isinstance(raw_cycle, dict):
                raise ValueError("cycle_stage 必须是对象或 null")
            rationale = raw_cycle.get("rationale")
            if not isinstance(rationale, str) or not rationale.strip():
                raise ValueError("cycle_stage.rationale 必须是非空字符串")
            cycle_stage = CycleStageAssessment(
                stage=CycleStage(raw_cycle.get("stage")), rationale=rationale
            )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"评分输入JSON校验错误: {exc}", file=sys.stderr)
        sys.exit(1)
    return metrics, assessments, cycle_stage


def cmd_score_fundamentals(args: list[str]) -> None:
    """用 a-stock-lib 对缓存与显式补充指标执行只读基本面评分。"""
    if len(args) != 3:
        print(
            "错误：需要参数 <代码> <框架A-F> '<评分输入JSON v1>'；"
            '格式={"schema_version":1,"metrics":{},'
            '"subjective_assessments":[],"cycle_stage":null}',
            file=sys.stderr,
        )
        sys.exit(1)
    code, framework_token, overrides_text = args
    framework = domain.FRAMEWORK_ALIASES.get(framework_token)
    if framework is None:
        print(f"错误：未知框架 {framework_token}", file=sys.stderr)
        sys.exit(1)
    overrides, assessments, cycle_assessment = _load_scoring_input(overrides_text)
    for gate_name in risk_gates.GATE_NAMES:
        if gate_name in overrides:
            error = store.validate_gate_payload(gate_name, overrides[gate_name])
            if error:
                print(f"错误：补充指标 {gate_name} 校验失败: {error}", file=sys.stderr)
                sys.exit(1)
    fundamentals = store.get_fundamentals(code)
    if fundamentals is None:
        print(f"错误：未找到 {code} 的有效基本面缓存", file=sys.stderr)
        sys.exit(1)
    metrics = dict(fundamentals)
    if "net_profit_growth" in metrics:
        metrics["net_profit_growth_3y"] = metrics["net_profit_growth"]
    operating_cf_ratio = framework_scoring.calculate_operating_cf_to_net_profit(
        metrics.get("operating_cf_per_share"), metrics.get("eps")
    )
    if operating_cf_ratio is not None:
        metrics["operating_cf_to_net_profit"] = operating_cf_ratio
    metrics.update(overrides)
    industry = fundamentals.get("_cache_meta", {}).get("industry")
    cash_gate = _cash_gate_for_framework(metrics, framework, industry)
    if cash_gate is not None:
        metrics["cash_flow_gate"] = cash_gate
    gate_state = _risk_gate_state(metrics)
    if gate_state is not None:
        _gate_status, gate_reasons, gates = gate_state
        if _hard_gate_failed(gates):
            print(
                json.dumps(
                    _not_formed_score(code, framework, gate_reasons, gates),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
    score = framework_scoring.score_fundamentals(
        FrameworkKey(framework[0]),
        metrics,
        assessments,
        cycle_stage=cycle_assessment.stage if cycle_assessment else None,
    )
    payload = asdict(score)
    payload["blocked"] = score.blocked
    if gate_state is not None:
        _, gate_reasons, gates = gate_state
        roe_gate = gates.get("roe_structural_gate", {})
        if roe_gate.get("status") != "clear":
            payload.update(
                {
                    "timing_status": "incomplete",
                    "scoring_status": "complete"
                    if score.complete and not score.blocked
                    else "incomplete",
                    "valuation_action_eligible": False,
                    "total": "not_formed",
                    "matrix": "not_formed",
                    "action_eligible": False,
                    "risk_gate_reasons": gate_reasons,
                }
            )
    payload["code"] = code
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _cmd_set_analysis_decision() -> None:
    try:
        decision = decision_contract.loads_decision(sys.stdin.read())
    except decision_contract.DecisionContractError as exc:
        print(f"错误：decision JSON 校验失败: {exc}", file=sys.stderr)
        sys.exit(1)

    code = decision["stock_code"]
    quote = store.get_latest_quote_snapshot(code, max_age=store.QUOTE_SNAPSHOT_MAX_AGE)
    if quote is None:
        print("错误：没有 fetcher 刚写入的有效行情快照，拒绝写入分析", file=sys.stderr)
        sys.exit(1)

    score = decision["framework_score"]
    framework = decision["framework"]
    scoring_status = "complete" if score is not None else "incomplete"
    result = decision_contract.render_decision_markdown(decision)
    decision_json = decision_contract.dumps_decision(decision)
    today = domain.cst_today()
    with db.db_session() as conn:
        name = _lookup_cached_stock_name(conn, code)
        existing = conn.execute(
            """SELECT score_breakdown FROM analysis_results
               WHERE code=? AND date=?""",
            (code, today),
        ).fetchone()
        clear_stale_breakdown = bool(existing and existing[0]) and not (
            _score_breakdown_matches_analysis(
                existing[0],
                framework=framework,
                scoring_status=scoring_status,
                score=score,
            )
        )
        conn.execute(
            """INSERT INTO analysis_results
               (code, date, name, result, decision_json, created_at, score, framework,
                quote_price, quote_as_of, quote_source, scoring_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(code, date) DO UPDATE SET
                   name=excluded.name,
                   result=excluded.result,
                   decision_json=excluded.decision_json,
                   created_at=excluded.created_at,
                   score=excluded.score,
                   framework=excluded.framework,
                   quote_price=excluded.quote_price,
                   quote_as_of=excluded.quote_as_of,
                   quote_source=excluded.quote_source,
                   scoring_status=excluded.scoring_status,
                   score_breakdown=CASE
                       WHEN ? THEN NULL ELSE analysis_results.score_breakdown
                   END""",
            (
                code,
                today,
                name,
                result,
                decision_json,
                domain.utc_now_iso(),
                score,
                framework,
                quote["price"],
                quote["quote_as_of"],
                quote["source"],
                scoring_status,
                clear_stale_breakdown,
            ),
        )
        conn.commit()
    print(
        f"分析结论已缓存：{code} ({today}) 框架:{framework} 评分状态:{scoring_status}"
    )


def cmd_set_analysis(args: list[str]) -> None:
    """Read one decision-v1 JSON document from stdin and cache it for today."""
    if args:
        print(
            "错误：set-analysis 不接受参数；从 stdin 读取 decision JSON",
            file=sys.stderr,
        )
        sys.exit(1)
    _cmd_set_analysis_decision()


def cmd_set_score(args: list[str]) -> None:
    """③ 写入今日综合得分（/80）。需先执行 set-analysis。"""
    if len(args) < 2:
        print("错误：需要参数 <代码> <分数>", file=sys.stderr)
        sys.exit(1)
    code = args[0]
    try:
        score = int(args[1])
    except ValueError:
        print("错误：分数必须为整数", file=sys.stderr)
        sys.exit(1)
    if not 0 <= score <= 80:
        print("错误：分数必须为 0—80 的整数", file=sys.stderr)
        sys.exit(1)
    today = domain.cst_today()
    with db.db_session() as conn:
        state = conn.execute(
            """SELECT framework, COALESCE(scoring_status, 'complete'), score_breakdown
               FROM analysis_results WHERE code=? AND date=?""",
            (code, today),
        ).fetchone()
        if state and state[1] == "incomplete":
            print("错误：评分状态 incomplete 时不得补写完整总分", file=sys.stderr)
            sys.exit(1)
        clear_stale_breakdown = bool(
            state and state[2]
        ) and not _score_breakdown_matches_analysis(
            state[2],
            framework=state[0],
            scoring_status=state[1],
            score=score,
        )
        updated = conn.execute(
            """UPDATE analysis_results
               SET score=?, score_breakdown=CASE WHEN ? THEN NULL ELSE score_breakdown END
               WHERE code=? AND date=?""",
            (score, clear_stale_breakdown, code, today),
        ).rowcount
        conn.commit()
    if updated:
        print(f"得分已记录：{code} → {score}/80")
        if clear_stale_breakdown:
            print("  ⚠️ 原分项得分与新综合得分不一致，已清除，须重新写入")
    else:
        print(
            f"未找到今日分析记录，请先执行 set-analysis（代码：{code}）",
            file=sys.stderr,
        )
        sys.exit(1)


def _validate_score_breakdown_schema(
    breakdown: dict,
    *,
    framework: str | None = None,
    scoring_status: str = "complete",
    score: int | None = None,
) -> str | None:
    """校验 score_breakdown 是否符合统一嵌套 schema，返回错误信息（None 表示通过）。

    统一 schema："fundamentals"/"timing" 均为 dict 且含 "subtotal"。
    旧平铺 schema（{"roe": 8, ...}）不再允许写入，仅历史记录保留供展示降级。
    """
    if not isinstance(breakdown, dict):
        return (
            f"score_breakdown 必须是 JSON 对象（dict），而非 {type(breakdown).__name__}"
        )
    if _contains_non_finite_number(breakdown):
        return "score_breakdown 不得包含 NaN 或 Infinity"
    if "fundamentals" not in breakdown or "timing" not in breakdown:
        return "score_breakdown 必须包含 'fundamentals' 和 'timing' 两个顶层字段（统一 schema）"
    subtotals: dict[str, float | None] = {}
    for key, maximum in (("fundamentals", 60), ("timing", 20)):
        section = breakdown[key]
        if not isinstance(section, dict) or "subtotal" not in section:
            return f"score_breakdown['{key}'] 必须是包含 'subtotal' 字段的对象"
        subtotal = section["subtotal"]
        allow_null_timing = key == "timing" and scoring_status == "incomplete"
        if subtotal is None and allow_null_timing:
            subtotals[key] = None
            continue
        if not isinstance(subtotal, (int, float)) or isinstance(subtotal, bool):
            return f"score_breakdown['{key}'] 的 subtotal 必须是数值（int/float），而非 {type(subtotal).__name__}"
        if not math.isfinite(float(subtotal)):
            return f"score_breakdown['{key}'] 的 subtotal 必须是有限数值"
        if not 0 <= subtotal <= maximum:
            return f"score_breakdown['{key}'] 的 subtotal 必须在 0—{maximum} 之间"
        subtotals[key] = float(subtotal)
    if scoring_status == "incomplete":
        if breakdown["timing"]["subtotal"] is not None:
            return "incomplete 状态的 timing.subtotal 必须为 null"
        if breakdown.get("total") is not None:
            return "incomplete 状态的 total 必须为 null"
        return None
    total = breakdown.get("total")
    if not isinstance(total, (int, float)) or isinstance(total, bool):
        return "完整评分的 score_breakdown 必须包含数值 total"
    if not math.isfinite(float(total)) or not 0 <= total <= 80:
        return "score_breakdown['total'] 必须是 0—80 的有限数值"
    expected_total = (subtotals["fundamentals"] or 0) + (subtotals["timing"] or 0)
    if not math.isclose(float(total), expected_total, abs_tol=1e-9):
        return "score_breakdown['total'] 必须等于基本面与择时 subtotal 之和"
    if score is not None and not math.isclose(float(total), float(score), abs_tol=1e-9):
        return "score_breakdown['total'] 必须与已写入的综合得分一致"
    return None


def _contains_non_finite_number(value: object) -> bool:
    if isinstance(value, dict):
        return any(_contains_non_finite_number(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_non_finite_number(item) for item in value)
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and not math.isfinite(float(value))
    )


def _score_breakdown_matches_analysis(
    raw_breakdown: str | None,
    *,
    framework: str | None,
    scoring_status: str,
    score: int | None,
) -> bool:
    """Return whether a persisted breakdown still satisfies its analysis row."""
    if raw_breakdown is None:
        return True
    try:
        breakdown = json.loads(raw_breakdown)
    except (TypeError, json.JSONDecodeError):
        return False
    return (
        _validate_score_breakdown_schema(
            breakdown,
            framework=framework,
            scoring_status=scoring_status,
            score=score,
        )
        is None
    )


def cmd_set_score_breakdown(args: list[str]) -> None:
    """写入今日各维度分项得分（JSON格式，统一嵌套 schema）。需先执行 set-analysis。"""
    if len(args) < 2:
        print("错误：需要参数 <代码> '<JSON>'", file=sys.stderr)
        sys.exit(1)
    code = args[0]
    try:
        breakdown = json.loads(args[1])
    except json.JSONDecodeError as e:
        print(f"JSON解析错误: {e}", file=sys.stderr)
        sys.exit(1)
    today = domain.cst_today()
    with db.db_session() as conn:
        analysis = conn.execute(
            """SELECT framework, COALESCE(scoring_status, 'complete'), score
               FROM analysis_results WHERE code=? AND date=?""",
            (code, today),
        ).fetchone()
        if analysis is None:
            print(
                f"未找到今日分析记录，请先执行 set-analysis（代码：{code}）",
                file=sys.stderr,
            )
            sys.exit(1)
        schema_error = _validate_score_breakdown_schema(
            breakdown,
            framework=analysis[0],
            scoring_status=analysis[1],
            score=analysis[2],
        )
        if schema_error:
            print(f"错误：{schema_error}", file=sys.stderr)
            sys.exit(1)
        updated = conn.execute(
            "UPDATE analysis_results SET score_breakdown=? WHERE code=? AND date=?",
            (json.dumps(breakdown, ensure_ascii=False), code, today),
        ).rowcount
        conn.commit()
    if updated:
        print(f"分项得分已记录：{code} → {breakdown}")
