"""Web proxy contracts with the in-process MCP endpoint and transport failures."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import requests

from a_share_research import api_keys
from a_share_research.tools_client import ToolCallError, ToolsClient, _reply
from support.accounts import batch
from support.mcp import ORIGIN


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

    def send(reply, read_only=False):
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
        return client._call("account_state", {}, REQUEST_ID, read_only=read_only)

    return send


def test_route_and_identity_use_internal_endpoint_and_public_host(call):
    assert call(response(rpc(result={"structuredContent": {"data": {"version": 1}}}))) == {"version": 1}


@pytest.mark.parametrize(("reply", "kind", "message"), [
    (response(tool_error("paused", "发布中，约 1 分钟后用原请求重试")), "unavailable", "发布中"),
    (response({}, 429), "unavailable", "限流"),
    (response({"error": {"code": -32603, "data": {"code": "paused"}}}, 503), "unavailable", "发布中"),
    (requests.ReadTimeout(), "unknown", "同一请求编号"),
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


def test_web_grant_and_gate_against_mcp(book, bind_account, tmp_path, monkeypatch, mcp_http):  # noqa: F811
    conn, _, directory, _ = book
    directory.rename(tmp_path / "account-evidence")
    bind_account(conn, "a")
    token = api_keys.create(conn, "web-owner-proxy", role="assistant")
    key_id = api_keys.authenticate(conn, token).key_id
    endpoint = "http://a-share-research-tools:8621"
    proxy = ToolsClient.from_settings(SimpleNamespace(tools_url=endpoint, base_url=ORIGIN, tools_token=token))
    server, gate = mcp_http

    def send(session, request, **kwargs):
        assert request.url == endpoint + "/mcp"
        return server.post(request.url, headers=dict(request.headers), content=request.body)

    monkeypatch.setattr(requests.Session, "send", send)
    for client, message in [(replace(proxy, host="a-share-research-tools:8621"), "HTTP 421"),
                            (replace(proxy, token="invalid"), "授权已失效")]:
        with pytest.raises(ToolCallError, match=message) as error:
            client._call("account_state", {}, REQUEST_ID)
        assert error.value.kind == "business"

    with pytest.raises(ToolCallError, match="account_id") as error:
        proxy._call("account_state", {"account_id": "other"}, REQUEST_ID)
    assert (error.value.kind, error.value.code) == ("business", "conflict")

    args = {"batch": batch(book).model_dump(mode="json")}
    with gate.slot(), pytest.raises(ToolCallError, match="名额已满") as busy:
        proxy._call("account_submit", args, REQUEST_ID)
    assert busy.value.kind == "unavailable"
    gate.pause()
    with pytest.raises(ToolCallError, match="发布中") as paused:
        proxy._call("account_submit", args, REQUEST_ID)
    assert paused.value.kind == "unavailable"
    gate.resume()
    assert proxy._call("account_state", {}, REQUEST_ID)["version"] == 0
    receipt = proxy._call("account_submit", args, REQUEST_ID)
    assert receipt["status"] == "posted" and receipt["key_id"] == key_id
    assert proxy._call("account_receipt", {"receipt_id": receipt["receipt_id"]}, REQUEST_ID) == receipt


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("exception,message", [
    (requests.ConnectTimeout, "连接工具服务超时"),
    (requests.ReadTimeout, "响应超时"),
    (requests.Timeout, "响应超时"),
    (requests.ConnectionError, "连接错误"),
    (requests.RequestException, "连接错误"),
])
def test_transport_failure_distinguishes_read_only_and_unsent_requests(call, read_only, exception, message):
    retryable = read_only or exception is requests.ConnectTimeout
    with pytest.raises(ToolCallError) as error:
        call(exception(), read_only=read_only)
    assert error.value.kind == ("unavailable" if retryable else "unknown")
    assert (message if retryable else "同一请求编号") in str(error.value)
    assert ("结果未知" in str(error.value)) is not retryable


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("code", ["unavailable", "service_failure", "timeout"])
def test_reply_service_failure_uses_read_only_flag(call, read_only, code):
    reply = response(tool_error(code, "上游超时，请重试"))
    # Exercise both the parser and propagation from _call.
    for invoke in (lambda: _reply(reply, REQUEST_ID, read_only=read_only),
                   lambda: call(reply, read_only=read_only)):
        with pytest.raises(ToolCallError) as error:
            invoke()
        assert error.value.kind == ("unavailable" if read_only else "unknown")
        assert "上游超时" in str(error.value)
        assert ("结果未知" in str(error.value)) is not read_only


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("reply", [response({}, 500), response({}, 408), response(b"broken")])
def test_reply_missing_receipt_is_safe_to_retry_only_for_reads(call, read_only, reply):
    with pytest.raises(ToolCallError) as error:
        call(reply, read_only=read_only)
    assert error.value.kind == ("unavailable" if read_only else "unknown")
    assert ("结果未知" in str(error.value)) is not read_only
