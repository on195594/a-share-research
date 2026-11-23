#!/usr/bin/env python3
"""Deliver the scheduled monthly admission or hourly status through Hermes."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ashare_schedule import scheduled_main


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if len(sys.argv) > 2 or mode not in ('submit', 'status'):
        sys.stderr.write('usage: a-share-monthly.py [submit|status]\n')
        return 2
    return scheduled_main('monthly_' + mode)


if __name__ == '__main__':
    raise SystemExit(main())
