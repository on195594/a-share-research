"""Hermes script loading and captured-shape advisor receipts."""

import importlib.util
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "ops/hermes"


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SOURCE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RECEIPT = dict(version='research-v1', request_id='$request_id', id=7, scan_id=7, path='/scans/7')


def response(**changes):
    return dict(version='research-v1', id=7, status='done', summary={}, notify=[],
                requested_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()) | changes
