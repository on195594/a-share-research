import json
from pathlib import Path

import pytest
import requests

from a_share_research.policy.v0 import cache, research_client


def test_research_url_keeps_credentials_on_explicit_secure_endpoint():
    assert (
        research_client.endpoint_url("https://stock.keyi.win/")
        == "https://stock.keyi.win"
    )
    assert (
        research_client.endpoint_url("http://127.0.0.1:18620")
        == "http://127.0.0.1:18620"
    )
    for value in (
        "http://example.com",
        "https://user:secret@example.com",
        "https://example.com?token=secret",
        "file:///tmp/x",
    ):
        with pytest.raises(ValueError):
            research_client.endpoint_url(value)


def test_research_cli_requires_idempotency_and_does_not_open_account_db(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "absent.db"
    monkeypatch.setenv("POLICY_V0_DB", str(path))
    monkeypatch.setenv("A_STOCK_CONFIG_FILE", str(tmp_path / "absent.env"))
    monkeypatch.delenv("A_STOCK_RESEARCH_URL", raising=False)
    monkeypatch.delenv("A_STOCK_RESEARCH_TOKEN", raising=False)
    assert (
        cache.main(["research", "start", "600519", "--question", "现金流变化？"]) == 2
    )
    assert cache.main(["research", "reports", "600519"]) == 1
    assert "A_STOCK_RESEARCH_URL" in capsys.readouterr().err
    assert not path.exists()


def test_compact_budget_and_recovery_are_get_only(monkeypatch, capsys):
    monkeypatch.setattr(research_client.paths, "research_connection", lambda: ("http://127.0.0.1:9000", "test-key"))
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"version": "research-v1", "status": "queued", "view": "compact"}).encode()
        return response

    monkeypatch.setattr(research_client.requests, "request", request)
    for args in (["report", "37", "--compact"], ["budget"], ["resume", "--request-id", "lost-response"]):
        assert research_client.cmd_research(args) == 0
        assert json.loads(capsys.readouterr().out)["version"] == "research-v1"
    assert [item[0] for item in calls] == ["GET", "GET", "GET"]
    assert calls[0][2]["params"] == {"compact": "true"}
    assert calls[2][2]["params"] == {"request_id": "lost-response"}
    assert all(item[2]["json"] is None for item in calls)


def test_submit_timeout_does_not_auto_retry_or_print_credentials(monkeypatch, capsys):
    monkeypatch.setattr(research_client.paths, "research_connection", lambda: ("http://127.0.0.1:9000", "test-secret"))
    calls = []

    def timeout(*args, **kwargs):
        calls.append(args)
        raise requests.ReadTimeout("test-secret should not be printed")

    monkeypatch.setattr(research_client.requests, "request", timeout)
    assert research_client.cmd_research(["start", "603606", "--question", "现金流", "--request-id", "original"]) == 1
    error = capsys.readouterr().err
    assert "resume --request-id original" in error
    assert "test-secret" not in error and len(calls) == 1


def test_old_server_cannot_silently_return_full_evidence_for_compact(monkeypatch, capsys):
    monkeypatch.setattr(research_client.paths, "research_connection", lambda: ("http://127.0.0.1:9000", "test-key"))
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps({"version": "research-v1", "report": {"summary": "old"}}).encode()
    monkeypatch.setattr(research_client.requests, "request", lambda *args, **kwargs: response)
    assert research_client.cmd_research(["report", "37", "--compact"]) == 1
    output = capsys.readouterr()
    assert not output.out and "尚未支持精简视图" in output.err


def test_lookup_not_found_keeps_public_diagnostic_and_redacts_key(monkeypatch, capsys):
    monkeypatch.setattr(research_client.paths, "research_connection", lambda: ("http://127.0.0.1:9000", "test-key"))
    response = requests.Response()
    response.status_code = 404
    response._content = json.dumps({"version": "research-v1", "error": "未找到不代表原提交失败；test-key\n不要换编号"}).encode()
    calls = []

    def request(*args, **kwargs):
        calls.append(args)
        return response

    monkeypatch.setattr(research_client.requests, "request", request)
    assert research_client.cmd_research(["resume", "--request-id", "original"]) == 1
    output = capsys.readouterr()
    assert not output.out and "未找到不代表原提交失败" in output.err
    assert "test-key" not in output.err and "[REDACTED]" in output.err
    assert "不要换编号" in output.err and len(calls) == 1


def test_missing_new_route_is_not_treated_as_a_missing_task(monkeypatch, capsys):
    monkeypatch.setattr(research_client.paths, "research_connection", lambda: ("http://127.0.0.1:9000", "test-key"))
    response = requests.Response()
    response.status_code = 404
    response._content = b'{"detail":"Not Found"}'
    monkeypatch.setattr(research_client.requests, "request", lambda *args, **kwargs: response)
    assert research_client.cmd_research(["resume", "--request-id", "original"]) == 1
    output = capsys.readouterr()
    assert not output.out and "尚未部署此读取接口" in output.err
    assert "勿换编号重复提交" in output.err


@pytest.mark.parametrize("status", [401, 404, 409, 503])
def test_http_denials_do_not_retry(monkeypatch, capsys, status):
    monkeypatch.setattr(research_client.paths, "research_connection", lambda: ("http://127.0.0.1:9000", "test-key"))
    calls = []

    def request(*args, **kwargs):
        calls.append(args)
        response = requests.Response()
        response.status_code = status
        return response

    monkeypatch.setattr(research_client.requests, "request", request)
    assert research_client.cmd_research(["resume", "--request-id", "original"]) == 1
    assert str(status) in capsys.readouterr().err
    assert len(calls) == 1


@pytest.mark.parametrize("args,path", [(["cards"], "/api/cards"), (["card", "600900"], "/api/companies/600900/card")])
def test_card_cli_read_only(monkeypatch, capsys, isolated_cache_database, args, path):
    fixture = Path(__file__).parent / "fixtures/monitor/research-cards.json"
    payload = json.loads(fixture.read_text())["response"]
    if args[0] == "card":
        payload = next(card for card in payload["cards"] if card["code"] == "600900")
    monkeypatch.setattr(research_client.paths, "research_connection", lambda: ("https://research.example", "test-key"))
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(payload).encode()
        return response

    monkeypatch.setattr(research_client.requests, "request", request)
    assert cache.main(["research", *args]) == 0
    assert json.loads(capsys.readouterr().out) == payload
    assert not isolated_cache_database.exists()
    assert len(calls) == 1 and calls[0][0:2] == ("GET", "https://research.example" + path)
    assert calls[0][2]["json"] is None
    assert calls[0][2]["timeout"] == (10, 30) and calls[0][2]["allow_redirects"] is False
    assert cache.main(["research", "card", "invalid"]) == 2
    assert len(calls) == 1
