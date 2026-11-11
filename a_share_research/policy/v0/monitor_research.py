"""Optional Research card projection and review prompts; no valuation formulas."""

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import requests

from a_share_research.policy.v0 import boards, monitor_contract, research_client
from a_share_research.policy.v0.trading_costs import COMMISSION_RATE, STAMP_TAX_RATE

SHANGHAI = ZoneInfo("Asia/Shanghai")


def fetch_cards(*, skip: bool = False) -> dict:
    if skip:
        return {"status": "skipped"}
    try:
        # Optional research must fail promptly so core risk checks can continue.
        payload = research_client.request_json(
            "/api/cards", new_endpoint=True, timeout=(2, 3)
        )
        monitor_contract.validate_research_cards(payload)
    except research_client.ResearchNotConfigured:
        return {"status": "not_configured"}
    except requests.RequestException as exc:
        # Transport exception text may contain credentials or response bodies.
        return {
            "status": "unavailable",
            "reason": f"Research 连接失败：{type(exc).__name__}",
        }
    except (OSError, RuntimeError) as exc:
        return {
            "status": "unavailable",
            "reason": f"Research 配置读取失败：{type(exc).__name__}",
        }
    except ValueError as exc:
        return {"status": "unavailable", "reason": str(exc)[:700]}
    return {"status": "ok", "cards": payload["cards"], "missing": payload["missing"]}


def _row(code: str, card: dict | None) -> dict:
    current = card["current"] if card else {}
    valuation = current.get("valuation") or {}
    returns = valuation.get("annual_returns") or {}
    return {
        "code": code,
        "card": card,
        "card_status": card["status"] if card else None,
        "verdict": card.get("verdict") if card else None,
        "report_id": card["report_id"] if card else None,
        "completed_at": card.get("completed_at") if card else None,
        "close": current.get("last_close"),
        "close_date": current.get("last_close_date"),
        "zone": current.get("current_zone"),
        "neutral_return": returns.get("中性"),
        "conservative_return": returns.get("保守"),
        "optimistic_return": returns.get("乐观"),
        "note": current.get("note") or valuation.get("reason") or current.get("reason"),
        # Research's caveat for rolled returns (e.g. unverified cumulative dividends).
        "assumption": valuation.get("assumption"),
        **_stance(card, valuation),
    }


def _stance(card: dict | None, valuation: dict) -> dict:
    """Research V4 stance and the v2 probability-weighted return at the current close (0.1.35)."""
    frozen = (card or {}).get("stance_calc") or {}
    v2 = valuation.get("v2") or {}
    return {
        "stance_action": frozen.get("action"),
        "stance_add_trigger": monitor_contract.stance_add_trigger(frozen),
        "stance_price": frozen.get("price"),
        "stance_valid_until": frozen.get("valid_until"),
        "expected_return": v2.get("annual_return"),
        "v2_zone": v2.get("zone"),
    }


def _opportunity(row: dict, snapshot_date: date) -> str | None:
    """Why a researched candidate is worth acting on now, or None. Research owns every number."""
    if (
        row["card_status"] != "有效"
        or row["verdict"] != "值得继续研究"
        or row["stance_action"] == "回避"
    ):
        return None
    if row["v2_zone"] == "测算支持区" and row["expected_return"] is not None:
        return f"按情景概率加权的期望年化 {row['expected_return']:.2%}，处于测算支持区（≥10%）"
    if (
        row["stance_action"] == "等价格"
        and row["stance_price"] is not None
        and row["close"] is not None
        and row["close"] <= row["stance_price"]
        and row["close_date"]
        and row["stance_valid_until"]
        and row["close_date"] <= row["stance_valid_until"]
        and snapshot_date.isoformat() <= row["stance_valid_until"]
    ):
        return f"收盘价 {row['close']:g} 已不高于研究立场的等待价 {row['stance_price']:.2f}（有效至 {row['stance_valid_until']}）"
    if row["stance_action"] in {"现在分批", "等证据"} and _fresh(
        row["completed_at"], snapshot_date
    ):
        return "研究立场为现在分批（报告 7 日内）"
    return None


