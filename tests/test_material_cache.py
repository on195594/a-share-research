"""Snapshot hashes and skipped writes on captured Moutai material."""

from copy import deepcopy
import json

import pytest

from a_share_research.evidence import material_hash
from a_share_research.material import Material
from support.continuation import BODY
from support.reports import MATERIAL


@pytest.mark.parametrize('change', ['page', 'raw_page', 'table_value', 'table_order',
                                   'mapping_order', 'document', 'extra', 'remove_extra'])
def test_snapshot_hash_tracks_nested_edits_and_order(change):
    material = Material.from_snapshot(deepcopy(MATERIAL.snapshot()))
    material.extra = {'prior': deepcopy(BODY[39]['prior'])}
    before = deepcopy(material.snapshot())
    if change == 'page':
        material.pages[9] = material.pages[10]
    elif change == 'raw_page':
        material.raw_pages[9] = material.pages[9]
    elif change == 'table_value':
        values = material.table[0]['values']
        values['2025-12-31'] = values['2024-12-31']
    elif change == 'table_order':
        material.table.reverse()
    elif change == 'mapping_order':
        material.pages[9] = material.pages.pop(9)
    elif change == 'document':
        material.doc.url = BODY[39]['document']['url']
    elif change == 'extra':
        material.extra['prior'].pop('question_added')
    else:
        material.extra.clear()
    after = material.snapshot()
    assert before['sha256'] == material_hash(before['data'])
    assert after['sha256'] == material_hash(after['data']) != before['sha256']
    assert after['version'] == (2 if material.extra else 1)
    restored = Material.from_snapshot(json.loads(json.dumps(after)))
    assert restored.snapshot()['sha256'] == after['sha256']
    assert material.snapshot() == after
