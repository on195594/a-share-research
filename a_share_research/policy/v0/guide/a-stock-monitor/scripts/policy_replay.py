#!/usr/bin/env python3
"""Replay heuristic exit thresholds on point-in-time daily snapshots.

CSV columns:
  date,close,valuation_percentile[,dividend_per_share]

Signals observed on day T are executed at day T+1 close to avoid same-bar lookahead.
This tool is an evaluation harness, not evidence that the defaults are optimal.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class Bar:
    day: date
    close: float
    valuation_percentile: float | None
    dividend_per_share: float = 0.0


@dataclass(frozen=True)
class Policy:
    tier1_gain: float = 0.25
    tier2_gain: float = 0.50
    tier2_valuation: float = 0.60
    valuation_exit: float = 0.85
    stop2_loss: float = 0.20
    fee_bps: float = 10.0


@dataclass(frozen=True)
class ReplayResult:
    total_return: float
    cagr: float
    max_drawdown: float
    turnover: float
    trades: int
    final_equity: float


def load_bars(path: Path) -> list[Bar]:
    rows: list[Bar] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            valuation = raw.get("valuation_percentile", "").strip()
            dividend = raw.get("dividend_per_share", "").strip()
            bar = Bar(
                day=date.fromisoformat(raw["date"]),
                close=float(raw["close"]),
                valuation_percentile=float(valuation) if valuation else None,
                dividend_per_share=float(dividend) if dividend else 0.0,
            )
            if bar.close <= 0:
                raise ValueError(f"{bar.day}: close must be positive")
            if bar.valuation_percentile is not None and not (
                0 <= bar.valuation_percentile <= 1
            ):
                raise ValueError(f"{bar.day}: valuation_percentile must be 0..1")
            rows.append(bar)
    rows.sort(key=lambda item: item.day)
    if len(rows) < 2:
        raise ValueError("at least two dated rows are required")
    if len({item.day for item in rows}) != len(rows):
        raise ValueError("duplicate dates are not allowed")
    return rows


def _metrics(
    equity_curve: list[float],
    start: date,
    end: date,
    turnover: float,
    trades: int,
) -> ReplayResult:
    initial = equity_curve[0]
    final = equity_curve[-1]
    peak = initial
    max_drawdown = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value / peak - 1)
    years = max((end - start).days / 365.25, 1 / 365.25)
    cagr = (final / initial) ** (1 / years) - 1
    return ReplayResult(
        total_return=final / initial - 1,
        cagr=cagr,
        max_drawdown=max_drawdown,
        turnover=turnover,
        trades=trades,
        final_equity=final,
    )


def replay(bars: list[Bar], policy: Policy) -> ReplayResult:
    """Replay Tier/valuation/stop exits with next-bar execution."""
    reference_cost = bars[0].close
    initial_shares = 1.0
    shares = initial_shares
    cash = 0.0
    fee_rate = policy.fee_bps / 10_000
    tier1_done = False
    tier2_done = False
    stop_done = False
    pending_fraction = 0.0
    turnover = 0.0
    trades = 0
    equity_curve = [reference_cost]

    for index, bar in enumerate(bars):
        if index > 0 and pending_fraction > 0 and shares > 0:
            sold = min(shares, initial_shares * pending_fraction)
            gross = sold * bar.close
            cash += gross * (1 - fee_rate)
            shares -= sold
            turnover += gross
            trades += 1
            pending_fraction = 0.0

        cash += shares * bar.dividend_per_share
        equity_curve.append(cash + shares * bar.close)
        if index == len(bars) - 1 or shares <= 0:
            continue

        valuation = bar.valuation_percentile
        if valuation is not None and valuation >= policy.valuation_exit:
            pending_fraction = shares / initial_shares
            continue
        if not stop_done and bar.close <= reference_cost * (1 - policy.stop2_loss):
            pending_fraction = 0.5
            stop_done = True
            continue
        if not tier1_done and bar.close >= reference_cost * (1 + policy.tier1_gain):
            pending_fraction = 1 / 3
            tier1_done = True
            continue
        if (
            tier1_done
            and not tier2_done
            and bar.close >= reference_cost * (1 + policy.tier2_gain)
            and valuation is not None
            and valuation >= policy.tier2_valuation
        ):
            pending_fraction = 1 / 3
            tier2_done = True

    return _metrics(
        equity_curve,
        bars[0].day,
        bars[-1].day,
        turnover / reference_cost,
        trades,
    )


def buy_and_hold(bars: list[Bar], fee_bps: float = 10.0) -> ReplayResult:
    shares = 1.0
    cash = 0.0
    curve = [bars[0].close]
    for bar in bars:
        cash += shares * bar.dividend_per_share
        curve.append(cash + shares * bar.close)
    return _metrics(curve, bars[0].day, bars[-1].day, 0.0, 0)


def grid_replay(bars: list[Bar], fee_bps: float) -> list[tuple[Policy, ReplayResult]]:
    results = []
    for tier1, stop2, valuation in itertools.product(
        (0.20, 0.25, 0.30),
        (0.15, 0.20, 0.25),
        (0.75, 0.85, 0.90),
    ):
        policy = Policy(
            tier1_gain=tier1,
            stop2_loss=stop2,
            valuation_exit=valuation,
            fee_bps=fee_bps,
        )
        results.append((policy, replay(bars, policy)))
    return sorted(
        results,
        key=lambda item: (
            item[1].total_return,
            item[1].max_drawdown,
            -item[1].turnover,
        ),
        reverse=True,
    )


def _pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--fee-bps", type=float, default=10.0)
    parser.add_argument("--grid", action="store_true")
    args = parser.parse_args()
    if not math.isfinite(args.fee_bps) or args.fee_bps < 0:
        parser.error("--fee-bps must be a non-negative finite number")
    bars = load_bars(args.csv_path)
    baseline = buy_and_hold(bars, args.fee_bps)
    current = replay(bars, Policy(fee_bps=args.fee_bps))
    print(
        "strategy,total_return,cagr,max_drawdown,turnover,trades\n"
        f"buy_hold,{_pct(baseline.total_return)},{_pct(baseline.cagr)},"
        f"{_pct(baseline.max_drawdown)},{baseline.turnover:.2f},{baseline.trades}\n"
        f"current_policy,{_pct(current.total_return)},{_pct(current.cagr)},"
        f"{_pct(current.max_drawdown)},{current.turnover:.2f},{current.trades}"
    )
    if args.grid:
        print(
            "\n\ntier1_gain,stop2_loss,valuation_exit,total_return,max_drawdown,turnover"
        )
        for policy, result in grid_replay(bars, args.fee_bps):
            print(
                f"{policy.tier1_gain:.2f},{policy.stop2_loss:.2f},"
                f"{policy.valuation_exit:.2f},{result.total_return:.6f},"
                f"{result.max_drawdown:.6f},{result.turnover:.4f}"
            )


if __name__ == "__main__":
    main()
