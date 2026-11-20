"""Stateless, authenticated MCP transport for the tools registry."""
import hashlib
import json
import logging
import sqlite3
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import MCPError
from mcp.shared.inbound import MCP_PROTOCOL_VERSION_HEADER
from mcp.types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from a_share_research import api_keys, oauth_store
from a_share_research.tools.control import ControlError, Gate
from a_share_research.tools.http_json import MAX_BODY_SIZE, BodyTooLarge, parse_object, read_body
from a_share_research.tools.registry import TOOLS, Tool, ToolError, UnknownTool, invoke, validation_message, visible_tools

logger = logging.getLogger(__name__)
_PAUSED = '发布中，约 1 分钟后用原请求重试'
_BUSY = '写入或采集名额已满，请稍后用原请求重试'


def _describe(tool: Tool) -> types.Tool:
    spec = tool.describe()
    return types.Tool(name=tool.name, description=spec['description'],
                      input_schema=spec['input_schema'], output_schema=spec['output_schema'],
                      meta={'win.keyi.stock/registry': {key: spec[key] for key in ('version', 'collects', 'errors')}},
                      annotations=types.ToolAnnotations(read_only_hint=spec['access'] == 'read',
                                                        destructive_hint=tool.destructive,
                                                        idempotent_hint=spec['idempotent']))


def _result(value: dict, *, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type='text', text=json.dumps(value, ensure_ascii=False))],
                                structured_content=None if error else value, is_error=error)


class _BearerEndpoint:
    def __init__(self, manager, resource, origin, hosts, connection, gate):
        self.manager = manager
        self.resource = resource
        self.challenge = {'WWW-Authenticate': f'Bearer resource_metadata="{origin}/.well-known/oauth-protected-resource/mcp"'}
        self.hosts = hosts
        self.origins = {f'{urlsplit(origin).scheme}://{host}' for host in hosts}
        self.connection = connection
        self.gate = gate

    def authenticate(self, token: str) -> api_keys.Principal | None:
        with self.connection('ro') as conn:
            principal = api_keys.authenticate(conn, token)
            if principal is not None:
                return principal if principal.role != 'owner' else None
            grant_id = oauth_store.token_grant(conn, token, kind='access', resource=self.resource)
            if grant_id is not None:
                # The v25 grant constraint fixes the OAuth role to assistant.
                return api_keys.Principal(grant_id, 'assistant')
            return None

    async def paused_response(self, request: Request) -> JSONResponse:
        if request.method == 'POST':
            try:
                rpc = types.JSONRPCRequest.model_validate(parse_object(await read_body(request)))
            except ValueError:
                rpc = None
            if rpc is not None and rpc.method == 'tools/call':
                result = _result({'error': {'code': 'paused', 'message': _PAUSED}}, error=True)
                modern = request.headers.get(MCP_PROTOCOL_VERSION_HEADER) in MODERN_PROTOCOL_VERSIONS
                if modern:
                    result.meta = {types.SERVER_INFO_META_KEY: self.manager.app.server_info_stamp}
                payload = result.model_dump(by_alias=True, exclude_none=True, exclude=set() if modern else {'result_type'})
                return JSONResponse({'jsonrpc': '2.0', 'id': rpc.id, 'result': payload})
        return JSONResponse({'jsonrpc': '2.0', 'id': None,
                             'error': {'code': types.INTERNAL_ERROR, 'message': _PAUSED, 'data': {'code': 'paused'}}},
                            status_code=503, headers={'Retry-After': '60'})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        hosts = request.headers.getlist('host')
        origins = request.headers.getlist('origin')
        if (len(hosts) != 1 or hosts[0].lower() not in self.hosts
                or (origins and (len(origins) != 1 or origins[0].lower() not in self.origins))):
            response = JSONResponse({'error': 'Host 或 Origin 与 MCP 公网地址不符'}, status_code=421)
            return await response(scope, receive, send)
        scheme, _, token = request.headers.get('authorization', '').partition(' ')
        principal = None
        try:
            with self.gate.acquire(limited=False):
                if scheme.lower() == 'bearer' and token:
                    principal = await run_in_threadpool(self.authenticate, token)
        except ControlError:
            response = await self.paused_response(request)
            return await response(scope, receive, send)
        except sqlite3.Error:
            logger.exception('MCP credential database unavailable')
            response = JSONResponse({'error': {'code': 'auth_unavailable', 'message': '认证服务暂不可用，请稍后用原请求重试'}},
                                    status_code=503)
            return await response(scope, receive, send)
        if principal is None:
            response = JSONResponse({'error': '需要有效的 Bearer 凭据'}, status_code=401, headers=self.challenge)
            return await response(scope, receive, send)
        if request.method != 'POST':
            # The legacy SDK GET handler would open a standalone SSE stream.
            response = JSONResponse({'error': '请使用 POST /mcp'}, status_code=405, headers={'Allow': 'POST'})
            return await response(scope, receive, send)

        try:
            body = await read_body(request)
            parse_object(body)
        except ValueError as exc:
            response = JSONResponse({'jsonrpc': '2.0', 'id': None,
                                     'error': {'code': types.PARSE_ERROR, 'message': str(exc)}},
                                    status_code=413 if isinstance(exc, BodyTooLarge) else 400)
            return await response(scope, receive, send)

        async def replay():
            nonlocal body
            if body is None:
                return await receive()
            message = {'type': 'http.request', 'body': body, 'more_body': False}
            body = None
            return message

        request.state.mcp_principal = principal
        await self.manager.handle_request(scope, replay, send)


