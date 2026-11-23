#!/usr/bin/env python3
"""Deliver the scheduled disclosure scan or weekly digest through Hermes."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ashare_schedule import scheduled_main


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else 'scan'
    if len(sys.argv) > 2 or mode not in ('scan', 'weekly'):
        sys.stderr.write('usage: a-share-advisor.py [scan|weekly]\n')
        return 2
    return scheduled_main('advisor_' + mode)


if __name__ == '__main__':
    raise SystemExit(main())
