"""Strict JSON parsing, canonical encoding and SHA-256 digests for shared records."""
from __future__ import annotations

import hashlib
import json
import math


class DuplicateJSONKey(ValueError):
    def __init__(self, key: str):
        super().__init__(f'JSON 字段 {key!r} 重复')


class NonFiniteJSONNumber(ValueError):
    def __init__(self, token: str):
        super().__init__(f'JSON 数值 {token} 必须为有限数')
        self.token = token


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJSONKey(key)
        result[key] = value
    return result


def _finite_float(token: str) -> float:
    number = float(token)
    if not math.isfinite(number):
        raise NonFiniteJSONNumber(token)
    return number


def loads_strict(raw: str | bytes):
    """Decode JSON with unique object keys and finite floating-point values."""
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_finite_float, parse_float=_finite_float)


def canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value: dict) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()

