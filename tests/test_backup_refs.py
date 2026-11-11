"""Captured report excerpts plus explicit structural hash/corruption variants."""
from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from a_share_research import backup_refs, db, gate, integrity
from a_share_research.account import analysis
from support.reports import MATERIAL, report


def put(conn, table, column, value, *, raw=False):
    conn.execute(f'CREATE TABLE IF NOT EXISTS {table} ({column} TEXT)')
    conn.execute(f'INSERT INTO {table} VALUES (?)', (value if raw else json.dumps(value),))


@pytest.fixture(params=[None, sqlite3.Row])
def conn(request):
    conn=sqlite3.connect(':memory:')
    conn.row_factory=request.param
    yield conn
    conn.close()


def test_missing_legacy_tables_and_empty_database(conn):
    assert backup_refs.required(conn)=={'account_evidence': [], 'pdf': []}


def test_all_committed_account_reference_locations_and_failed_batch_exclusion(conn):
    for index,(table,column,is_list) in enumerate((
        ('account_books','opening_evidence',False),
        ('account_observations','evidence',False),
        ('account_position_books','evidence_hashes',True),
        ('account_analysis_history','evidence_hashes',True),
        ('policy_v0_account_openings','evidence_hashes',True),
    ),start=1):
        sha=str(index)*64
        put(conn,table,column,[sha,sha] if is_list else sha,raw=not is_list)
    conn.execute('CREATE TABLE account_batches (status TEXT, request TEXT)')
    conn.executemany('INSERT INTO account_batches VALUES (?,?)',[
        ('posted',json.dumps({'evidence_hashes':['6'*64,'1'*64]})),
        ('rejected','{invalid JSON'),
        ('pending',json.dumps({'evidence_hashes':['../missing']})),
    ])
    assert backup_refs.required(conn)=={'account_evidence':[str(i)*64 for i in range(1,7)],'pdf':[]}


def test_captured_material_and_report_without_pdf_hash_do_not_guess(conn):
    put(conn,'runs','material',MATERIAL.snapshot())
    put(conn,'external_research','material',MATERIAL.snapshot())
    put(conn,'reports','body',report(document=MATERIAL.doc.info()))
    assert backup_refs.required(conn)['pdf']==[]



def test_legacy_missing_pdf_hash_and_uncollected_material_are_skipped(conn):
    snapshot=deepcopy(MATERIAL.snapshot())
    del snapshot['data']['doc']['sha256']
    snapshot['data']['extra']={'catalog':{'D9':{'sha256':'a'*64}}}
    put(conn,'runs','material',snapshot)
    put(conn,'runs','material',None,raw=True)
    put(conn,'reports','body',{'document':{'title':'legacy'},'sources':{'D1':{'document':{}}}})
    assert backup_refs.required(conn)['pdf']==[]


def test_explicit_pdf_hash_variants_cover_all_actual_document_paths(conn):
    # Hashes here are structural variants of the captured excerpt, not evidence of downloaded PDFs.
    snapshot=deepcopy(MATERIAL.snapshot())
    snapshot['data']['doc']['sha256']='a'*64
    snapshot['data']['extra']={'sources':{'D1':{'document':dict(MATERIAL.doc.info(),sha256='b'*64),'pages':{'9':'excerpt'}}}}
    put(conn,'runs','material',snapshot)
    external=deepcopy(snapshot)
    external['data']['doc']['sha256']='c'*64
    external['data']['extra']['sources']['D1']['document']['sha256']='d'*64
    put(conn,'external_research','material',external)
    put(conn,'reports','body',report(document=dict(MATERIAL.doc.info(),sha256='e'*64),
        sources={'D2':{'document':dict(MATERIAL.doc.info(),sha256='f'*64),'pages':[9]}},
        sha256='0'*64))
    assert backup_refs.required(conn)=={'account_evidence':[],'pdf':[c*64 for c in 'abcdef']}


@pytest.mark.parametrize('value',['not a hash','../outside','A'*64,None,123])
def test_nonempty_illegal_pdf_hash_fails_closed(conn,value):
    put(conn,'reports','body',{'document':{'sha256':value}})
    with pytest.raises(ValueError,match='SHA256'):
        backup_refs.required(conn)


@pytest.mark.parametrize('table,column,value',[
    ('account_books','opening_evidence','../outside'),
    ('account_position_books','evidence_hashes','{broken'),
    ('account_analysis_history','evidence_hashes','{}'),
    ('policy_v0_account_openings','evidence_hashes','["not a hash"]'),
    ('runs','material','{broken'),
    ('external_research','material','{}'),
    ('external_research','material','{"data":{}}'),
    ('reports','body','null'),
    ('reports','body','{"unused":NaN}'),
    ('reports','body','{"sources":{"D1":[]}}'),
    ('reports','body','{"sources":[]}'),
])
def test_corrupt_committed_references_are_rejected(conn,table,column,value):
    put(conn,table,column,value,raw=True)
    with pytest.raises(ValueError):
        backup_refs.required(conn)


@pytest.mark.parametrize('body',[
    {'document':None,'sources':None},
    {'sources':{'D1':{}}},
    {'sources':{'D1':{'document':None}}},
])
def test_unrecorded_document_identity_keeps_existing_source_registry_contract(conn,body):
    from a_share_research.evidence import source_registry
    source_registry(body.get('document'),sources=body.get('sources'))
    put(conn,'reports','body',body)
    assert backup_refs.required(conn)['pdf']==[]


@pytest.mark.parametrize('payload',['{broken','{}','{"evidence_hashes":["bad"]}'])
def test_posted_batch_cannot_hide_invalid_evidence(conn,payload):
    conn.execute('CREATE TABLE account_batches (status TEXT, request TEXT)')
    conn.execute('INSERT INTO account_batches VALUES (?,?)',('posted',payload))
    with pytest.raises(ValueError):
        backup_refs.required(conn)


@pytest.mark.parametrize('version',[15,16,17])
def test_old_analysis_scope_inspection_is_read_only_and_preserves_legacy_hashes(tmp_path,version):
    path=tmp_path/'legacy.db'
    conn=sqlite3.connect(path,isolation_level=None)
    conn.row_factory=sqlite3.Row
    conn.executescript(db.SCHEMA)
    for number,migration in enumerate(db.MIGRATIONS[:version],start=1):
        with db.transaction(conn):
            migration(conn)
            conn.execute(f'PRAGMA user_version={number}')
    snapshot={table:[] for table in analysis.TABLES}
    request={'source':'legacy_baseline','holding_id':1,'snapshot':snapshot}
    conn.execute('''INSERT INTO account_analysis_history
        (id,holding_id,company_key,version,parent_id,request_hash,source,actor_key_id,
         created_at,snapshot_hash,evidence_hashes,request,snapshot)
         VALUES (1,1,'600519',0,NULL,?,'legacy_baseline',NULL,'2026-11-11',?,'[]',?,?)''',
         (gate.digest(request),gate.digest(snapshot),gate.canonical(request),gate.canonical(snapshot)))
    conn.close()
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    ro=sqlite3.connect(f'file:{path}?mode=ro',uri=True)
    try:
        assert analysis.inspect(ro)==[]
        assert backup_refs.required(ro)=={'account_evidence':[],'pdf':[]}
        assert ro.execute('PRAGMA user_version').fetchone()[0]==version
        assert 'account_id' not in {row[1] for row in ro.execute('PRAGMA table_info(account_analysis_history)')}
    finally:
        ro.close()
    problems,_=integrity.check(path,tmp_path/'pdf')
    assert problems==[]
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
