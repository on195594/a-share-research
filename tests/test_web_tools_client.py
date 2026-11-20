"""Wire contract with an in-process HTTP stand-in. No sockets or bearer credentials leave the test."""
import json
from types import SimpleNamespace

import pytest
import requests

from a_share_research.tools_client import ToolCallError, ToolsClient


def response(body, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return result


REQUEST_ID = "original-form-id"


def rpc(**fields):
    return {"jsonrpc": "2.0", "id": REQUEST_ID, **fields}


def tool_error(code, message):
    detail = json.dumps({"error": {"code": code, "message": message}})
    return rpc(result={"isError": True, "content": [{"type": "text", "text": detail}]})


@pytest.fixture
def call(monkeypatch):
    settings = SimpleNamespace(tools_url="http://a-share-research-tools:8621", tools_token="private-test",
                               base_url="https://stock.example:8443")
    client = ToolsClient.from_settings(settings)

    def send(reply):
        def post(http, url, *, data, headers, allow_redirects, timeout):
            assert url == "http://a-share-research-tools:8621/mcp"
            assert headers["Host"] == "stock.example:8443" and headers["Authorization"] == "Bearer private-test"
            assert not http.trust_env and not allow_redirects
            request = json.loads(data)
            assert request["id"] == REQUEST_ID
            assert headers["Mcp-Method"] == request["method"] == "tools/call"
            assert headers["Mcp-Name"] == request["params"]["name"] == "account_state"
            assert request["params"]["_meta"] == {"io.modelcontextprotocol/clientCapabilities": {},
                "io.modelcontextprotocol/protocolVersion": headers["MCP-Protocol-Version"]}
            if isinstance(reply, Exception):
                raise reply
            return reply

        monkeypatch.setattr(requests.Session, "post", post)
        return client._call("account_state", {}, REQUEST_ID)

    return send


def test_route_and_identity_use_internal_endpoint_and_public_host(call):
    assert call(response(rpc(result={"structuredContent": {"data": {"version": 1}}}))) == {"version": 1}


@pytest.mark.parametrize(("reply", "kind", "message"), [
    (response(tool_error("paused", "发布中，约 1 分钟后用原请求重试")), "unavailable", "发布中"),
    (response(tool_error("busy", "写入或采集名额已满，请稍后用原请求重试")), "unavailable", "名额已满"),
    (response({}, 429), "unavailable", "限流"),
    (response({"error": {"code": -32603, "data": {"code": "paused"}}}, 503), "unavailable", "发布中"),
    (response(b"", 503), "unknown", "同一请求编号"),
    (requests.ReadTimeout(), "unknown", "同一请求编号"),
    (requests.exceptions.ChunkedEncodingError(), "unknown", "同一请求编号"),
    (response(b'{"jsonrpc":'), "unknown", "同一请求编号"),
    (response(rpc(result={}) | {"id": "some-other-request"}), "unknown", "同一请求编号"),
    (response(tool_error("invalid_arguments", "batch.snapshot.market_values: 缺 600519 市值")),
     "business", "batch.snapshot.market_values: 缺 600519 市值"),
    (response(rpc(error={"code": -32602, "message": "arguments: 缺少字段"}), 400), "business", "arguments: 缺少字段"),
    (response({}, 401), "business", "授权已失效"),
])
def test_page_recovery_depends_on_the_receipt(call, reply, kind, message):
    with pytest.raises(ToolCallError) as error:
        call(reply)
    assert error.value.kind == kind and message in str(error.value)
