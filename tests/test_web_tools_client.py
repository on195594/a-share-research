"""Web proxy contracts with the in-process MCP endpoint and transport failures."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
import requests

from a_share_research import api_keys
from a_share_research.tools import remote_mcp
from a_share_research.tools.control import Gate
from a_share_research.tools.server import create_app
from a_share_research.tools_client import ToolCallError, ToolsClient
from test_account_ledger import batch, book  # noqa: F401
from test_tools_remote_mcp import ORIGIN, RESOURCE


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


def test_session_is_reused_only_within_one_thread(monkeypatch):
    client = ToolsClient("http://tools/mcp", "stock.example", "private-test")
    sessions = []

    def post(session, *args, **kwargs):
        sessions.append(session)
        return response(rpc(result={"structuredContent": {"data": {}}}))

    monkeypatch.setattr(requests.Session, "post", post)
    client._call("account_state", {}, REQUEST_ID)
    client._call("account_state", {}, REQUEST_ID)
    with ThreadPoolExecutor(max_workers=1) as worker:
        worker.submit(client._call, "account_state", {}, REQUEST_ID).result()
    assert sessions[0] is sessions[1]
    assert sessions[0] is not sessions[2]


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


@pytest.fixture
def mcp_http(tmp_path, monkeypatch):
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
