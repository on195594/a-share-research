"""Thread-local web connections against SQLite and captured production records."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from a_share_research import db
from support.continuation import RECORDS


def test_internal_tools_settings_accept_a_trimmed_pair_or_two_empty_values(web, monkeypatch):
    monkeypatch.setenv("ASHARE_TOOLS_URL", "  http://a-share-research-tools:8621/  ")
    monkeypatch.setenv("ASHARE_TOOLS_TOKEN", "  web-test-token  ")
    settings = web.Settings.from_env()
    assert (settings.tools_url, settings.tools_token) == ("http://a-share-research-tools:8621", "web-test-token")
    monkeypatch.setenv("ASHARE_TOOLS_URL", " ")
    monkeypatch.setenv("ASHARE_TOOLS_TOKEN", " ")
    settings = web.Settings.from_env()
    assert (settings.tools_url, settings.tools_token) == ("", "")


@pytest.mark.parametrize(("url", "token"), [
    ("https://tools", " "),
    ("https://user:secret@tools", "web-test-token"),
])
def test_internal_tools_settings_reject_an_incomplete_pair_or_invalid_address(web, monkeypatch, url, token):
    monkeypatch.setenv("ASHARE_TOOLS_URL", url)
    monkeypatch.setenv("ASHARE_TOOLS_TOKEN", token)
    with pytest.raises(SystemExit, match="ASHARE_TOOLS_URL"):
        web.Settings.from_env()


def test_other_thread_cannot_read_commit_rollback_or_close_an_open_transaction(web):
    conn = web.conn
    before = list(conn.iterdump())
    report_id = RECORDS[0]["report"]["id"]
    ready = Barrier(2, timeout=10)
    written = Barrier(2, timeout=10)
    observed = Barrier(2, timeout=10)

    def writer():
        try:
            conn.execute("SELECT 1")  # Open both connections before taking the write lock.
            ready.wait()
            with pytest.raises(RuntimeError, match="rollback isolated write"):
                with db.transaction(conn):
                    conn.execute("DELETE FROM reports WHERE id=?", (report_id,))
                    written.wait()
                    observed.wait()
                    assert conn.in_transaction
                    assert conn.execute("SELECT id FROM reports WHERE id=?", (report_id,)).fetchone() is None
                    raise RuntimeError("rollback isolated write")
        finally:
            conn.close()

    def reader():
        try:
            conn.execute("SELECT 1")
            ready.wait()
            written.wait()
            assert not conn.in_transaction
            assert conn.execute("SELECT id FROM reports WHERE id=?", (report_id,)).fetchone()[0] == report_id
            conn.commit()
            conn.rollback()
        finally:
            conn.close()
        observed.wait()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(writer), pool.submit(reader)]
        for future in futures:
            future.result(timeout=15)
    assert list(conn.iterdump()) == before
