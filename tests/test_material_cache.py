"""Snapshot caching on captured Moutai material and production continuation records."""

from copy import deepcopy
import json

import pytest

from a_share_research import db
from a_share_research.evidence import material_hash
from a_share_research.material import Material, replay_material
from support.continuation import BODY
from support.reports import MATERIAL


def test_snapshot_reuses_hash_and_isolates_mutable_results(tmp_path):
    material = Material.from_snapshot(MATERIAL.snapshot())
    first = material.snapshot()
    cached = material._cached_snapshot
    assert material.snapshot() == first
    assert material._cached_snapshot is cached
    first['data']['table'].clear()
    first['sha256'] = ''
    assert material.snapshot() == MATERIAL.snapshot()
    assert material._cached_snapshot is cached

    conn = db.connect(tmp_path / 'material.db')
    try:
        run_id = db.create_run(conn, material.code)
        snapshot = material.snapshot()
        db.save_material(conn, run_id, snapshot)
        before, statements = conn.total_changes, []
        conn.set_trace_callback(statements.append)
        db.save_material(conn, run_id, material.snapshot())
        conn.set_trace_callback(None)
        assert conn.total_changes == before
        assert not any('UPDATE' in sql.upper() for sql in statements)
        assert replay_material(conn, run_id).snapshot() == snapshot
        stored = conn.execute('SELECT material FROM runs WHERE id=?', (run_id,)).fetchone()[0]
        assert stored == json.dumps(snapshot, ensure_ascii=False)
    finally:
        conn.close()


@pytest.mark.parametrize('change', ['page', 'raw_page', 'table_value', 'table_order',
                                   'mapping_order', 'document', 'extra', 'remove_extra'])
def test_snapshot_invalidates_nested_edits_and_preserves_ordered_hash(change):
    material = Material.from_snapshot(MATERIAL.snapshot())
    material.extra = {'prior': deepcopy(BODY[39]['prior'])}
    before = material.snapshot()
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
    cached = material._cached_snapshot
    assert material.snapshot() == after
    assert material._cached_snapshot is cached
