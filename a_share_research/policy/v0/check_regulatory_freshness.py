#!/usr/bin/env python3
"""Fail when a deterministic regulatory rule needs review."""

from __future__ import annotations

from datetime import date, datetime
import sys

from a_share_research.policy.v0.risk_gates import (
    CST,
    REGULATORY_RULES,
    evaluate_rule_freshness,
)


def check(today: date | None = None) -> list[str]:
    current = today or datetime.now(CST).date()
    errors: list[str] = []
    for key, rule in sorted(REGULATORY_RULES.items()):
        status = evaluate_rule_freshness(rule, current)
        label = "/".join(key)
        if status != "current":
            errors.append(f"{label}: regulatory rule {status}")
            continue
        days = (date.fromisoformat(rule["review_after"]) - current).days
        if days < 30:
            print(f"warning: {label} regulatory review due in {days} days", file=sys.stderr)
    return errors


def main() -> int:
    errors = check()
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("regulatory freshness passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
