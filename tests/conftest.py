import os

import pytest


@pytest.fixture
def bind_account():
    from a_share_research import db, gate

    def bind(conn, account_id):
        binding = dict(schema_version=1, account_id=account_id, account_scope='structural-scope', mirror_watermark={})
        conn.execute('INSERT INTO account_source VALUES(1,?,?,?,?)',
                     ('ledger', 'a' * 64, db.now(), gate.canonical(binding)))
    return bind


def pytest_configure(config):
    config.addinivalue_line("markers", "wide_scan_scope: scan beyond subscriptions (holdings, tracking L5 and card companies)")


@pytest.fixture(autouse=True)
def narrow_scan_scope(request, monkeypatch):
    # Captured reports give several fixture companies a card; scan replays written against
    # subscriptions keep that scope unless a test opts into the wide production scope.
    if request.node.get_closest_marker("wide_scan_scope") is None:
        from a_share_research import scans
        monkeypatch.setattr(scans, "scan_scope", lambda conn, **_: scans.subscriptions(conn))


@pytest.fixture(autouse=True)
def isolate_tushare_cache(monkeypatch, tmp_path):
    # Offline market replays may change between calls; never read/write the owner's cache.
    from a_share_research import db
    monkeypatch.setattr(db, "DATA", tmp_path / "data")
    monkeypatch.setenv("TUSHARE_CACHE", "0")
    # Releases sign with the owner's standing credential; tests never read the real one.
    from a_share_research import deploy
    monkeypatch.setattr(deploy, "OWNER_CREDENTIAL", tmp_path / "absent-owner.json")


@pytest.fixture
def web(tmp_path, monkeypatch):
    import importlib
    import sys

    import a_share_research
    from a_share_research import db
    from support.continuation import load

    monkeypatch.setattr(db, "DATA", tmp_path)
    monkeypatch.setenv("DEV_LOGIN", "1")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://127.0.0.1:8627")
    monkeypatch.setenv("SESSION_SECRET", "isolated-web-connection-" * 2)
    monkeypatch.setattr(a_share_research, "web", None, raising=False)
    monkeypatch.delitem(sys.modules, "a_share_research.web", raising=False)
    module = importlib.import_module("a_share_research.web")
    load(module.conn)
    try:
        yield module
    finally:
        module.conn.close()


@pytest.fixture
def server(tmp_path):
    import socket
    import subprocess
    import sys
    import time
    from pathlib import Path

    import requests

    from a_share_research import api_keys, db, service
    from support.delivery import saved

    conn = db.connect(tmp_path / "app.db")
    run, report = saved(conn)
    _, failed = saved(conn, passed=False)
    queued = service.submit(conn, "600519", "lost-http-response", question="现金流")
    token = api_keys.create(conn, "isolated-http-test")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    env = {**os.environ, "DATA_DIR": str(tmp_path), "PUBLIC_BASE_URL": url,
           "DEV_LOGIN": "1", "SESSION_SECRET": "isolated-test-only-not-a-real-secret"}
    log_path = tmp_path / "web.log"
    log = log_path.open("w")
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "a_share_research.web:app",
                                "--fd", str(listener.fileno()), "--log-level", "error"],
                               pass_fds=(listener.fileno(),), env=env,
                               cwd=Path(__file__).resolve().parents[1],
                               stdout=log, stderr=log)
    listener.close()
    client = requests.Session()
    client.trust_env = False
    client.headers["Authorization"] = f"Bearer {token}"
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert process.poll() is None, f"isolated web process exited; log: {log_path}"
            try:
                if client.get(url + "/login", timeout=1).status_code == 200:
                    break
            except (requests.ConnectionError, requests.Timeout):
                pass
            time.sleep(0.05)
        else:
            pytest.fail("isolated web did not become ready")
        yield conn, client, url, run, report, failed, queued
    finally:
        client.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        conn.close()
        log.close()


