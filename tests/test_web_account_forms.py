"""Owner routes, multipart uploads and the installed Skill, without a listening web server."""
import asyncio
import hashlib
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest

from a_share_research import api_keys, skill_bundle
from a_share_research.tools import registry
from a_share_research.tools_client import ToolCallError
from test_overview import owner_request, page_request  # noqa: F401


@pytest.fixture
def upload(web, monkeypatch, tmp_path):
    token = api_keys.create(web.conn, "web-owner-proxy", role="assistant")
    settings = replace(web.settings, tools_url="http://tools:8621", tools_token=token)
    monkeypatch.setattr(web, "settings", settings)
    principal = api_keys.authenticate(web.conn, token)
    ctx = registry.Context(web.conn, tmp_path / "app.db", principal, account_shadow_enabled=True)

    async def call(client, name, arguments, *, request_id=None):
        assert api_keys.authenticate(web.conn, client.token) == principal
        try:
            return registry.invoke(ctx, name, arguments)["data"]
        except registry.ToolError as exc:
            raise ToolCallError("business", str(exc)) from exc

    monkeypatch.setattr(web.ToolsClient, "call", call)
    return principal


@pytest.mark.parametrize("partial_failure", [False, True])
def test_multipart_preserves_files_labels_and_individual_results(web, owner_request, upload, partial_failure):  # noqa: F811
    route = owner_request("/account/evidence")
    captures = sorted((Path(__file__).parent / "data").glob("*-announcement.pdf"))
    files = [("files", (p.name, p.read_bytes(), "application/pdf")) for p in captures]
    if partial_failure:
        files.insert(1, ('files', ('x' * 201 + '.pdf', captures[0].read_bytes(), 'application/pdf')))
    async def visit():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url=web.settings.base_url,
                                     cookies=dict(route.cookies)) as browser:
            fields = {"csrf": web.csrf_token(web.current_session(route)), "label": "收盘核对"}
            refused = await browser.post(route.url.path, headers={"Origin": "https://other.example"},
                                         data=fields, files=files)
            assert refused.status_code == 403
            return await browser.post(route.url.path, headers={"Origin": web.settings.base_url}, data=fields, files=files)

    result = asyncio.run(visit())
    body = result.text
    assert result.status_code == 200 and body.count("上传成功") == len(captures) == 2
    assert ("filename:" in body and "上传失败" in body) == partial_failure
    assert 'accept="image/*,application/pdf" multiple' in body and "20 MiB" in body
    records = list(web.conn.execute("SELECT * FROM account_evidence_uploads ORDER BY id"))
    assert [row["filename"] for row in records] == [p.name for p in captures]
    assert all(row["key_id"] == upload.key_id and row["label"] == "收盘核对" for row in records)
    for row, p in zip(records, captures, strict=True):
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
        assert row["sha256"] == sha and body.count(sha[:12]) >= 2


def test_account_forms_require_owner_session_csrf_and_tools_settings(web, owner_request, monkeypatch):  # noqa: F811
    monkeypatch.setattr(web, "settings", replace(web.settings, tools_url="", tools_token=""))

    async def visit():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url=web.settings.base_url,
                                     headers={"Origin": web.settings.base_url}) as browser:
            for path in ("/account/evidence", "/account/ledger/new", "/account/nav/new"):
                browser.cookies.clear()
                assert (await browser.get(path)).headers["location"] == "/login"
                browser.cookies.update(owner_request(path).cookies)
                page = await browser.get(path)
                assert "Web 账户写入未启用" in page.text and " disabled" in page.text
                assert (await browser.post(path, data={"csrf": "wrong"})).status_code == 403

    asyncio.run(visit())


def test_expired_ledger_session_returns_to_the_draft_after_login(web):
    request = page_request("/account/ledger/new")
    response = asyncio.run(web.ledger_new(request))
    cookie = response.headers["set-cookie"].split(";", 1)[0]
    resumed = web.start_session(page_request("/auth/dev", cookie=cookie))
    assert resumed.headers["location"] == "/account/ledger/new"


def test_skill_download_matches_the_mcp_version_and_repository_contents(web, owner_request):  # noqa: F811
    assert web.download_skill(page_request("/connections/skill.zip")).headers["location"] == "/login"
    assert '/connections/skill.zip' in web.connections(owner_request("/connections")).body.decode()
    reply = web.download_skill(owner_request("/connections/skill.zip"))
    with ZipFile(BytesIO(reply.body)) as package:
        files = {p.relative_to(skill_bundle.SKILL_DIR.parent).as_posix(): p.read_bytes()
                 for p in skill_bundle.SKILL_DIR.rglob("*") if p.is_file()}
        assert {name: package.read(name) for name in package.namelist()} == files
        version = hashlib.sha256(package.read("a-share/SKILL.md")).hexdigest()[:12]
    assert reply.media_type == "application/zip"
    assert reply.headers["content-disposition"] == f'attachment; filename="a-share-{version}.zip"'
