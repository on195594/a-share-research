"""Keep repository-local modules importable under both pytest entry points."""

from __future__ import annotations

import sys
from pathlib import Path


MONITOR_ROOT = Path(__file__).resolve().parents[3] / "a_share_research/policy/v0/guide" / "a-stock-monitor"
if str(MONITOR_ROOT) not in sys.path:
    sys.path.insert(0, str(MONITOR_ROOT))
if str(MONITOR_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(MONITOR_ROOT / "scripts"))
