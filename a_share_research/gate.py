"""Canonical JSON and SHA-256 digests shared by account, policy and evidence records."""
from __future__ import annotations

import hashlib
import json


def canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value: dict) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()

