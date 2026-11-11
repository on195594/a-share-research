"""Content identity of the installed policy runtime, parameters, rules and guides.

The code scope deliberately includes every Python source in the application and
the dependency lock: shared implementation changes require a new explicit policy
registration too. This module never activates a policy or imports the v0 runtime.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re

from a_share_research import gate


ROOT = Path(__file__).absolute().parents[2]
GUIDE = 'a_share_research/policy/v0/guide/'
RULES = 'a_share_research/policy/v0/RULES.md'
FIELDS = {'schema_version', 'policy', 'code_hash', 'parameters', 'rules_hash',
          'guide_version', 'files', 'guides', 'identity'}


class IdentityError(ValueError):
    """The manifest is invalid or no longer describes the installed runtime."""


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


def _snapshot() -> dict:
    """Read the actual installation, excluding generated Python bytecode caches."""
    files: dict[str, dict[str, str]] = {'code': {}, 'rules': {}, 'guides': {}}
    guides: dict[str, str] = {}
    paths = ['uv.lock']
    package = ROOT / 'a_share_research'
    if package.is_symlink() or not package.is_dir():
        raise IdentityError('政策运行时目录不可用或是符号链接')
    def unreadable(error):
        raise error

    try:
        for directory, dirs, names in os.walk(package, followlinks=False, onerror=unreadable):
            if any((Path(directory) / name).is_symlink() for name in dirs):
                raise IdentityError('政策运行时目录不能含符号链接')
            dirs[:] = sorted(name for name in dirs if name != '__pycache__')
            for name in sorted(names):
                path = Path(directory) / name
                relative = path.relative_to(ROOT).as_posix()
                if path.is_symlink():
                    raise IdentityError('政策运行时文件不能是符号链接')
                if (path.suffix == '.py' or relative == RULES
                        or relative.startswith(GUIDE) and path.suffix not in ('.pyc', '.pyo')):
                    paths.append(relative)
        for relative in sorted(paths):
            data = _read(relative)
            digest = hashlib.sha256(data).hexdigest()
            if relative.endswith('.py') or relative == 'uv.lock':
                files['code'][relative] = digest
            if relative == RULES or relative.startswith(GUIDE) and relative.endswith('.json'):
                files['rules'][relative] = digest
            if relative.startswith(GUIDE):
                files['guides'][relative] = digest
                guides[relative[len(GUIDE):]] = data.decode('utf-8')
    except (OSError, UnicodeError) as exc:
        raise IdentityError('无法读取完整的政策运行时和 UTF-8 指南') from exc
    if RULES not in files['rules'] or not guides or not files['code']:
        raise IdentityError('政策代码、规则或指南缺失')
    return {'files': files, 'guides': guides}


def build(parameters: dict) -> dict:
    """Capture this installation with explicit parameters; never infer account policy."""
    if type(parameters) is not dict or not parameters:
        raise IdentityError('政策参数须为非空 JSON 对象')
    parameters = _copy(parameters)
    snapshot = _snapshot()
    manifest = {'schema_version': 1, 'policy': 'v0', 'parameters': parameters,
                'code_hash': gate.digest(snapshot['files']['code']),
                'rules_hash': gate.digest(snapshot['files']['rules']),
                'guide_version': gate.digest(snapshot['files']['guides']), **snapshot}
    manifest['identity'] = 'policy:' + gate.digest(manifest)
    verify(manifest)
    return manifest


def verify(manifest: dict) -> None:
    """Validate archived content and internal digests, without requiring current code."""
    value = _copy(manifest)
    if (type(value) is not dict or set(value) != FIELDS or type(value['schema_version']) is not int
            or value['schema_version'] != 1 or value['policy'] != 'v0'
            or type(value['parameters']) is not dict or not value['parameters']
            or type(value['files']) is not dict or set(value['files']) != {'code', 'rules', 'guides'}
            or type(value['guides']) is not dict or not value['guides']):
        raise IdentityError('政策身份结构不支持')
    files = value['files']
    for category, entries in files.items():
        if type(entries) is not dict or not entries:
            raise IdentityError('政策文件清单不能为空')
        for path, digest in entries.items():
            _path(path)
            if type(digest) is not str or re.fullmatch(r'[0-9a-f]{64}', digest) is None:
                raise IdentityError('政策文件哈希格式无效')
            allowed = (path == 'uv.lock' or path.startswith('a_share_research/') and path.endswith('.py')) if category == 'code' else (
                path == RULES or path.startswith(GUIDE) and path.endswith('.json')) if category == 'rules' else path.startswith(GUIDE)
            if not allowed:
                raise IdentityError('政策文件不属于声明的分类')
    if 'uv.lock' not in files['code'] or RULES not in files['rules']:
        raise IdentityError('政策身份缺少依赖锁或规则索引')
    if files['rules'] != {RULES: files['rules'][RULES], **{
            path: sha for path, sha in files['guides'].items() if path.endswith('.json')}}:
        raise IdentityError('规则清单与指南规则不一致')
    if set(value['guides']) != {path[len(GUIDE):] for path in files['guides']}:
        raise IdentityError('指南正文与清单不一致')
    for relative, content in value['guides'].items():
        _path(relative)
        if type(content) is not str:
            raise IdentityError('指南须为 UTF-8 文本')
        try:
            digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        except UnicodeError as exc:
            raise IdentityError('指南须为有效 UTF-8 文本') from exc
        if files['guides'][GUIDE + relative] != digest:
            raise IdentityError('指南正文哈希不一致')
        if relative.endswith('.py') and files['code'].get(GUIDE + relative) != digest:
            raise IdentityError('指南脚本与代码清单不一致')
    for field, category in (('code_hash', 'code'), ('rules_hash', 'rules'), ('guide_version', 'guides')):
        if value[field] != gate.digest(files[category]):
            raise IdentityError('政策身份的分类哈希不一致')
    claimed = value.pop('identity')
    if claimed != 'policy:' + gate.digest(value):
        raise IdentityError('政策身份内容哈希不一致')


def check_installed(manifest: dict) -> None:
    """Refuse evaluation under an identity whose installed implementation has changed."""
    verify(manifest)
    snapshot = _snapshot()
    if manifest['files'] != snapshot['files'] or manifest['guides'] != snapshot['guides']:
        raise IdentityError('当前安装的代码、规则或指南与政策身份不一致；须显式登记新身份')