def _checkpoint_detail(row: dict) -> str:
    trigger = row["stance_add_trigger"]
    if trigger is None:
        return ""
    # Research's T.op_cash financial table is denominated in 100 million CNY.
    metric = "经营现金流" if trigger["metric"] == "T.op_cash" else trigger["metric"]
    unit = "亿元" if trigger["metric"] == "T.op_cash" else "（单位与累计口径同财务表）"
    op = {">=": "≥", "<=": "≤"}[trigger["op"]]
    # Shortest round-trip text so the shown threshold equals the frozen value exactly.
    value = trigger["value"]
    shown = str(int(value)) if float(value).is_integer() else repr(float(value))
    return (
        f"动作上限为首批（position-size tranches[0]，不足一手则为 0）；加仓检查点："
        f"{trigger['period']} {metric} {op} {shown} {unit}（参考，不阻止首批）；"
        "目标期间披露达到阈值且更新的有效研究确认后，才按立场加后续批次；未达到则复核是否减仓；"
        "未披露不视为未达到。"
    )


def _caution_detail(card: dict) -> str:
    """Display Research's warnings without changing eligibility or recomputing its prices."""
    cautions = monitor_contract.research_cautions(card)
    parts = []
    if cautions.get("report_boundary") is True:
        parts.append(
            "报告形成时为边界立场：同样材料重跑可能得出相反立场，不代表稳定的买入优势"
        )
    if cautions.get("current_boundary") is True:
        parts.append("当前价格处于边界：现价与期望回报门槛价相差不超过5%")
    if cautions.get("open_questions"):
        parts.append(
            "已通过复核，但以下问题尚未答完："
            + "；".join(
                f"[{q['id']} / {q['where']}] {q['problem']}"
                for q in cautions["open_questions"]
            )
        )
    unknown = []
    if cautions.get("report_boundary") is None:
        unknown.append("报告边界")
    if (
        card["current"].get("valuation") is not None
        and cautions.get("current_boundary") is None
    ):
        unknown.append("当前边界")
    if "open_questions" not in cautions:
        unknown.append("未答问题记录")
    if unknown:
        parts.append(
            "/".join(unknown) + "未提供或不可判定，不代表判断稳定或问题已完整回答"
        )
    if not parts:
        return ""
    return (
        f"；Research 风险提示（{card['code']} 报告 #{card['report_id']}）："
        + "；".join(parts)
    )


def _evidence(row: dict) -> dict:
    return {
        key: row[key] for key in ("code", "report_id", "close_date", "close")
    } | monitor_contract.research_cautions(row["card"])


