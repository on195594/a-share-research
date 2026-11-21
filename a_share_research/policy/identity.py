"""Content identity of the installed policy rules, guides and parameters."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re

from a_share_research import gate


ROOT = Path(__file__).absolute().parents[2]
GUIDE = 'a_share_research/policy/v0/guide/'
SCHEMA_VERSION = 3
RULES = 'a_share_research/policy/v0/RULES.md'
FIELDS = {'schema_version', 'policy', 'parameters', 'rules_hash',
          'guide_version', 'files', 'guides', 'identity'}


class IdentityError(ValueError):
    """The manifest is invalid or the policy files cannot be read."""


def _json_value(value: object) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise IdentityError('政策身份只能包含有限数字和标准 JSON 类型，键须为字符串')


def _copy(value: dict) -> dict:
    try:
        _json_value(value)
        return json.loads(gate.canonical(value).encode('utf-8'))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise IdentityError('政策身份包含无效 JSON 内容') from exc


def _path(value: object) -> str:
    if (type(value) is not str or not value or '\\' in value
            or PurePosixPath(value).is_absolute() or value != PurePosixPath(value).as_posix()
            or any(part in ('', '.', '..') for part in value.split('/'))):
        raise IdentityError('政策文件路径须为规范的相对路径')
    return value


def _read(relative: str) -> bytes:
    path = ROOT / relative
    for component in (path, *path.parents):
        if component.is_symlink():
            raise IdentityError('政策文件及其父目录不能是符号链接')
        if component == ROOT:
            break
    return path.read_bytes()


def _rules(rule_hash, guide_hashes):
    return {RULES: rule_hash, **{path: sha for path, sha in guide_hashes.items() if path.endswith('.json')}}


def _snapshot() -> dict:
    guides = {}
    guide_hashes = {}

    def unreadable(error):
        raise error

    try:
        rule_hash = hashlib.sha256(_read(RULES)).hexdigest()
        for directory, dirs, names in (ROOT / GUIDE).walk(on_error=unreadable):
            if '__pycache__' in dirs:
                dirs.remove('__pycache__')
            for name in names:
                path = directory / name
                if path.suffix in ('.pyc', '.pyo'):
                    continue
                relative = path.relative_to(ROOT).as_posix()
                data = _read(relative)
                guides[relative[len(GUIDE):]] = data.decode('utf-8')
                guide_hashes[relative] = hashlib.sha256(data).hexdigest()
    except (OSError, UnicodeError) as exc:
        raise IdentityError('无法读取政策规则或 UTF-8 指南') from exc
    if not guides:
        raise IdentityError('政策指南缺失')
    return {'files': {'rules': _rules(rule_hash, guide_hashes), 'guides': guide_hashes}, 'guides': guides}


def installed(manifest: dict) -> bool:
    try:
        return manifest['schema_version'] == SCHEMA_VERSION and manifest['files'] == _snapshot()['files']
    except IdentityError:
        return False


def build(parameters: dict) -> dict:
    """Capture the rules and guides with explicit account policy parameters."""
    if type(parameters) is not dict or not parameters:
        raise IdentityError('政策参数须为非空 JSON 对象')
    snapshot = _snapshot()
    manifest = {'schema_version': SCHEMA_VERSION, 'policy': 'v0', 'parameters': _copy(parameters),
                'rules_hash': gate.digest(snapshot['files']['rules']),
                'guide_version': gate.digest(snapshot['files']['guides']), **snapshot}
    manifest['identity'] = 'policy:' + gate.digest(manifest)
    verify(manifest)
    return manifest


def verify(manifest: dict) -> None:
    """Verify the archived contents independently of the installed policy files."""
    value = _copy(manifest)
    if (type(value) is not dict or set(value) != FIELDS or type(value['schema_version']) is not int
            or value['schema_version'] != SCHEMA_VERSION or value['policy'] != 'v0'
            or type(value['parameters']) is not dict or not value['parameters']
            or type(value['files']) is not dict or set(value['files']) != {'rules', 'guides'}
            or type(value['guides']) is not dict or not value['guides']):
        raise IdentityError('政策身份结构不支持')
    files = value['files']
    guide_hashes = {}
    for relative, content in value['guides'].items():
        _path(relative)
        if type(content) is not str:
            raise IdentityError('指南须为 UTF-8 文本')
        guide_hashes[GUIDE + relative] = hashlib.sha256(content.encode('utf-8')).hexdigest()
    if files['guides'] != guide_hashes:
        raise IdentityError('指南正文哈希不一致')
    rules = files['rules']
    rule_hash = rules.get(RULES) if isinstance(rules, dict) else None
    if not isinstance(rule_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', rule_hash):
        raise IdentityError('规则索引哈希无效')
    if rules != _rules(rule_hash, guide_hashes):
        raise IdentityError('规则清单与指南规则不一致')
    if value['rules_hash'] != gate.digest(rules) or value['guide_version'] != gate.digest(guide_hashes):
        raise IdentityError('政策身份的分类哈希不一致')
    claimed = value.pop('identity')
    if claimed != 'policy:' + gate.digest(value):
        raise IdentityError('政策身份内容哈希不一致')
