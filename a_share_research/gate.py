"""Mechanical submission gate; passing it never means independent review has passed."""
from __future__ import annotations

import hashlib
import json


def canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value: dict) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def check_draft(draft: dict, snapshot: dict) -> list[dict]:
    """Validate against server-owned evidence using the same checks as worker research."""
    from a_share_research.checking import check
    from a_share_research.material import Material
    if 'repair_rebuttals' in draft and not isinstance(draft['repair_rebuttals'], list):
        return [{'where': 'repair_rebuttals', 'problem': 'repair_rebuttals 必须是数组'}]
    return check(draft, Material.from_snapshot(snapshot))
