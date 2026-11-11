"""One bounded, model-free acquisition in a worker-owned subprocess.

The caller supplies a server-held snapshot, never an untrusted client snapshot. This
module does not open a writable application database or publish a material revision.
"""
from __future__ import annotations

from contextlib import closing, redirect_stdout
from copy import deepcopy
import json
import math
import os
import re
import sqlite3
import sys
import time

from a_share_research import db, evidence, material as materials, stock_codes
from a_share_research.research_tools import ResearchTools, TOOLS

ARGUMENT_BYTES = 65_536


class InputError(ValueError):
    """A public, credential-free boundary error safe to return to the caller."""


def _json(value) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, dict) and all(type(key) is str for key in value):
        for item in value.values():
            _json(item)
        return
    if isinstance(value, list):
        for item in value:
            _json(item)
        return
    raise InputError('参数须为有效 JSON，数字须有限且对象键须为文字')


def _validate(value, schema: dict, path: str) -> None:
    """Validate exactly the schema vocabulary used by the frozen TOOLS registry."""
    supported = {'type', 'properties', 'required', 'additionalProperties', 'enum',
                 'minimum', 'maximum', 'exclusiveMinimum', 'minLength', 'maxLength', 'pattern'}
    if schema.keys() - supported:
        raise InputError('工具参数合同包含尚不支持的约束，须先更新适配器')
    types = schema.get('type', [])
    types = [types] if isinstance(types, str) else types
    matches = {'object': isinstance(value, dict), 'string': type(value) is str,
               'integer': type(value) is int, 'number': type(value) in (int, float),
               'boolean': type(value) is bool, 'null': value is None,
               'array': isinstance(value, list)}
    if types and not any(matches.get(kind, False) for kind in types):
        raise InputError(f'{path} 类型不符合工具合同')
    if 'enum' in schema and value not in schema['enum']:
        raise InputError(f'{path} 不在工具允许的选项中')
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        if set(schema.get('required', [])) - value.keys():
            raise InputError(f'{path} 缺少必需字段')
        if schema.get('additionalProperties') is False and value.keys() - properties.keys():
            raise InputError(f'{path} 含未开放字段')
        for key, item in value.items():
            if key in properties:
                _validate(item, properties[key], f'{path}.{key}')
    if type(value) is str:
        if len(value) < schema.get('minLength', 0) or len(value) > schema.get('maxLength', math.inf):
            raise InputError(f'{path} 长度超出工具限制')
        if 'pattern' in schema and re.search(schema['pattern'], value) is None:
            raise InputError(f'{path} 格式不符合工具合同')
    if type(value) in (int, float):
        if value < schema.get('minimum', -math.inf) or value > schema.get('maximum', math.inf):
            raise InputError(f'{path} 超出工具允许范围')
        if 'exclusiveMinimum' in schema and value <= schema['exclusiveMinimum']:
            raise InputError(f'{path} 须大于工具下限')


def validate_call(name: str, args: dict) -> None:
    """Reject unknown tools, extra fields, coercions and nonfinite arguments before I/O."""
    spec = next((tool for tool in TOOLS if tool['name'] == name), None) if type(name) is str else None
    if spec is None:
        raise InputError('未知取证工具；请使用服务端工具目录')
    _json(args)
    if len(json.dumps(args, ensure_ascii=False).encode()) > ARGUMENT_BYTES:
        raise InputError('取证参数不得超过 64 KiB')
    _validate(args, spec['parameters'], name)


def _held(old, new, path: str) -> None:
    """Permit additive values, but never change or remove a previously held fact."""
    if isinstance(old, dict) and isinstance(new, dict):
        for key, value in old.items():
            if key not in new:
                raise evidence.EvidenceError(f'{path} 丢失既有证据字段')
            _held(value, new[key], path)
    elif old != new:
        raise evidence.EvidenceError(f'{path} 与既有证据冲突')


def validate_extension(before: materials.Material, after: materials.Material) -> None:
    """Check source/page/row identity across a tool result before a revision can be saved."""
    for field in ('code', 'name', 'market_date'):
        if getattr(before, field) != getattr(after, field):
            raise evidence.EvidenceError('取证不得改写基础材料身份或日期')
    old_sources, new_sources = before.source_registry(), after.source_registry()
    for source, old in old_sources.items():
        new = new_sources.get(source)
        if new is None or evidence.identity_conflict(old['document'], new['document']):
            raise evidence.EvidenceError(f'{source} 与既有文档身份冲突')
        for field in ('pages', 'raw_pages'):
            _held(old[field], new[field], f'{source}.{field}')
    rows = {row['id']: row for row in after.table}
    if len(rows) != len(after.table):
        raise evidence.EvidenceError('表格证据 ID 重复')
    for row in before.table:
        if row['id'] not in rows:
            raise evidence.EvidenceError('取证丢失既有表格证据')
        _held(row, rows[row['id']], row['id'])


