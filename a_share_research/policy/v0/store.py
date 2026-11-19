"""Shared helpers for the legacy fundamentals/quote cache tables.

The writers were retired with the P0-P2 gates (2026-11-15); ``commands_admin``
still reads the tables for ``watchlist`` and ``list``.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def safe_json_value(raw: str | None, expected_type: type, default):
    """Read legacy JSON without letting one corrupt row break batch commands."""
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        logger.warning("Ignoring malformed cached JSON value")
        return default
    if not isinstance(value, expected_type):
        logger.warning("Ignoring cached JSON value with unexpected type")
        return default
    return value
