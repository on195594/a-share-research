"""Captured stance decisions and boundary inputs."""

import json
from copy import deepcopy
from pathlib import Path

FLIP_SAMPLES = json.loads((Path(__file__).resolve().parents[1] / "data/stance-flip-samples-20261107.json").read_text())
EVIDENCE = {'action': '等证据', 'text': '上半年经营现金流转负，先看下一期是否转正。',
            'cites': [{'ref': 'T.op_cash', 'period': '2026-06-30'}],
            'trigger': {'metric': 'T.op_cash', 'period': '2026-09-30', 'op': '>=', 'value': 0}}
ADD = {k: deepcopy(v) for k, v in EVIDENCE.items() if k != 'trigger'}
ADD.update(action='现在分批', add_trigger=deepcopy(EVIDENCE['trigger']))
