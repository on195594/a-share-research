"""Identity tests use installed sources; corruptions are explicit content variants."""
import hashlib
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
    assert manifest['identity'].startswith('policy:')
    assert manifest['schema_version'] == 3
    assert manifest['files'].keys() == {'rules', 'guides'}
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
    assert first['rules_hash'] == second['rules_hash']
    assert first['guide_version'] == second['guide_version']


@pytest.mark.parametrize('field', ['rules_hash', 'guide_version', 'identity'])
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


@pytest.mark.parametrize('version', [1, 2])
def test_verify_requires_current_schema(version):
    manifest = identity.build({'source': 'explicit-identity-test'})
    manifest['schema_version'] = version
    with pytest.raises(identity.IdentityError):
        identity.verify(resign(manifest))


def test_only_rules_and_guides_affect_installed_identity(tmp_path, monkeypatch):
    root = tmp_path / 'a_share_research/policy/v0'
    shutil.copytree(identity.ROOT / identity.GUIDE, root / 'guide')
    shutil.copyfile(identity.ROOT / identity.RULES, tmp_path / identity.RULES)
    monkeypatch.setattr(identity, 'ROOT', tmp_path)
    original = identity.build({'source': 'explicit-identity-test'})
    (root / '__init__.py').write_text('# Refactored policy implementation.\n')
    assert identity.build(original['parameters']) == original

    script = root / 'guide' / 'example.py'
    script.write_text('# Guide example.\n')
    changed = identity.build(original['parameters'])
    assert changed['guides']['example.py'] == script.read_text()
    assert changed['identity'] != original['identity']
    assert changed['guide_version'] != original['guide_version']
    assert changed['rules_hash'] == original['rules_hash']
