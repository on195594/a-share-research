#!/usr/bin/env python3
"""Deliver the scheduled daily_brief receipt through Hermes."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ashare_schedule import policy_main


def main() -> int:
    return policy_main('daily_brief')


if __name__ == '__main__':
    raise SystemExit(main())
