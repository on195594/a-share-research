"""Snapshot hashes and skipped writes on captured Moutai material."""

from copy import deepcopy
import json

import pytest

from a_share_research import db
from a_share_research.evidence import material_hash
from a_share_research.material import Material, replay_material
from support.continuation import BODY
from support.reports import MATERIAL


def test_save_material_skips_writes_when_sha_is_unchanged(tmp_path):
    material = Material.from_snapshot(deepcopy(MATERIAL.snapshot()))
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
