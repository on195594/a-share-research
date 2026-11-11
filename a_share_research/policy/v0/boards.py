"""A-share board by code. Pure; whether an account may trade a board is a risk-policy fact.

See docs/specs/2026-11-04-untradable-boards-spec.md.
"""

from __future__ import annotations

# Boards an account can lack permission for; the main boards (including ChiNext) are not modelled.
RESTRICTABLE = ("STAR", "BSE")
LABELS = {"STAR": "科创板", "BSE": "北交所", "MAIN": "主板/创业板"}


def board(code: str) -> str:
    if code.startswith("688"):
        return "STAR"
    if code.startswith(("4", "8", "920")):
        return "BSE"
    return "MAIN"


def advisable(code: str, untradable: object) -> bool:
    """Whether buy/add advice may name `code` under a policy's `untradable_boards` (None/absent = all)."""
    return board(code) not in (untradable or ())