def summarize(
    result: dict,
    holding_codes: list[str],
    snapshot_date: date,
    untradable_boards: list[str] | None = None,
) -> tuple[dict, list, list]:
    """Return research, informational prompts, and thesis-only review escalations.

    Candidates on a board the account cannot trade stay in research but are never named by
    buy/switch advice (docs/specs/2026-11-04-untradable-boards-spec.md).
    """
    cards = {card["code"]: card for card in result.get("cards", [])}
    missing = result.get("missing", [])
    research = {
        "status": result["status"],
        "holdings": [
            _row(code, cards.get(code)) for code in dict.fromkeys(holding_codes)
        ],
        "candidates": [
            _row(code, cards.get(code))
            for code in sorted(
                {card["code"] for card in cards.values() if card["origin"] == "l5"}
                | {item["code"] for item in missing if item["origin"] == "l5"}
            )
            if code not in holding_codes
        ],
        "missing": [item["code"] for item in missing],
        "opportunity_costs": [],
    }
    if result.get("reason"):
        research["reason"] = result["reason"]
    prompts, escalations = [], []
    if result["status"] == "unavailable":
        prompts.append(
            {
                "code": None,
                "kind": "research_unavailable",
                "detail": f"论点卡状态未知：{result['reason']}",
                "evidence": {},
            }
        )
    mismatches = []
    blocked = [
        c["code"]
        for c in research["candidates"]
        if not boards.advisable(c["code"], untradable_boards)
    ]
    if blocked:
        mismatches.append(
            f"{'、'.join(blocked)} 所属板块（{'、'.join(dict.fromkeys(boards.LABELS[boards.board(c)] for c in blocked))}）账户无交易权限（风险政策 untradable_boards），"
            "保留研究，不作机会提示或换仓候选"
        )
    advisable = [c for c in research["candidates"] if c["code"] not in blocked]
    for holding in research["holdings"]:
        code, card = holding["code"], holding["card"]
        if holding["card_status"] == "待复核":
            events = (
                "；".join(
                    f"{event.get('title') or '待复核事件'}（{event.get('kind') or event.get('type') or '未分类'}）"
                    for event in card["pending_events"]
                )
                or "Research 论点卡待复核"
            )
            escalations.append(
                {
                    "code": code,
                    "reason_code": "research_thesis_review",
                    "candidate": "review",
                    "detail": f"报告 {card['report_id']}：{events}；区间暂停解读，按日常监控与 L3 资料复核",
                }
            )
            continue
        if holding["card_status"] != "有效":
            continue
        if holding["zone"] == "测算透支区":
            prompts.append(
                {
                    "code": code,
                    "kind": "valuation_stretched",
                    "detail": f"{code} 收盘价 {holding['close']} 处于测算透支区，复核经营假设与退出倍数"
                    + _caution_detail(card),
                    "evidence": _evidence(holding),
                }
            )
        if holding["neutral_return"] is None:
            continue
        best = None
        for candidate in advisable:
            if (
                candidate["card_status"] != "有效"
                or candidate["verdict"] != "值得继续研究"
                or candidate["neutral_return"] is None
            ):
                continue
            horizon = (candidate["card"]["current"].get("valuation") or {}).get(
                "horizon"
            )
            if horizon is None or horizon <= 0:
                continue
            # Candidates that cannot reach the threshold even before costs need no date check.
            if candidate["neutral_return"] - holding["neutral_return"] < 0.03:
                continue
            if not holding["close_date"] or not candidate["close_date"]:
                mismatches.append(
                    f"{code}/{candidate['code']} 缺少收盘日，未比较机会成本"
                )
                continue
            days = abs(
                (
                    date.fromisoformat(holding["close_date"])
                    - date.fromisoformat(candidate["close_date"])
                ).days
            )
            if days > 3:
                mismatches.append(
                    f"{code}/{candidate['code']} 收盘日相差 {days} 个自然日（超过3日），未比较机会成本"
                )
                continue
            # Compare the candidate with none of its frozen dividends remaining against
            # the holding with all remaining. This is sensitivity, not a return guarantee.
            candidate_low = candidate["card"]["current"]["valuation"].get(
                "neutral_return_without_dividends"
            )
            holding_low = (holding["card"]["current"].get("valuation") or {}).get(
                "neutral_return_without_dividends"
            )
            if (
                candidate_low is None
                or holding_low is None
                or not -1 <= candidate_low <= candidate["neutral_return"]
                or not -1 <= holding_low <= holding["neutral_return"]
            ):
                mismatches.append(
                    f"{code}/{candidate['code']} 缺少适用的分红敏感性值，未比较机会成本"
                )
                continue
            # Decimal avoids excluding the exact 3pp boundary through float subtraction.
            cost = (
                Decimal(str(STAMP_TAX_RATE)) + 2 * Decimal(str(COMMISSION_RATE))
            ) / Decimal(str(horizon))
            advantage = (
                Decimal(str(candidate_low))
                - Decimal(str(holding["neutral_return"]))
                - cost
            )
            if advantage < Decimal("0.03"):
                # Near misses keep both bases visible so dividend-heavy candidates are not hidden.
                nominal = (
                    Decimal(str(candidate["neutral_return"]))
                    - Decimal(str(holding["neutral_return"]))
                    - cost
                )
                mismatches.append(
                    f"{code}/{candidate['code']} 扣费后优势未稳健达到3个百分点"
                    f"（计分红 {nominal * 100:.2f} 个百分点，候选不计累计分红 {advantage * 100:.2f} 个百分点）；"
                    "现金流未核对，不提示换仓优势"
                )
            if advantage >= Decimal("0.03") and (best is None or advantage > best[0]):
                best = advantage, cost, candidate
        if best:
            advantage, cost, candidate = best
            research["opportunity_costs"].append(
                {
                    "holding": _evidence(holding),
                    "candidate": _evidence(candidate),
                    "holding_neutral_return": holding["neutral_return"],
                    "candidate_neutral_return": candidate["neutral_return"],
                    "candidate_horizon": candidate["card"]["current"]["valuation"][
                        "horizon"
                    ],
                    "annual_switch_cost": float(cost),
                    "net_advantage": float(advantage),
                    "candidate_return_without_dividends": candidate["card"]["current"][
                        "valuation"
                    ]["neutral_return_without_dividends"],
                    "comparison_basis": "候选不计累计分红、持仓计全部分红；仅非负冻结分红假设内的敏感性比较，非真实收益上下界",
                }
            )
    grouped = {}
    for comparison in research["opportunity_costs"]:
        grouped.setdefault(comparison["candidate"]["code"], []).append(comparison)
    for code, comparisons in grouped.items():
        compared_codes = [code, *(item["holding"]["code"] for item in comparisons)]
        if not any(
            _fresh(cards[c].get("completed_at"), snapshot_date) for c in compared_codes
        ):
            continue
        candidate = cards[code]
        names = "/".join(
            cards[item["holding"]["code"]].get("name") or item["holding"]["code"]
            for item in comparisons
        )
        maximum = max(item["net_advantage"] for item in comparisons)
        caveats = dict.fromkeys(
            caveat
            for c in compared_codes
            if (
                caveat := (cards[c]["current"].get("valuation") or {}).get("assumption")
            )
        )
        prompts.append(
            {
                "code": code,
                "kind": "opportunity_cost",
                "detail": (
                    f"{candidate.get('name') or code} {code} 中性年化 "
                    f"{comparisons[0]['candidate_neutral_return']:.2%}，按候选不计累计分红、持仓计全部分红，扣费后比 {names} "
                    f"高 ≥3 个百分点（最大差 {maximum * 100:.4f} 个百分点），仅冻结分红敏感性、非真实收益界限；情景未经记分，仅提示复核"
                    + "".join(
                        _caution_detail(cards[c]) for c in dict.fromkeys(compared_codes)
                    )
                    + "".join(f"；Research 注：{caveat}" for caveat in caveats)
                ),
                "evidence": {"comparisons": comparisons},
            }
        )
    for candidate in advisable:
        if reason := _opportunity(candidate, snapshot_date):
            name = candidate["card"].get("name") or candidate["code"]
            prompts.append(
                {
                    "code": candidate["code"],
                    "kind": "research_opportunity",
                    "detail": (
                        f"{name} {candidate['code']}：{reason}；研究立场 {candidate['stance_action'] or '未记录'}。"
                        + _checkpoint_detail(candidate)
                        + "情景未经记分，按 position-size 定仓后再决定，不构成已形成的交易动作"
                        + _caution_detail(candidate["card"])
                    ),
                    "evidence": _evidence(candidate)
                    | {
                        key: candidate[key]
                        for key in (
                            "stance_action",
                            "stance_price",
                            "stance_valid_until",
                            "stance_add_trigger",
                            "expected_return",
                            "v2_zone",
                        )
                    },
                }
            )
    if mismatches:
        research["reason"] = "；".join(mismatches)
    return research, prompts, escalations


def _fresh(completed_at: object, snapshot_date: date) -> bool:
    try:
        completed = datetime.fromisoformat(completed_at)
    except (TypeError, ValueError):
        return False
    # Research timestamps are Shanghai dates; naive values are treated as already local.
    if completed.tzinfo is not None:
        completed = completed.astimezone(SHANGHAI)
    completed_date = completed.date()
    return 0 <= (snapshot_date - completed_date).days <= 7
