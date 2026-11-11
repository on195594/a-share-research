"""Release backup integration only; never invokes Docker or touches live data."""
import os

import pytest

from a_share_research import backup, deploy
from test_backup import source as source_fixture, database_rows


@pytest.fixture
def source(tmp_path):
    yield from source_fixture.__wrapped__(tmp_path)


def test_release_backup_keeps_legacy_db_entry_and_verified_evidence_bundle(source,tmp_path):
    target=tmp_path/'backup-pre-structural-20261111-000000.db'
    result=deploy.create_release_backup(source[0]/'app.db',target)
    bundle=target.with_suffix('.bundle')
    assert result['bundle']==str(bundle) and result['account_evidence']==1
    assert target.read_bytes()==(bundle/'app.db').read_bytes()
    assert target.stat().st_ino!=(bundle/'app.db').stat().st_ino
    assert database_rows(target)==database_rows(source[0]/'app.db')
    assert backup.verify(bundle)['ok']


def test_release_pruning_removes_only_matched_complete_old_pairs(source,tmp_path):
    targets=[]
    for index in range(4):
        target=tmp_path/f'backup-pre-structural-20261111-00000{index}.db'
        deploy.create_release_backup(source[0]/'app.db',target)
        os.utime(target,(1000+index,1000+index))
        targets.append(target)
    assert deploy.prune_backups(tmp_path)==[targets[0].name]
    assert not targets[0].exists() and not targets[0].with_suffix('.bundle').exists()
    assert all(t.exists() and t.with_suffix('.bundle').exists() for t in targets[1:])
    assert (source[0]/'app.db').exists()


def test_unmatched_or_incomplete_bundle_is_preserved_during_pruning(source,tmp_path):
    target=tmp_path/'backup-pre-structural-20261111-000000.db'
    deploy.create_release_backup(source[0]/'app.db',target)
    bundle=target.with_suffix('.bundle')
    extra=bundle/'unrelated.txt'
    extra.write_text('Must not delete unrelated bytes even inside a matching-name directory')
    extra.chmod(0o600)
    assert deploy.prune_backups(tmp_path,keep=0)==[]
    assert extra.exists() and target.exists()