def execute(request: dict) -> dict:
    """Gather a company or run one whitelisted call; raise without publishing on any failure."""
    if not isinstance(request, dict):
        raise InputError('取证请求须为对象')
    action = request.get('action')
    if action == 'open':
        if set(request) != {'action', 'code'}:
            raise InputError('首次取证仅接受 action 和 code')
        code = request['code']
        if type(code) is not str or not stock_codes.KEY_RE.fullmatch(code):
            raise InputError('公司代码须为六位数字')
        material = materials.gather(code)
        if stock_codes.normalize_key(material.code) != code or material.doc.code != material.code:
            raise evidence.EvidenceError('采集材料与请求公司不一致')
        material.source_registry()
        return {'material': material.snapshot(), 'result': {'company_key': code}}
    if action != 'acquire' or set(request) != {'action', 'material', 'tool', 'arguments'}:
        raise InputError('补充取证须提供 action、material、tool、arguments')
    name, args = request['tool'], request['arguments']
    validate_call(name, args)
    # from_snapshot retains nested structures, so isolate candidate writes from the caller.
    material = materials.Material.from_snapshot(deepcopy(request['material']))
    before = deepcopy(material)
    material.extra.setdefault('catalog', {'D0': material.doc.info()})
    material.extra.setdefault('sources', {})
    material.extra.setdefault('gaps', [])
    started = time.monotonic()
    toolkit = ResearchTools(material, None)
    # Each durable operation has a fresh process; recover the frozen session's peer cache.
    # These rows retain their original retrieved_at and are reuse, not a financial refresh.
    for row in material.table:
        match = re.fullmatch(r'P([0-9]{6})\..+', row['id'])
        if match and match[1] != stock_codes.normalize_key(material.code):
            toolkit.financial_cache.setdefault(match[1], []).append(row)
    if name == 'research_methods' and 'method_catalog' not in material.extra:
        # Read history only; db.connect would run migrations and is deliberately forbidden here.
        with closing(sqlite3.connect((db.DATA / 'app.db').resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            toolkit.conn = conn
            result = toolkit.run(name, args)
    else:
        try:
            result = toolkit.run(name, args)
        except ValueError as exc:
            if name not in ('calculate', 'valuation'):
                raise
            # These tools only compute from held evidence, without provider I/O.
            raise InputError(safe_detail(str(exc))) from None
    validate_extension(before, material)
    material.extra.setdefault('tools', []).append(
        {'tool': name, 'arguments': deepcopy(args), 'result': result, 'error': False,
         'stage': 'external', 'seconds': round(time.monotonic() - started, 3)})
    return {'material': material.snapshot(), 'result': result}



def safe_detail(message: str) -> str:
    """Bound a deterministic validation error and remove credential-like content."""
    for key, value in os.environ.items():
        if value and (key.upper().endswith('_KEY') or any(
                part in key.upper() for part in ('TOKEN', 'PASSWORD', 'SECRET', 'CREDENTIAL', 'API_KEY'))):
            message = message.replace(value, '[redacted]')
    message = re.sub(r'(?i)\b[a-z][a-z0-9+.-]*://[^\s；，]+', '[redacted-url]', message)
    message = re.sub(r'(?i)\b(?:token|api[_-]?key|password|secret|authorization)\s*[:=]\s*(?:bearer\s+)?[^\s；，]+',
                     '[redacted]', message)
    message = re.sub(r'\b(?:sk-|asr_)[A-Za-z0-9_-]+', '[redacted]', message)
    message = ''.join(char if char.isprintable() else ' ' for char in message).strip()
    return message[:1024] or '计算参数不符合工具合同，请核对参数与可用证据'


def main() -> None:
    """Worker-internal stdin/stdout protocol; process lifetime is enforced by the supervisor."""
    try:
        request = json.load(sys.stdin, parse_constant=lambda _: (_ for _ in ()).throw(InputError('JSON 数字须有限')))
        # Dependencies may write informational output; stdout is reserved for the single result.
        with redirect_stdout(sys.stderr):
            result = execute(request)
        output = json.dumps(result, ensure_ascii=False, allow_nan=False)
    except Exception as exc:
        # Provider errors may contain request URLs or credential-bearing responses.
        if isinstance(exc, InputError):
            sys.stdout.write(json.dumps({'error': {'code': 'invalid_arguments', 'message': safe_detail(str(exc))}},
                                        ensure_ascii=False) + '\n')
        else:
            sys.stderr.write(f'取证失败（{type(exc).__name__}）；未生成材料版本\n')
        raise SystemExit(1) from None
    sys.stdout.write(output + '\n')


if __name__ == '__main__':
    main()
