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
        {"id": row["id"], "name": "skills-runtime", "created_at": row["created_at"]}
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
