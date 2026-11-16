"""Structural checks on the monitor guide; its wording is frozen by the policy identity."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3] / "a_share_research/policy/v0/guide" / "a-stock-monitor"


def test_decision_table_rule_ids_and_priorities_are_unique():
    decisions = json.loads((ROOT / "references" / "decision-table.json").read_text(encoding="utf-8"))
    rules = decisions["rules"]
    assert decisions["schema_version"] == 1
    assert len({rule["id"] for rule in rules}) == len(rules)
    assert len({rule["priority"] for rule in rules}) == len(rules)


def test_all_routed_reference_files_exist():
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    paths = set(re.findall(r"`(references/[^`]+)`", skill))
    assert paths
    for relative in paths:
        assert (ROOT / relative).exists(), relative
