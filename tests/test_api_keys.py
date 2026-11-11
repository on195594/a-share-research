import hashlib
import json
import sys

import pytest

from a_share_research import api_keys, db


def test_key_cli_lists_only_metadata_and_revokes_by_stable_id(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DATA", tmp_path)
    monkeypatch.setattr(sys, "argv", ["api_keys", "create", "skills-runtime"])
    api_keys.main()
    token = capsys.readouterr().out.strip()
    conn = db.connect()
    row = conn.execute("SELECT * FROM api_keys").fetchone()
    assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in tuple(row)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    monkeypatch.setattr(sys, "argv", ["api_keys", "list"])
    api_keys.main()
    assert json.loads(capsys.readouterr().out) == [
        {"id": row["id"], "name": "skills-runtime", "created_at": row["created_at"], "role": "research"}
    ]
    monkeypatch.setattr(sys, "argv", ["api_keys", "revoke", str(row["id"])])
    api_keys.main()
    assert json.loads(capsys.readouterr().out) == {"id": row["id"], "revoked": True}
    assert conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] == 0
    api_keys.create(conn, "skills-runtime")
    assert conn.execute("SELECT id FROM api_keys").fetchone()[0] > row["id"]
    with pytest.raises(SystemExit) as exc:
        api_keys.main()
    assert exc.value.code == 2
    assert conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] == 1
    for name in ("", "  ", "x" * 81, "line\nbreak"):
        with pytest.raises(ValueError):
            api_keys.create(conn, name)
    conn.close()


def test_roles_migrate_existing_keys_and_cannot_be_changed():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE api_keys (id INTEGER PRIMARY KEY, name TEXT, token_hash TEXT UNIQUE, created_at TEXT)")
    conn.execute("INSERT INTO api_keys VALUES (7, 'legacy', ?, '2026-11-11')",
                 (hashlib.sha256(b'legacy-token').hexdigest(),))
    api_keys.migrate_roles(conn)
    api_keys.migrate_roles(conn)
    assert api_keys.authenticate(conn, 'legacy-token') == api_keys.Principal(7, 'research')
    assert api_keys.authenticate(conn, 'unknown') is None
    for role in api_keys.ROLES:
        token = api_keys.create(conn, role, role=role)
        principal = api_keys.authenticate(conn, token)
        assert principal.role == role
        with pytest.raises(sqlite3.IntegrityError, match='rotate key'):
            conn.execute("UPDATE api_keys SET role=? WHERE id=?",
                         ('owner' if role != 'owner' else 'research', principal.key_id))
        conn.execute('DELETE FROM api_keys WHERE id=?', (principal.key_id,))
        assert api_keys.authenticate(conn, token) is None
    with pytest.raises(ValueError, match='类型'):
        api_keys.create(conn, 'bad', role='admin')
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO api_keys (role) VALUES ('admin')")