@pytest.fixture
def mcp_tools(tmp_path, monkeypatch):
    """The tools /mcp endpoint over tmp_path/app.db, the database of `server` and `reading_server`."""
    from fastapi.testclient import TestClient

    from a_share_research.tools.server import create_app
    from support.mcp import ORIGIN, RESOURCE

    monkeypatch.setenv("ASHARE_PUBLIC_MCP_URL", RESOURCE)
    with TestClient(create_app(tmp_path / "app.db"), base_url=ORIGIN) as client:
        yield client


@pytest.fixture
def ashare_cli(tmp_path, monkeypatch):
    """A real subprocess boundary with scripted MCP data and exit statuses."""
    import json
    import sys

    config = tmp_path / "hermes-assistant.json"
    config.write_text(json.dumps({"url": "https://stock.test/mcp", "token": "fixture-assistant"}))
    config.chmod(0o600)
    monkeypatch.setenv("ASHARE_HERMES_CONFIG", str(config))
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    program = tmp_path / "ashare"
    program.write_text(f"#!{sys.executable}\n" + """\
import json
from pathlib import Path
import sys

root = Path(__file__).parent
config_flag, config, verb, name, json_flag, raw = sys.argv[1:]
assert (config_flag, config, verb, json_flag) == ('--config', str(root / 'hermes-assistant.json'), 'call', '--json')
args = json.loads(raw)
with (root / 'calls.jsonl').open('a') as calls:
    print(json.dumps([name, args]), file=calls)
reply = json.loads((root / (name + '.reply')).read_text())
if reply['delay']:
    import time
    time.sleep(reply['delay'])

def echo_inputs(value):
    if isinstance(value, dict):
        return {k: echo_inputs(v) for k, v in value.items()}
    if isinstance(value, list):
        return [echo_inputs(v) for v in value]
    if isinstance(value, str) and value in ('$request_id', '$expected_activation_id', '$slot', '$job', '$run_id'):
        return args[value[1:]]
    return value

print(json.dumps({'ok': True, 'result': {'data': echo_inputs(reply['data'])}}))
print(reply['stderr'], file=sys.stderr)
sys.exit(reply['code'])
""")
    program.chmod(0o700)

    class Client:
        def reply(self, name, data=None, *, code=0, stderr="", delay=0):
            (tmp_path / (name + ".reply")).write_text(json.dumps(dict(data=data, code=code, stderr=stderr, delay=delay)))

        @property
        def calls(self):
            log = tmp_path / "calls.jsonl"
            return [(name, args) for name, args in map(json.loads, log.read_text().splitlines())] if log.exists() else []

    return Client()


