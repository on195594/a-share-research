#!/usr/bin/env python3
"""Deliver the scheduled stop_alert receipt through Hermes."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ashare_schedule import policy_main


def main() -> int:
    return policy_main('stop_alert')


if __name__ == '__main__':
    raise SystemExit(main())
