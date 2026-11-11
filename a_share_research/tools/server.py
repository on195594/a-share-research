"""Local read-only tools service. Schema migration belongs to the release process."""
import logging
import argparse
import json
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Depends
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from a_share_research import api_keys, db
from a_share_research.tools.registry import Context, ToolError, TOOLS, discover, invoke
from a_share_research.tools import control

logger = logging.getLogger(__name__)


def create_app(database: Path, *, external_enabled: bool = False, external_review_enabled: bool = False,
               account_shadow_enabled: bool = False, gate: control.Gate | None = None) -> FastAPI:
    if external_review_enabled and not external_enabled:
        raise ValueError('外部复核必须同时启用外部研究入口')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    gate = gate or control.Gate()
    app.state.gate = gate
    mode = 'account-shadow' if account_shadow_enabled else 'external-staging' if external_enabled else 'read-only'

    @app.exception_handler(control.ControlError)
    async def control_error(request, exc):
        return JSONResponse({'ok':False,'error':{'code':exc.code,'message':str(exc)}},status_code=exc.status)

    @app.exception_handler(ToolError)
    async def tool_error(request, exc):
        return JSONResponse({'ok': False, 'error': {'code': exc.code, 'message': str(exc)}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({'ok': False, 'error': {'code': 'invalid_arguments',
                             'message': '请求须为符合工具 input_schema 的 JSON 对象'}}, status_code=422)

    @contextmanager
    def context(request, name=None):
        header = request.headers.get('authorization', '')
        if not header.startswith('Bearer '):
            raise ToolError('unauthorized', '需要 bearer 凭据', 401)
        with gate.acquire():
            with connection(header, name) as ctx:
                yield ctx

    @contextmanager
    def connection(header, name):
        conn = None
        try:
            tool = TOOLS.get(name)
            writable = tool is not None and tool.access == 'write' and (
                external_enabled and tool.external or account_shadow_enabled and tool.account)
            mode = 'rw' if writable else 'ro'
            conn = sqlite3.connect(database.resolve().as_uri() + '?mode=' + mode, uri=True,
                                   timeout=5, isolation_level=None)
            conn.execute('PRAGMA foreign_keys=ON')
            conn.row_factory = sqlite3.Row
            principal = api_keys.authenticate(conn, header[7:])
            if principal is None:
                raise ToolError('unauthorized', '凭据无效或已撤销', 401)
            yield Context(conn, database, principal, external_enabled, external_review_enabled, account_shadow_enabled)
        except sqlite3.Error:
            logger.exception('Tool database unavailable')
            raise ToolError('service_failure' if name and name.startswith('account.') else 'unavailable',
                            '工具数据库不可用；未返回成功回执，可用原请求重试', 503) from None
        finally:
            if conn is not None:
                conn.close()

    @app.get('/health')
    def health():
        return {'status': 'ok', 'mode': mode}

    @app.get('/ready')
    def ready():
        state = gate.status()
        result = dict(status='not-ready',mode=mode,schema_version=None,database_ready=False,**state)
        try:
            with closing(sqlite3.connect(database.absolute().as_uri()+'?mode=ro',uri=True,timeout=1)) as conn:
                conn.execute('BEGIN')
                result['schema_version'] = conn.execute('PRAGMA user_version').fetchone()[0]
                if result['schema_version'] != len(db.MIGRATIONS):
                    return JSONResponse(result,status_code=503)
                expected = {'api_keys','runs','reports','prices','batches',*db.ACCOUNT_TABLES}
                if external_enabled:
                    expected.update(('external_research','external_submissions','external_reviews'))
                if account_shadow_enabled:
                    expected.update(('account_books','account_batches','account_journal','account_voids','account_observations',
                                     'account_analysis_history','account_position_books','account_projection_ids',
                                     'policy_v0_profiles','policy_v0_account_openings',
                                     'policy_versions','policy_activations','policy_evaluations'))
                    conn.execute('SELECT account_id,cycle_id FROM account_analysis_history LIMIT 0')
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                conn.execute('SELECT role FROM api_keys LIMIT 0')
                if not expected <= tables:
                    return JSONResponse(result,status_code=503)
        except sqlite3.Error:
            return JSONResponse(result,status_code=503)
        result.update(database_ready=True,**gate.status())
        if result['paused']:
            return JSONResponse(result,status_code=503)
        return result | {'status':'ready'}

    @app.get('/tools')
    def tools(request: Request):
        with context(request) as ctx:
            return {'tools': discover(ctx.principal, external_enabled=external_enabled, account_shadow_enabled=account_shadow_enabled)}

    async def arguments_json(request: Request):
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate JSON key')
                result[key] = value
            return result
        def invalid_constant(value):
            raise ValueError('nonfinite JSON number')
        try:
            arguments = json.loads(await request.body(), object_pairs_hook=unique, parse_constant=invalid_constant)
            if not isinstance(arguments, dict):
                raise ValueError('object required')
            return arguments
        except (ValueError, UnicodeError):
            raise ToolError('invalid_arguments', '请求须为无重复键、无非有限数字的JSON对象', 422) from None

    @app.post('/tools/{name}')
    def call(name: str, request: Request, arguments: dict = Depends(arguments_json)):
        with context(request, name) as ctx:
            return {'ok': True, 'result': invoke(ctx, name, arguments)}

    return app


def main():
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',choices=('127.0.0.1','0.0.0.0'),default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8621)
    parser.add_argument('--control-socket',type=Path,default=Path('/run/ashare-tools/control.sock'))
    args = parser.parse_args()
    def enabled(name):
        value = os.environ.get(name,'0')
        if value not in ('0','1'):
            parser.error(f'{name} 只能为 0 或 1')
        return value == '1'
    database = Path(os.environ.get('DATA_DIR',str(db.DATA))).absolute()/'app.db'
    gate = control.Gate(marker=database.parent/'.tools-paused')
    app = create_app(database,gate=gate,external_enabled=enabled('ASHARE_TOOLS_EXTERNAL_ENABLED'),
                     external_review_enabled=enabled('ASHARE_TOOLS_EXTERNAL_REVIEW_ENABLED'),
                     account_shadow_enabled=enabled('ASHARE_TOOLS_ACCOUNT_SHADOW_ENABLED'))
    with control.ControlServer(gate,args.control_socket):
        uvicorn.run(app,host=args.host,port=args.port,workers=1,timeout_graceful_shutdown=60)


if __name__ == '__main__':
    main()
