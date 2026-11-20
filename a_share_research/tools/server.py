"""Local tools service. Schema migration belongs to the release process."""
import logging
import argparse
import os
import sqlite3
from contextlib import ExitStack, closing, contextmanager
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, Request, Depends
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from a_share_research import api_keys, db
from a_share_research.tools.http_json import BodyTooLarge, parse_object, read_body
from a_share_research.tools.registry import Context, ToolError, TOOLS, authorize_tool, check_tool_scope, discover, invoke, validation_message
from a_share_research.tools import control

logger = logging.getLogger(__name__)


def create_app(database: Path, *, account_shadow_enabled: bool = False,
               gate: control.Gate | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    gate = gate or control.Gate()
    app.state.gate = gate
    mode = 'account-shadow' if account_shadow_enabled else 'read-only'

    @app.exception_handler(control.ControlError)
    async def control_error(request, exc):
        return JSONResponse({'ok':False,'error':{'code':exc.code,'message':str(exc)}},status_code=exc.status)

    @app.exception_handler(ToolError)
    async def tool_error(request, exc):
        return JSONResponse({'ok': False, 'error': {'code': exc.code, 'message': str(exc)}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({'ok': False, 'error': {'code': 'invalid_arguments',
                             'message': validation_message(exc.errors())}}, status_code=422)

    @contextmanager
    def connection(mode):
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=' + mode, uri=True,
                                     timeout=5, isolation_level=None)) as conn:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.row_factory = sqlite3.Row
            yield conn

    @contextmanager
    def context(request, name=None, *, principal=None):
        tool = TOOLS.get(name)
        try:
            with gate.acquire(limited=False), ExitStack() as resources:
                bearer = request.headers.get('authorization', '')
                if principal is None and not bearer.startswith('Bearer '):
                    raise ToolError('unauthorized', '需要 bearer 凭据', 401)
                with connection('ro') as conn:
                    principal = principal or api_keys.authenticate(conn, bearer[7:])
                    if principal is None:
                        raise ToolError('unauthorized', '凭据无效或已撤销', 401)
                    ctx = Context(conn, database, principal, account_shadow_enabled)
                    if tool and (tool.access == 'write' or tool.collects):
                        if tool.access == 'write':
                            authorize_tool(ctx, tool)
                        else:
                            check_tool_scope(ctx, tool)
                        resources.enter_context(gate.slot())
                    if tool is None or tool.access == 'read':
                        yield ctx
                        return
                with connection('rw') as conn:
                    yield replace(ctx, connection=conn)
        except sqlite3.Error:
            logger.exception('Tool database unavailable')
            account_tool = account_shadow_enabled and tool and tool.name.startswith('account_')
            raise ToolError('service_failure' if account_tool else 'unavailable',
                            '工具数据库不可用；未返回成功回执，可用原请求重试', 503) from None

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
                expected = {'api_keys','runs','reports','prices','batches','scan_requests',*db.ACCOUNT_TABLES,
                            *db.V25_TABLES}
                if account_shadow_enabled:
                    expected.update(('account_books','account_batches','account_journal','account_voids','account_observations',
                                     'account_analysis_history','account_position_books','account_projection_ids',
                                     'policy_v0_profiles','policy_v0_account_openings',
                                     'policy_versions','policy_activations','policy_evaluations'))
                    conn.execute('SELECT account_id,cycle_id FROM account_analysis_history LIMIT 0')
                    conn.execute('SELECT capture_context,capture_context_hash FROM policy_evaluations LIMIT 0')
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                conn.execute('SELECT role,kind,client,revoked_at FROM api_keys LIMIT 0')
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
            return {'tools': discover(ctx.principal, account_shadow_enabled=account_shadow_enabled)}

    async def arguments_json(request: Request):
        try:
            return parse_object(await read_body(request))
        except BodyTooLarge as exc:
            raise ToolError('invalid_arguments', str(exc), 413) from None
        except ValueError as exc:
            raise RequestValidationError([{'loc': ('body',), 'msg': str(exc)}]) from None

    @app.post('/tools/{name}')
    def call(name: str, request: Request, arguments: dict = Depends(arguments_json)):
        with context(request, name) as ctx:
            return {'ok': True, 'result': invoke(ctx, name, arguments)}

    if resource := os.environ.get('ASHARE_PUBLIC_MCP_URL'):
        from a_share_research.tools.remote_mcp import mcp_router
        app.include_router(mcp_router(resource, connection, context, gate))

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
    app = create_app(database,gate=gate,account_shadow_enabled=enabled('ASHARE_TOOLS_ACCOUNT_SHADOW_ENABLED'))
    with control.ControlServer(gate,args.control_socket):
        uvicorn.run(app,host=args.host,port=args.port,workers=1,timeout_graceful_shutdown=60)


if __name__ == '__main__':
    main()