def mcp_router(resource: str, connection, context, gate: Gate) -> APIRouter:
    url = urlsplit(resource)
    if (url.scheme not in ('https', 'http') or not url.hostname or url.username is not None
            or url.path != '/mcp' or url.query or url.fragment):
        raise ValueError('ASHARE_PUBLIC_MCP_URL 须为完整的公网 /mcp 地址')
    host = f'[{url.hostname}]' if ':' in url.hostname else url.hostname
    default_port = 443 if url.scheme == 'https' else 80
    if url.port in (None, default_port):
        hosts = {host, f'{host}:{default_port}'}
    else:
        host = f'{host}:{url.port}'
        hosts = {host}
    origin = f'{url.scheme}://{host}'
    catalog = {name: _describe(tool) for name, tool in TOOLS.items()}

    def listed(request: Request) -> types.ListToolsResult:
        with context(request, principal=request.state.mcp_principal) as ctx:
            tools = visible_tools(ctx.principal, account_shadow_enabled=ctx.account_shadow_enabled)
            return types.ListToolsResult(tools=[catalog[tool.name] for tool in tools])

    async def list_tools(ctx, params):
        try:
            return await run_in_threadpool(listed, ctx.request)
        except ControlError:
            raise MCPError(types.INTERNAL_ERROR, _PAUSED, {'code': 'paused'}) from None
        except ToolError as exc:
            raise MCPError(types.INTERNAL_ERROR, str(exc)) from None
        except Exception:
            logger.exception('Unexpected MCP tools/list failure')
            raise MCPError(types.INTERNAL_ERROR, '工具目录暂不可用，请稍后重试') from None

    def called(request: Request, name: str, arguments: dict) -> types.CallToolResult:
        try:
            with context(request, name, principal=request.state.mcp_principal) as ctx:
                return _result(invoke(ctx, name, arguments))
        except UnknownTool:
            raise MCPError(types.INVALID_PARAMS, 'name: 工具不存在，请用 tools/list 查询可用名称') from None
        except ControlError as exc:
            code, message = exc.code, _BUSY if exc.code == 'busy' else _PAUSED
        except ToolError as exc:
            code, message = exc.code, str(exc)
            if exc.status in (503, 504) and '重试' not in message:
                message += '。请稍后用原请求重试'
        except Exception:
            logger.exception('Unexpected MCP tool failure for %s', name)
            code, message = 'service_failure', '未取得成功回执，请先查询状态，再用原身份和原请求重试'
        return _result({'error': {'code': code, 'message': message}}, error=True)

    async def call_tool(ctx, params):
        return await run_in_threadpool(called, ctx.request, params.name, params.arguments or {})

    root = Path(__file__).resolve().parents[2]
    with (root / 'pyproject.toml').open('rb') as project:
        version = tomllib.load(project)['project']['version']
    skill_version = hashlib.sha256((root / 'skill/a-share/SKILL.md').read_bytes()).hexdigest()[:12]
    server = Server('a-share', version=version, instructions=(
        f'a-share Skill 版本 {skill_version}。先发现可用工具，读研究摘要，按需读取证据。账户入账前核对凭证和版本。'
        f'提交研究后立即给出任务编号与进度链接（{origin} 加回执 path），本轮无需轮询。'
        '按字段路径纠正参数。凭据失效时刷新或重新授权。发布暂停或繁忙时稍后重试。'
        '超时或断线先查回执，沿用原身份、请求编号及内容。'),
        on_list_tools=list_tools, on_call_tool=call_tool,
        get_tool_input_schema=lambda name: catalog[name].input_schema if name in catalog else None)

    async def protocol_request(ctx, call_next):
        # Business handlers enter the shared Gate in their synchronous connection scope.
        try:
            if ctx.method in ('tools/list', 'tools/call'):
                return await call_next(ctx)
            with gate.acquire(limited=False):
                return await call_next(ctx)
        except ValidationError as exc:
            raise MCPError(types.INVALID_PARAMS, validation_message(exc.errors(include_input=False))) from None
        except ControlError:
            raise MCPError(types.INTERNAL_ERROR, _PAUSED, {'code': 'paused'}) from None
        except MCPError:
            raise
        except Exception:
            logger.exception('Unexpected MCP protocol failure for %s', ctx.method)
            raise MCPError(types.INTERNAL_ERROR, '协议请求暂时无法完成，请稍后重试') from None

    server.middleware.append(protocol_request)
    manager = StreamableHTTPSessionManager(
        server, stateless=True, json_response=True, max_request_body_size=MAX_BODY_SIZE)
    endpoint = _BearerEndpoint(manager, resource, origin, hosts, connection, gate)
    return APIRouter(routes=[Route('/mcp', endpoint)], lifespan=lambda app: manager.run())
