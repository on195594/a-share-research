import hashlib
import sqlite3
import time
from contextlib import closing

import pytest

from a_share_research import api_keys, db, oauth_store
from support.mcp import BINDING, REDIRECT
from support.mcp import OAUTH_RESOURCE as RESOURCE


@pytest.fixture
def grant(tmp_path):
    with closing(db.connect(tmp_path / "app.db")) as conn:
        oauth_store.register_client(conn, "browser", "Browser", [REDIRECT, "https://client.example/other"])
        oauth_store.register_client(conn, "other", "Other", [REDIRECT])
        grant_id = oauth_store.create_grant(conn, "browser")
        yield conn, grant_id


def test_code_bound_to_grant_client_redirect_resource_and_challenge(grant):
    conn, grant_id = grant
    assert oauth_store.get_client(conn, "browser") == dict(
        client_id="browser", name="Browser", redirect_uris=[REDIRECT, "https://client.example/other"])
    expiry = time.time() + 300
    for mismatch in ({"client_id": "other"}, {"redirect_uri": REDIRECT + "/unregistered"}):
        with pytest.raises(ValueError, match="授权无效或回调地址未登记"):
            oauth_store.save_code(conn, "code", grant_id=grant_id, expires_at=expiry, **(BINDING | mismatch))
    oauth_store.save_code(conn, "code", grant_id=grant_id, expires_at=expiry, **BINDING)
    assert conn.execute("SELECT code_hash FROM oauth_codes").fetchone()[0] == hashlib.sha256(b"code").hexdigest()
    for field in BINDING:
        assert oauth_store.consume_code(conn, "code", **(BINDING | {field: "mismatch"})) is None
    assert oauth_store.consume_code(conn, "code", **BINDING) == grant_id
    assert oauth_store.consume_code(conn, "code", **BINDING) is None
    oauth_store.save_code(conn, "expired", grant_id=grant_id, expires_at=0, **BINDING)
    assert oauth_store.consume_code(conn, "expired", **BINDING) is None


@pytest.mark.parametrize("revoke", ["client", "grant"])
def test_credentials_expire_and_revocation_covers_existing_and_new_credentials(grant, revoke):
    conn, grant_id = grant
    expiry = time.time() + 300
    assert conn.execute("SELECT token_hash FROM api_keys WHERE id=?", (grant_id,)).fetchone()[0] is None
    assert api_keys.is_active(conn, grant_id, "assistant")
    assert not api_keys.is_active(conn, grant_id, "owner")
    oauth_store.save_code(conn, "unused", grant_id=grant_id, expires_at=expiry, **BINDING)
    for kind in ("access", "refresh"):
        oauth_store.save_token(conn, kind, grant_id=grant_id, kind=kind, resource=RESOURCE, expires_at=expiry)
        assert oauth_store.token_grant(conn, kind, kind=kind, resource=RESOURCE) == grant_id
        assert api_keys.authenticate(conn, kind) is None
    assert set(row[0] for row in conn.execute("SELECT token_hash FROM oauth_tokens")) == {
        hashlib.sha256(value).hexdigest() for value in (b"access", b"refresh")}
    assert oauth_store.token_grant(conn, "refresh", kind="access", resource=RESOURCE) is None
    assert oauth_store.token_grant(conn, "access", kind="access", resource=RESOURCE + "/other") is None
    oauth_store.save_token(conn, "expired", grant_id=grant_id, kind="access", resource=RESOURCE, expires_at=0)
    assert oauth_store.token_grant(conn, "expired", kind="access", resource=RESOURCE) is None
    oauth_store.save_token(conn, "rotated", grant_id=grant_id, kind="refresh", resource=RESOURCE, expires_at=expiry)
    assert oauth_store.consume_refresh(conn, "rotated", client_id="browser", resource=RESOURCE) == grant_id
    assert oauth_store.consume_refresh(conn, "rotated", client_id="browser", resource=RESOURCE) is None
    assert oauth_store.token_grant(conn, "rotated", kind="refresh", resource=RESOURCE) is None
    if revoke == "client":
        assert oauth_store.revoke_client(conn, "browser")
        assert oauth_store.get_client(conn, "browser") is None
        with pytest.raises(ValueError, match="未登记或已撤销"):
            oauth_store.create_grant(conn, "browser")
    else:
        api_keys.revoke(conn, grant_id)
    assert oauth_store.token_grant(conn, "refresh", kind="refresh", resource=RESOURCE) is None
    assert not api_keys.is_active(conn, grant_id, "assistant")
    assert oauth_store.consume_code(conn, "unused", **BINDING) is None
    with pytest.raises(ValueError, match="授权无效"):
        oauth_store.save_token(conn, "new", grant_id=grant_id, kind="access", resource=RESOURCE, expires_at=expiry)


def test_schema_rejects_oauth_grants_outside_assistant_scope(grant):
    conn, _ = grant
    insert = "INSERT INTO api_keys(name,created_at,role,kind,client,token_hash) VALUES ('x','t',?,?,?,?)"
    for row in (('owner', 'oauth', 'browser', None), ('assistant', 'oauth', None, None),
                ('assistant', 'oauth', 'browser', 'hash'), ('assistant', 'pat', 'browser', 'hash')):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, row)


def test_oauth_grants_have_distinct_request_identities(grant):
    from a_share_research import service
    conn, grant_id = grant
    other = oauth_store.create_grant(conn, "browser")
    first, second = (service.requester(conn, api_keys.Principal(key_id, "assistant")) for key_id in (grant_id, other))
    assert first == f"grant:{grant_id}" and second == f"grant:{other}"
    token = api_keys.create(conn, "pat", role="assistant")
    pat = api_keys.authenticate(conn, token)
    assert service.requester(conn, pat).startswith("key:")
    conn.execute("UPDATE api_keys SET revoked_at='now' WHERE id=?", (grant_id,))
    with pytest.raises(service.RequestError, match="撤销"):
        service.requester(conn, api_keys.Principal(grant_id, "assistant"))


def test_evidence_uploads_are_checked_and_append_only(grant):
    conn, grant_id = grant
    sha = hashlib.sha256(b"x").hexdigest()
    insert = "INSERT INTO account_evidence_uploads(sha256,uploaded_at,key_id,filename,label) VALUES (?,?,?,?,?)"
    conn.execute(insert, (sha, "2026-11-20T10:00:00+08:00", grant_id, "a.jpg", "交割单"))
    for bad in ((sha.upper(), grant_id, "a.jpg", None), (sha, grant_id, "", None),
                (sha, 999, None, None), (sha, grant_id, None, "x" * 81)):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, (bad[0], "t", *bad[1:]))
    for statement in ("UPDATE account_evidence_uploads SET label='x'", "DELETE FROM account_evidence_uploads"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(statement)
