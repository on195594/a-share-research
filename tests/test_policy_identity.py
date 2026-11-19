"""Identity tests use installed sources; corruptions are explicit content variants."""
import copy
import hashlib
from pathlib import Path

import shutil

import pytest

from a_share_research import gate
from a_share_research.policy import identity


def resign(manifest):
    manifest['identity'] = 'policy:' + gate.digest({k: v for k, v in manifest.items() if k != 'identity'})
    return manifest


def test_real_installation_identity_and_archived_guides():
    parameters = {'source': 'explicit-identity-test', 'nested': {'values': [1, True, None]}}
    manifest = identity.build(parameters)
    identity.verify(manifest)
    identity.check_installed(manifest)
    assert manifest['identity'].startswith('policy:')
    assert 'a_share_research/policy/identity.py' in manifest['files']['code']
    assert 'a_share_research/db.py' in manifest['files']['code']
    assert manifest['schema_version'] == 2 and identity.LOCK in manifest['files']['code']
    assert 'a_share_research/research.py' not in manifest['files']['code']
    assert 'a_share_research/prompts.py' not in manifest['files']['code']
    guide = 'a-stock-monitor/SKILL.md'
    assert manifest['guides'][guide] == (identity.ROOT/identity.GUIDE/guide).read_text()
    assert manifest['files']['rules'][identity.RULES] == hashlib.sha256((identity.ROOT/identity.RULES).read_bytes()).hexdigest()
    parameters['nested']['values'].append('later edit')
    assert manifest['parameters']['nested']['values'] == [1, True, None]
    assert identity.build(manifest['parameters']) == manifest


@pytest.mark.parametrize('parameters', [{}, [], {'number': float('inf')}, {'number': float('nan')},
    {'nested': {1: 'numeric key'}}, {'tuple': (1, 2)}, {'text': '\ud800'}])
def test_parameters_require_strict_json(parameters):
    with pytest.raises(identity.IdentityError):
        identity.build(parameters)


def test_different_parameters_have_different_identity():
    first = identity.build({'source': 'explicit-first-parameter-variant'})
    second = identity.build({'source': 'explicit-second-parameter-variant'})
    assert first['identity'] != second['identity']
    assert first['code_hash'] == second['code_hash']
    assert first['guide_version'] == second['guide_version']


@pytest.mark.parametrize('field', ['code_hash', 'rules_hash', 'guide_version', 'identity'])
def test_tampered_hash_is_rejected(field):
    manifest = identity.build({'source': 'explicit-identity-test'})
    manifest[field] = '0' * 64
    with pytest.raises(identity.IdentityError):
        identity.verify(manifest)


def test_guide_body_tampering_rejected_even_with_recomputed_outer_identity():
    manifest = identity.build({'source': 'explicit-identity-test'})
    manifest['guides']['a-stock-monitor/SKILL.md'] += '\nExplicit tamper variant.\n'
    with pytest.raises(identity.IdentityError, match='指南正文哈希'):
        identity.verify(resign(manifest))


def test_rule_and_guide_hashes_cannot_disagree():
    manifest = identity.build({'source': 'explicit-identity-test'})
    rule = next(path for path in manifest['files']['rules'] if path.endswith('.json'))
    manifest['files']['rules'][rule] = '0' * 64
    manifest['rules_hash'] = gate.digest(manifest['files']['rules'])
    with pytest.raises(identity.IdentityError, match='规则清单'):
        identity.verify(resign(manifest))


@pytest.mark.parametrize('bad_path', ['../outside.py', '/absolute.py', 'a_share_research/../outside.py',
    'a_share_research//module.py', 'a_share_research/./module.py', 'a_share_research\\module.py'])
def test_manifest_paths_never_escape(bad_path):
    manifest = identity.build({'source': 'explicit-identity-test'})
    manifest['files']['code'][bad_path] = '0' * 64
    manifest['code_hash'] = gate.digest(manifest['files']['code'])
    with pytest.raises(identity.IdentityError):
        identity.verify(resign(manifest))


@pytest.mark.parametrize('category', ['code', 'rules', 'guides'])
def test_archived_identity_is_not_current_installation(monkeypatch, category):
    manifest = identity.build({'source': 'explicit-identity-test'})
    changed = copy.deepcopy(identity._snapshot())
    path = next(iter(changed['files'][category]))
    changed['files'][category][path] = '0' * 64
    monkeypatch.setattr(identity, '_snapshot', lambda: changed)
    # Historical integrity remains valid; the current implementation does not match.
    identity.verify(manifest)
    with pytest.raises(identity.IdentityError, match='当前安装'):
        identity.check_installed(manifest)


@pytest.mark.parametrize('directory', [False, True])
def test_installation_symlinks_are_rejected(monkeypatch, tmp_path, directory):
    package = tmp_path/'a_share_research'
    package.mkdir()
    target = tmp_path/'target'
    if directory:
        target.mkdir()
    else:
        target.write_text('explicit symlink test')
    (package/'linked').symlink_to(target, target_is_directory=directory)
    monkeypatch.setattr(identity, 'ROOT', tmp_path)
    with pytest.raises(identity.IdentityError, match='符号链接'):
        identity.build({'source': 'explicit-identity-test'})


