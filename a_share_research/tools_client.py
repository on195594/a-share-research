"""Call account tools on the internal network with the public MCP identity."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from threading import local
from typing import TYPE_CHECKING, Literal, Self
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from starlette.concurrency import run_in_threadpool

from a_share_research.tools.http_json import parse_object

if TYPE_CHECKING:
    from a_share_research.web import Settings

_VERSION = "2026-07-28"
_UNKNOWN = "结果未知。请先核对回执，重试时使用同一请求编号和原内容。"
_PAUSED = "发布中，约 1 分钟后用原请求重试。"
_worker = local()


class ToolCallError(Exception):
    def __init__(self, kind: Literal["unavailable", "unknown", "business"], message: str, code: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.code = code


def _reply(response: requests.Response, request_id: str, read_only: bool = False) -> dict:
    status = response.status_code
    if status == 429:
        raise ToolCallError("unavailable", "服务限流，约 1 分钟后用原请求重试。")
    if status in (401, 403):
        raise ToolCallError("business", "Web 账户授权已失效或权限不足，请核对 web-owner-proxy 授权。")
    if status == 503:
        try:
            error = parse_object(response.content).get("error")
        except ValueError:
            error = None
        if isinstance(error, dict):
            if error.get("data") == {"code": "paused"} and error.get("code") == -32603:
                raise ToolCallError("unavailable", _PAUSED)
            if error.get("code") == "auth_unavailable":
                raise ToolCallError("unavailable", "认证服务暂未受理，请稍后用原请求重试。")
    if status >= 500 or status == 408:
        if read_only:
            raise ToolCallError("unavailable", "工具服务暂不可用，请稍后重试。")
        raise ToolCallError("unknown", _UNKNOWN)
    if status != 200 and status not in (400, 404, 422):
        raise ToolCallError("business", f"工具服务拒绝请求（HTTP {status}），请核对账户工具配置。")

    try:
        rpc = parse_object(response.content)
        if (rpc.get("jsonrpc") != "2.0" or rpc.get("id") != request_id
                or ("result" in rpc) == ("error" in rpc)):
            raise ValueError("invalid JSON-RPC envelope")
        if "error" in rpc:
            detail = rpc["error"]
            if detail.get("code") in (-32601, -32602) and isinstance(detail.get("message"), str):
                raise ToolCallError("business", detail["message"])
            raise ValueError("no tool receipt")
        result = rpc["result"]
        if status != 200 or result.get("resultType", "complete") != "complete":
            raise ValueError("incomplete tool result")
        if result.get("isError") is True:
            block, = result["content"]
            if block["type"] != "text":
                raise ValueError("missing error message")
            detail = parse_object(block["text"].encode())["error"]
            code, message = detail["code"], detail["message"]
            if not isinstance(message, str):
                raise ValueError("invalid error message")
            if code in ("paused", "busy"):
                raise ToolCallError("unavailable", message)
            if code in ("unavailable", "service_failure", "timeout"):
                if read_only:
                    raise ToolCallError("unavailable", message)
                raise ToolCallError("unknown", f"{message}。{_UNKNOWN}")
            raise ToolCallError("business", message, code)
        if result.get("isError", False) is not False:
            raise ValueError("invalid tool result")
        data = result["structuredContent"]["data"]
        if not isinstance(data, dict):
            raise ValueError("missing account payload")
        return data
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        if read_only:
            raise ToolCallError("unavailable", "工具服务响应异常，请稍后重试。") from exc
        raise ToolCallError("unknown", _UNKNOWN) from exc


@dataclass(frozen=True)
class ToolsClient:
    endpoint: str
    host: str
    token: str = field(repr=False)

    @classmethod
    def from_settings(cls, settings: Settings) -> Self | None:
        if not settings.tools_url or not settings.tools_token:
            return None
        return cls(settings.tools_url.rstrip("/") + "/mcp", urlsplit(settings.base_url).netloc, settings.tools_token)

    async def call(self, name: str, arguments: dict, *, request_id: str | None = None, read_only: bool = False) -> dict:
        """read_only calls have no receipt, so failures say to retry instead of checking one."""
        return await run_in_threadpool(self._call, name, arguments, request_id or uuid4().hex, read_only)

    def _call(self, name: str, arguments: dict, request_id: str, read_only: bool = False) -> dict:
        params = {"name": name, "arguments": arguments, "_meta": {
            "io.modelcontextprotocol/protocolVersion": _VERSION, "io.modelcontextprotocol/clientCapabilities": {}}}
        headers = {"Authorization": "Bearer " + self.token, "Host": self.host, "Accept": "application/json",
                   "Content-Type": "application/json", "MCP-Protocol-Version": _VERSION,
                   "Mcp-Method": "tools/call", "Mcp-Name": name}
        body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params},
                          allow_nan=False, ensure_ascii=False).encode()
        try:
            if not hasattr(_worker, "session"):
                _worker.session = requests.Session()
                _worker.session.trust_env = False
            response = _worker.session.post(self.endpoint, data=body, headers=headers, allow_redirects=False,
                                            timeout=(5, 60))
        except requests.ConnectTimeout as exc:
            raise ToolCallError("unavailable", "连接工具服务超时，请稍后用原请求重试。") from exc
        except requests.Timeout as exc:
            if read_only:
                raise ToolCallError("unavailable", "工具服务响应超时，请稍后重试。") from exc
            raise ToolCallError("unknown", _UNKNOWN) from exc
        except requests.RequestException as exc:
            if read_only:
                raise ToolCallError("unavailable", "工具服务连接错误，请稍后重试。") from exc
            raise ToolCallError("unknown", _UNKNOWN) from exc
        return _reply(response, request_id, read_only=read_only)