@pytest.fixture
def mcp_http(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import httpx

    from a_share_research.tools import remote_mcp
    from a_share_research.tools.control import Gate
    from a_share_research.tools.server import create_app
    from support.mcp import ORIGIN, RESOURCE

    monkeypatch.setenv("ASHARE_PUBLIC_MCP_URL", RESOURCE)
    gate = Gate(limit=1)

    # Keep ASGI dispatch on the test thread; all handlers and database calls are real.
    async def inline(function, *args):
        return function(*args)

    monkeypatch.setattr(remote_mcp, "run_in_threadpool", inline)

    async def request(url, *, headers, content):
        app = create_app(tmp_path / "app.db", gate=gate, account_shadow_enabled=True)
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as server:
            return await server.post(url, headers=headers, content=content)

    def post(url, *, headers, content):
        return asyncio.run(request(url, headers=headers, content=content))

    return SimpleNamespace(post=post), gate


@pytest.fixture
def policy_book(tmp_path):
    from support.positions import policy_account_book

    yield from policy_account_book(tmp_path)


@pytest.fixture
def conn(tmp_path):
    from a_share_research import db
    from support.continuation import load

    connection = db.connect(tmp_path / 'scans.db')
    load(connection)
    yield connection
    connection.close()


@pytest.fixture
def disclosure_replay(conn):
    from support.scans import replay_disclosure

    return replay_disclosure(conn)


@pytest.fixture
def position_book(tmp_path):
    from support.positions import cost_book

    yield from cost_book(tmp_path)


@pytest.fixture
def policy_setup(tmp_path, monkeypatch):
    from copy import deepcopy

    from a_share_research import api_keys, db
    from a_share_research.account import mirror
    from a_share_research.policy import monitor, registry
    from a_share_research.policy.v0 import risk_budget

    path=tmp_path/'app.db'
    conn=db.connect(path)
    principals={role:api_keys.authenticate(conn,api_keys.create(conn,role,role=role))
                for role in ('owner','assistant','research')}
    parameters=risk_budget.policy()
    manifest=registry.register(conn,principals['owner'],parameters)
    activation=registry.activate(conn,principals['owner'],manifest['identity'],expected_id=0,
                                 request_id='structural-activation',reason='empty-account transport fixture')
    sha=mirror.digest(mirror._read(conn,tmp_path))
    conn.execute('INSERT INTO account_source VALUES (1,?,?,?,?)',('mirror',sha,db.now(),'{}'))
    bundle=dict(local=dict(holdings=[],closed_count=0),quotes={},portfolio_value=None,industry_result=None,
                now=db.now(),risk_policy=parameters,denominator_evidence={},account_scope_required=False,
                research_result={'status':'not_configured','cards':[]})
    captured=[]
    def capture(database, actual_parameters):
        captured.append((database,actual_parameters))
        assert not conn.in_transaction  # Network collection must not retain the caller's database lock.
        return deepcopy(bundle)
    monkeypatch.setattr(monitor,'capture',capture)
    yield path,conn,principals,manifest,activation,bundle,captured
    conn.close()


@pytest.fixture
def ledger_monitor(bound_book, monkeypatch):
    from a_share_research import api_keys
    from a_share_research.policy import capture, monitor, registry
    from support.policy import NOW, scoped_parameters

    path,conn,principal,directory,sha=bound_book
    owner=api_keys.authenticate(conn,api_keys.create(conn,'ledger-monitor-owner',role='owner'))
    manifest=registry.register(conn,owner,scoped_parameters())
    active=registry.activate(conn,owner,manifest['identity'],expected_id=0,
        request_id='ledger-monitor-activate',reason='structural ledger collector')
    monkeypatch.setenv('POLICY_V0_DB',str(path))
    monkeypatch.delenv('POLICY_V0_ACCOUNT_SHADOW',raising=False)
    monkeypatch.delenv('POLICY_V0_ANALYSIS_SHADOW',raising=False)
    monkeypatch.setattr(capture.domain,'cst_now',lambda:NOW)
    monkeypatch.setattr(capture.monitor_research,'fetch_cards',lambda:{'status':'not_configured','cards':[]})
    monkeypatch.setattr(capture.commands_holdings,'fetch_monitor_price_quotes',lambda codes:({code:None for code in codes},None))
    calls=[]
    def collect(database,parameters,*,source):
        assert not conn.in_transaction
        calls.append(source)
        return capture.collect(parameters,source=source)
    monkeypatch.setattr(monitor,'capture',collect)
    return bound_book,owner,active,calls


@pytest.fixture
def bound_book(tmp_path):
    from support.policy import policy_input_book

    yield from policy_input_book(tmp_path)


@pytest.fixture
def owner_request(web):
    from support.web import page_request

    cookie = web.start_session().headers["set-cookie"].split(";", 1)[0]
    return lambda path, **kwargs: page_request(path, cookie=cookie, **kwargs)


@pytest.fixture
def feedback_web(tmp_path, monkeypatch):
    import importlib
    import sys

    import a_share_research
    from a_share_research import db
    from support.continuation import load

    monkeypatch.setattr(db, 'DATA', tmp_path)
    monkeypatch.setenv('DEV_LOGIN', '1')
    monkeypatch.setenv('PUBLIC_BASE_URL', 'http://127.0.0.1:8627')
    monkeypatch.setenv('SESSION_SECRET', 'isolated-feedback-retry-' * 2)
    monkeypatch.setattr(a_share_research, 'web', None, raising=False)
    monkeypatch.delitem(sys.modules, 'a_share_research.web', raising=False)
    module = importlib.import_module('a_share_research.web')
    load(module.conn)
    yield module
    module.conn.close()


@pytest.fixture
def local_policy(tmp_path, monkeypatch):
    import json
    import os
    import shutil
    import sqlite3
    import subprocess
    import sys
    from contextlib import closing
    from types import SimpleNamespace

    from a_share_research import api_keys, db, deploy
    from a_share_research.account import mirror
    from a_share_research.policy.v0 import risk_budget

    original_freeze, original_probe = deploy.freeze_policy, deploy.policy_probe
    old = tmp_path / 'old-install'
    old.mkdir()
    shutil.copytree(deploy.ROOT / 'a_share_research', old / 'a_share_research',
                    ignore=shutil.ignore_patterns('__pycache__'))
    with (old / 'a_share_research/policy/v0/RULES.md').open('a') as stream:
        stream.write('\nPredecessor rule text for the release fixture.\n')
    config = tmp_path / 'empty.env'
    config.touch(mode=0o600)

    def setup(state=None, *, parameters=None):
        monkeypatch.setattr(deploy, 'freeze_policy', original_freeze)
        monkeypatch.setattr(deploy, 'policy_probe', original_probe)
        data = state.data if state else tmp_path / 'data'
        data.mkdir(exist_ok=True)
        database = data / 'app.db'
        with closing(db.connect(database)) as conn:
            token = api_keys.create(conn, 'isolated-owner', role='owner')
            conn.execute('INSERT INTO account_source VALUES (1,?,?,?,?)',
                         ('mirror', mirror.digest(mirror._read(conn, data)), db.now(), '{}'))
        local = SimpleNamespace(current='old-worker', database=database,
                                token=token, lose_reply=False, failed_writes=False, after_activate=None, probe_time=None)
        def command(script, payload, *, image=None):
            selected = image or (state.containers['worker'] if state else local.current)
            root = old if selected == 'old-worker' else deploy.ROOT
            action = 'activate' if script == deploy.POLICY_ACTIVATE else payload['action']
            if action == 'activate' and local.failed_writes:
                raise RuntimeError('injected activation failure')
            script = script.replace("Path('/data/app.db')", f'Path({str(database)!r})')
            script = script.replace("sqlite3.connect('/data/app.db',", f'sqlite3.connect({str(database)!r},')
            if local.probe_time is not None:
                script = script.replace('now = datetime.fromisoformat(db.now())',
                                        f'now = datetime.fromisoformat({local.probe_time!r})')
            result = subprocess.run([sys.executable, '-c', script], input=json.dumps(payload), text=True,
                capture_output=True, cwd=root, timeout=10,
                env={'PATH': os.environ['PATH'], 'HOME': str(tmp_path), 'PYTHONPATH': str(root),
                     'A_STOCK_CONFIG_FILE': str(config)})
            if result.returncode:
                raise RuntimeError('local policy command failed')
            if action == 'activate' and local.lose_reply:
                local.lose_reply = False
                raise RuntimeError('injected lost activation receipt')
            if action == 'activate' and local.after_activate:
                hook, local.after_activate = local.after_activate, None
                hook()
            return json.loads(result.stdout)
        monkeypatch.setattr(deploy, 'policy_command', command)
        local.command = command
        params = parameters or risk_budget.policy()
        candidate = command(deploy.POLICY_PROBE, dict(action='candidate', parameters=params), image='old-worker')
        local.old = candidate
        local.activation = command(deploy.POLICY_ACTIVATE, dict(identity=candidate['identity'], parameters=params,
            owner_token=token, expected_id=0, request_id='predecessor', reason='synthetic initial fixture'))
        local.target = command(deploy.POLICY_PROBE, dict(action='candidate', parameters=params), image='new-worker')
        local.approval = dict(identity=local.target['identity'], expected_id=local.activation['id'],
                             request_id='approved-release-once', reason='isolated exact release approval', owner_token=token)
        local.approval_path = tmp_path / 'approval.json'
        def approve():
            local.approval_path.write_text(json.dumps(local.approval))
            local.approval_path.chmod(0o600)
            return local.approval_path
        local.approve = approve
        def count():
            with closing(sqlite3.connect(database)) as conn:
                return conn.execute('SELECT COUNT(*) FROM policy_activations').fetchone()[0]
        local.count = count
        if not state:
            monkeypatch.setattr(deploy, 'describe', lambda service: dict(image=local.current, running=True))
        return local
    return setup


@pytest.fixture
def daily_bundle(tmp_path, monkeypatch):
    from dataclasses import asdict
    from datetime import datetime

    from a_share_research.policy.v0 import commands_monitor, domain, market_quotes, risk_budget
    from support.evaluations import DAILY_NOW as NOW
    from support.evaluations import provider
    from support.policy_replay import HUADONG_DIVIDENDS, HUADONG_EVENTS, _seed_holding

    path = tmp_path / 'app.db'
    monkeypatch.setenv('POLICY_V0_DB', str(path))
    monkeypatch.setenv('A_STOCK_CONFIG_FILE', str(tmp_path / 'missing-runtime.env'))
    monkeypatch.delenv('POLICY_V0_ACCOUNT_SHADOW', raising=False)
    monkeypatch.delenv('POLICY_V0_ANALYSIS_SHADOW', raising=False)
    monkeypatch.setattr(domain, 'cst_now', lambda: datetime.fromisoformat(NOW))
    monkeypatch.setattr(domain, 'cst_today', lambda: '2026-10-22')
    monkeypatch.setattr(domain, 'utc_now', lambda: datetime.fromisoformat('2026-10-22T07:30:05+00:00'))
    _seed_holding(path, '000963', shares=800)
    local = commands_monitor._load_monitor_local_snapshot()
    row = local['holdings'][0]
    normalized = [dict(div_proc='实施', record_date=d['record_date'], ex_date=d['ex_date'],
                       pay_date=d['pay_date'], cash_div_tax=d['cash_div'], stk_div=d['split_ratio'])
                  for d in HUADONG_DIVIDENDS]
    return dict(local=local, quotes={'000963': asdict(market_quotes.PriceQuote(
                    26.21, '2026-10-22', '15:00:00', 'historical-boundary-fixture', previous_close=26.59))},
                portfolio_value=None, industry_result=None, now=NOW, risk_policy=risk_budget.policy(),
                denominator_evidence={}, account_scope_required=False, research_result={'status': 'not_configured'},
                dividend_today='2026-10-22', brief_today='2026-10-22', dividends=[
                    dict(holding_id=row['id'], code=row['code'], name=row['name'], current_shares=row['shares'],
                         events=[list(event) for event in HUADONG_EVENTS], provider=provider(normalized))])


@pytest.fixture
def reading_server(tmp_path, monkeypatch):
    """Exercise the HTTP app in process, without a listener or uvicorn subprocess."""
    from fastapi.testclient import TestClient

    from a_share_research import api_keys, db, service
    from support.delivery import saved

    monkeypatch.setenv('PUBLIC_BASE_URL', 'http://localhost')
    monkeypatch.setenv('SESSION_SECRET', 'isolated-test-only-not-a-real-secret')
    monkeypatch.setenv('DEV_LOGIN', '1')
    from a_share_research import web

    conn = db.connect(tmp_path / 'app.db')
    monkeypatch.setattr(web, 'conn', conn)
    monkeypatch.setattr(web, 'settings', web.Settings.from_env())
    run, report = saved(conn)
    _, failed = saved(conn, passed=False)
    queued = service.submit(conn, '600519', 'lost-http-response', question='现金流')
    token = api_keys.create(conn, 'isolated-http-test')
    with TestClient(web.app, base_url='http://localhost',
                    headers={'Authorization': 'Bearer ' + token}) as client:
        yield conn, client, 'http://localhost', run, report, failed, queued
    conn.close()


@pytest.fixture
def backup_source(tmp_path):
    from datetime import datetime
    from decimal import Decimal

    from a_share_research import api_keys, db
    from a_share_research.account import evidence, ledger
    from support.backup import research_db

    directory = tmp_path / 'source'
    directory.mkdir(mode=0o700)
    conn, run = research_db(directory)
    token = api_keys.create(conn, 'invented-backup-account', role='assistant')
    principal = api_keys.authenticate(conn, token)
    sha = evidence.store(directory / 'account-evidence', b'INVENTED ACCOUNT OPENING; NOT BROKER OR INVESTMENT EVIDENCE')
    ledger.initialize(conn, principal, 'structural', dict(positions={}, cash='10000'),
        datetime.fromisoformat('2026-11-01T00:00:00+08:00'), directory / 'account-evidence', sha, tolerance=Decimal(0))
    # Allocate and delete a run so preserving sqlite_sequence is observably different from MAX(id).
    removed = db.create_run(conn, '600001', status='queued')
    conn.execute('DELETE FROM runs WHERE id=?', (removed,))
    yield directory, conn, principal, sha, run
    conn.close()


@pytest.fixture
def book(tmp_path):
    from datetime import datetime
    from decimal import Decimal

    from a_share_research import api_keys, db
    from a_share_research.account import evidence, ledger
    from a_share_research.account.reconciliation import Opening

    conn = db.connect(tmp_path / 'app.db')
    principal = api_keys.authenticate(conn, api_keys.create(conn, 'account', role='assistant'))
    directory = tmp_path / 'evidence'
    sha = evidence.store(directory, b'Structural account test evidence, not a broker statement')
    ledger.initialize(conn, principal, 'a', Opening(positions={}, cash='200000'),
                      datetime.fromisoformat('2026-10-01T00:00:00+08:00'), directory, sha, tolerance=Decimal('0'))
    yield conn, principal, directory, sha
    conn.close()


@pytest.fixture
def branch(tmp_path):
    from datetime import datetime
    from decimal import Decimal

    from a_share_research import api_keys, db
    from a_share_research.account import analysis, evidence, ledger, position_store
    from support.positions import seed

    conn = db.connect(tmp_path / 'app.db')
    analysis.migrate(conn)
    principal = api_keys.authenticate(conn, api_keys.create(conn, 'structural', role='assistant'))
    directory = tmp_path / 'evidence'
    sha = evidence.store(directory, b'Structural analysis evidence; no broker or investment assertion')
    conn.execute("INSERT INTO holdings(id,code,name) VALUES (1,'600001','Structural fixture')")
    conn.execute("INSERT INTO holdings(id,code,name) VALUES (2,'600002','Other structural fixture')")
    conn.execute("INSERT INTO holding_thesis_versions(id,holding_id,version,l1,l2,rewrite_reason,status,created_at)"
                 " VALUES (1,1,1,'Structural L1','Structural L2','fixture','active','2026-11-01')")
    for identity, holding, active in ((1,1,1),(2,1,0),(3,2,1)):
        conn.execute("INSERT INTO holding_l3_conditions(id,holding_id,condition_text,created_at,updated_at,is_active)"
                     " VALUES (?,?, 'Structural condition','2026-11-01','2026-11-01',?)", (identity,holding,active))
    for identity, status in ((1,'active'),(2,'pending'),(3,'resolved')):
        conn.execute("INSERT INTO holding_alerts(id,holding_id,code,level,category,reason_code,reason,status,opened_at,updated_at)"
                     " VALUES (?,1,'600001','yellow','unverified',?,'Structural alert',?,'2026-11-01','2026-11-01')",
                     (identity,f'structural-{identity}',status))
    conn.execute("INSERT INTO holding_tier_state(holding_id,updated_at) VALUES (1,'2026-11-01')")
    seeds = [seed(), seed(code='600002', cycle_id='legacy:2', legacy_holding_id=2)]
    ledger.initialize(conn, principal, 'a', dict(positions={s.code: s.shares for s in seeds}, cash='10000'),
                      datetime.fromisoformat('2026-11-01T00:00:00+08:00'), directory, sha, tolerance=Decimal(0))
    position_store.initialize(conn, principal, 'a', seeds, directory, [sha])
    yield conn, principal, directory, sha
    conn.close()
