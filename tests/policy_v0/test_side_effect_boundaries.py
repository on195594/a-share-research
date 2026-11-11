from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from a_share_research.policy.v0 import cache, commands_holdings, db, fetcher, paths


def test_isolation_path_propagates_to_child_process(tmp_path) -> None:
    database = paths.cache_db_path()
    assert database.parent == tmp_path
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from a_share_research.policy.v0.paths import cache_db_path; print(cache_db_path())",
        ],
        env={**os.environ},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == str(database)


def test_read_only_lookup_does_not_create_database(tmp_path, monkeypatch) -> None:
    database = tmp_path / "missing" / "cache.db"
    monkeypatch.setenv("POLICY_V0_DB", str(database))
    assert fetcher._lookup_cached_industry("600000") is None
    assert fetcher._lookup_cached_name("600000") is None
    assert not database.parent.exists()


def test_read_only_scope_resets_after_exception() -> None:
    with pytest.raises(RuntimeError):
        with db.read_only_scope():
            assert db.is_read_only()
            raise RuntimeError("fixture")
    assert not db.is_read_only()


@pytest.mark.parametrize("suffix", ["#fragment", "?query", "%23"])
def test_read_only_database_path_is_literal(tmp_path, monkeypatch, suffix) -> None:
    database = tmp_path / f"cache.db{suffix}"
    decoy = tmp_path / "cache.db"
    for path, value in ((database, "selected"), (decoy, "decoy")):
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE identity (value TEXT)")
        connection.execute("INSERT INTO identity VALUES (?)", (value,))
        connection.commit()
        connection.close()
    monkeypatch.setenv("POLICY_V0_DB", str(database))
    before = set(tmp_path.iterdir())
    with db.read_only_db_session() as connection:
        assert connection.execute("SELECT value FROM identity").fetchone() == (
            "selected",
        )
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TABLE forbidden (value TEXT)")
    assert set(tmp_path.iterdir()) == before
    database.unlink()
    with pytest.raises(sqlite3.OperationalError):
        with db.read_only_db_session():
            pytest.fail("missing database must not be created or replaced by the decoy")
    assert set(tmp_path.iterdir()) == before - {database}


def test_w1_refuses_public_parent_without_changing_it(tmp_path, monkeypatch, capsys):
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    monkeypatch.setenv("POLICY_V0_DB", str(parent / "cache.db"))
    assert cache.main(["--confirm-write", "add-holding", "600036", "40", "100"]) == 1
    assert "0700" in capsys.readouterr().err
    assert parent.stat().st_mode & 0o777 == 0o755
    assert list(parent.iterdir()) == []


def test_missing_w1_confirmation_keeps_fixture_hash(tmp_path, monkeypatch) -> None:
    database = tmp_path / "cache.db"
    monkeypatch.setenv("POLICY_V0_DB", str(database))
    before = (
        hashlib.sha256(database.read_bytes()).hexdigest() if database.exists() else None
    )
    assert cache.main(["set", "000001", "名称", "行业", "{}"]) == 3
    after = (
        hashlib.sha256(database.read_bytes()).hexdigest() if database.exists() else None
    )
    assert before == after


def test_r0_command_never_bootstraps_or_changes_database(tmp_path, monkeypatch) -> None:
    database = tmp_path / "cache.db"
    sqlite3.connect(database).close()
    monkeypatch.setenv("POLICY_V0_DB", str(database))
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    assert cache.main(["holdings"]) == 1

    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    with sqlite3.connect(database) as conn:
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            == []
        )


def test_populated_r0_commands_keep_fixture_hash(tmp_path, monkeypatch) -> None:
    database = tmp_path / "cache.db"
    monkeypatch.setenv("POLICY_V0_DB", str(database))
    commands_holdings.cmd_add_holding(["600036", "40", "100", "--notes", "fixture"])

    for command in (["holdings"], ["retro-pending"], ["retro-outliers"]):
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        assert cache.main(command) == 0
        assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_business_content_has_no_legacy_client_paths() -> None:
    root = Path(__file__).resolve().parents[2] / "a_share_research/policy/v0"
    for base in (root / "guide", root):
        for path in base.rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="ignore")
                assert ".claude/skills" not in text
                assert ".agents/skills" not in text
                assert ".hermes/skills" not in text


def test_tests_never_undo_the_autouse_database_isolation():
    # Undoing monkeypatch also reverts conftest's POLICY_V0_DB, so later CLI calls
    # in that test reach the real ~/.local/share database (happened 2026-10-22).
    root = Path(__file__).resolve().parent
    pattern = "monkeypatch" + ".undo("
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if pattern in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