def test_missing_installation_is_not_silently_registered(monkeypatch, tmp_path):
    monkeypatch.setattr(identity, 'ROOT', tmp_path)
    with pytest.raises(identity.IdentityError):
        identity.build({'source': 'explicit-identity-test'})


def test_no_old_runtime_imported_by_identity_module():
    import subprocess
    import sys
    code = ('import sys; from a_share_research.policy import identity; '
            'identity.build({"source":"identity-import-test"}); '
            'assert not any(n.startswith("a_share_research.policy.v0") for n in sys.modules)')
    result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).parents[1],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def install(tmp_path, monkeypatch):
    shutil.copytree(identity.ROOT/'a_share_research', tmp_path/'a_share_research',
                    ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copyfile(identity.ROOT/'uv.lock', tmp_path/'uv.lock')
    monkeypatch.setattr(identity, 'ROOT', tmp_path)
    return tmp_path


def _edit(path, old, new):
    text = path.read_text()
    assert text.count(old) == 1
    path.write_text(text.replace(old, new))


def _locked(name):
    return f'[[package]]\nname = "{name}"\nversion = "'


@pytest.mark.parametrize(('change', 'same'), [
    (lambda root: (root/'a_share_research/prompts.py').open('a').write('\n# research-only variant\n'), True),
    (lambda root: (root/'a_share_research/research.py').open('a').write('\n# research-only variant\n'), True),
    (lambda root: _edit(root/'uv.lock', _locked('pytest'), _locked('pytest') + '0.'), True),
    (lambda root: _edit(root/'uv.lock', 'dev = [\n    { name = "httpx" },\n', 'dev = [\n'), True),
    (lambda root: (root/'a_share_research/gate.py').open('a').write('\n# shared variant\n'), False),
    (lambda root: (root/'a_share_research/policy/v0/__init__.py').open('a').write('\n# policy variant\n'), False),
    (lambda root: _edit(root/'uv.lock', _locked('pandas'), _locked('pandas') + '0.'), False),
])
def test_code_scope_is_policy_import_closure_and_runtime_lock(install, change, same):
    manifest = identity.build({'source': 'explicit-identity-test'})
    change(install)
    if same:
        identity.check_installed(manifest)
        assert identity.build(manifest['parameters']) == manifest
    else:
        with pytest.raises(identity.IdentityError, match='当前安装'):
            identity.check_installed(manifest)


def test_newly_imported_shared_module_joins_scope(install):
    before = identity.build({'source': 'explicit-identity-test'})
    assert 'a_share_research/prompts.py' not in before['files']['code']
    (install/'a_share_research/policy/v0/__init__.py').open('a').write('\nfrom a_share_research import prompts\n')
    assert 'a_share_research/prompts.py' in identity.build(before['parameters'])['files']['code']


@pytest.mark.parametrize('snippet', [
    '\nimport importlib\nimportlib.import_module("x")\n',
    '\nfrom importlib import import_module\nimport_module("x")\n',
    '\nfrom importlib.util import spec_from_file_location as load\nload("x", "y")\n',
    '\nimport builtins\nbuiltins.__import__("x")\n',
])
def test_dynamic_import_in_scope_fails_closed(install, snippet):
    (install/'a_share_research/gate.py').open('a').write(snippet)
    with pytest.raises(identity.IdentityError, match='动态导入'):
        identity.build({'source': 'explicit-identity-test'})


def test_runtime_lock_handles_cyclic_optional_extras():
    lock = b'''
requires-python = ">=3.13"

[[package]]
name = "a-share-research"
version = "0.0.0"
source = { editable = "." }
dependencies = [{ name = "alpha", extra = ["x"] }]

[[package]]
name = "alpha"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }
[package.optional-dependencies]
x = [{ name = "beta", extra = ["y"] }]

[[package]]
name = "beta"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }
[package.optional-dependencies]
y = [{ name = "alpha", extra = ["x"] }]
'''
    digest = identity._runtime_lock(lock)
    assert len(digest) == 64


def test_schema_1_whole_package_manifest_still_verifies():
    manifest = identity.build({'source': 'explicit-identity-test'})
    code = {path.relative_to(identity.ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (identity.ROOT/'a_share_research').rglob('*.py') if '__pycache__' not in path.parts}
    code['uv.lock'] = hashlib.sha256((identity.ROOT/'uv.lock').read_bytes()).hexdigest()
    manifest.update(schema_version=1, code_hash=gate.digest(code))
    manifest['files']['code'] = code
    identity.verify(resign(manifest))
    with pytest.raises(identity.IdentityError, match='当前安装'):
        identity.check_installed(manifest)
    manifest['files']['code'][identity.LOCK] = manifest['files']['code'].pop('uv.lock')
    manifest['code_hash'] = gate.digest(manifest['files']['code'])
    with pytest.raises(identity.IdentityError):
        identity.verify(resign(manifest))
