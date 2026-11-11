"""Local read-only tools service. Schema migration belongs to the release process."""
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from a_share_research import api_keys, db
from a_share_research.tools.registry import Context, ToolError, discover, invoke

logger = logging.getLogger(__name__)


def create_app(database: Path) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    slots = threading.BoundedSemaphore(2)

    @app.exception_handler(ToolError)
    async def tool_error(request, exc):
        return JSONResponse({'ok': False, 'error': {'code': exc.code, 'message': str(exc)}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({'ok': False, 'error': {'code': 'invalid_arguments',
                             'message': '请求须为符合工具 input_schema 的 JSON 对象'}}, status_code=422)

    @contextmanager
    def context(request):
        header = request.headers.get('authorization', '')
        if not header.startswith('Bearer '):
            raise ToolError('unauthorized', '需要 bearer 凭据', 401)
        if not slots.acquire(blocking=False):
            raise ToolError('busy', '工具服务繁忙；请稍后重试', 503)
        conn = None
        try:
            conn = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            principal = api_keys.authenticate(conn, header[7:])
            if principal is None:
                raise ToolError('unauthorized', '凭据无效或已撤销', 401)
            yield Context(conn, database, principal)
        except sqlite3.Error:
            logger.exception('Tool database unavailable')
            raise ToolError('unavailable', '工具数据库不可用', 503) from None
        finally:
            if conn is not None:
                conn.close()
            slots.release()

    @app.get('/health')
    def health():
        return {'status': 'ok', 'mode': 'read-only'}

    @app.get('/tools')
    def tools(request: Request):
        with context(request) as ctx:
            return {'tools': discover(ctx.principal)}

    @app.post('/tools/{name}')
    def call(name: str, arguments: dict, request: Request):
        with context(request) as ctx:
            return {'ok': True, 'result': invoke(ctx, name, arguments)}

    return app


def main():
    import uvicorn
    uvicorn.run(create_app(Path(os.environ.get('DATA_DIR', str(db.DATA))) / 'app.db'),
                host='127.0.0.1', port=8621, workers=1)


if __name__ == '__main__':
    main()
