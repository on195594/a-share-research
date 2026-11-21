"""Owner consent and public-client OAuth over the in-process web app."""

import html
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from a_share_research import api_keys, oauth_store
from support.mcp import BINDING, REDIRECT

# RFC 7636 Appendix B verifier for the existing store fixture's S256 challenge.
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
AUTHORIZE = "/oauth/authorize"
TOKEN = "/oauth/token"
CALLBACK = REDIRECT + "?flow=mcp"
CLIENT_ORIGIN = "https://client.example"


@pytest.fixture
def browser(web):
    base = web.settings.base_url
    with TestClient(web.app, base_url=base, headers={"Origin": base}, follow_redirects=False) as client:
        yield client


@pytest.fixture
def params(web):
    oauth_store.register_client(web.conn, "browser", "Browser", [CALLBACK, REDIRECT])
    oauth_store.register_client(web.conn, "other", "Other", ["https://other.example/callback"])
    return {**BINDING, "redirect_uri": CALLBACK, "resource": web.settings.base_url + "/mcp",
            "code_challenge_method": "S256", "response_type": "code", "state": "outer+a/b&c=1"}


def hidden(response, name):
    assert response.status_code == 200, response.text
    match = re.search(rf'name="{name}" value="([^"]+)"', response.text)
    assert match is not None, name
    return html.unescape(match[1])


def decide(browser, page, decision):
    return browser.post(AUTHORIZE, data={
        "decision": decision, "csrf": hidden(page, "csrf"), "consent_id": hidden(page, "consent_id"),
    })


def callback_query(response, params):
    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    expected = urlsplit(params["redirect_uri"])
    assert (location.scheme, location.netloc, location.path) == (expected.scheme, expected.netloc, expected.path)
    query = parse_qs(location.query)
    assert query["state"] == [params["state"]]
    for field, values in parse_qs(expected.query).items():
        assert query[field] == values
    return query


def code_request(browser, params):
    consent = browser.get(AUTHORIZE, params=params)
    code, = callback_query(decide(browser, consent, "approve"), params)["code"]
    return {"grant_type": "authorization_code", "code": code, "client_id": params["client_id"],
            "redirect_uri": params["redirect_uri"], "code_verifier": VERIFIER, "resource": params["resource"]}


def token_request(browser, data, *, error=None):
    response = browser.post(TOKEN, data=data, headers={"Origin": CLIENT_ORIGIN})
    assert response.status_code == (400 if error else 200), response.text
    assert "no-store" in response.headers["cache-control"]
    body = response.json()
    if error:
        assert body["error"] == error
        assert "access_token" not in body and "refresh_token" not in body
    else:
        assert body["token_type"].lower() == "bearer" and body["expires_in"] > 0
    return body


def test_discovery_and_token_endpoint_do_not_restore_a_device(web, browser):
    assert browser.post("/auth/dev").status_code == 303
    csrf = hidden(browser.get("/connections"), "csrf")
    assert browser.post("/auth/remember-device", data={"csrf": csrf}).status_code == 303
    web.conn.execute("UPDATE sessions SET expires_at=0")

    base = web.settings.base_url
    protected = browser.get("/.well-known/oauth-protected-resource/mcp").json()
    assert protected["resource"] == base + "/mcp" and protected["authorization_servers"] == [base]
    issuer = browser.get("/.well-known/oauth-authorization-server").json()
    assert "scopes_supported" not in issuer and "scopes_supported" not in protected
    assert issuer["issuer"] == base
    assert issuer["authorization_endpoint"] == base + AUTHORIZE and issuer["token_endpoint"] == base + TOKEN
    assert issuer["code_challenge_methods_supported"] == ["S256"]
    assert issuer["token_endpoint_auth_methods_supported"] == ["none"]
    assert set(issuer["grant_types_supported"]) == {"authorization_code", "refresh_token"}
    assert issuer["response_types_supported"] == ["code"]
    cross_origin = {"Origin": CLIENT_ORIGIN}
    assert browser.post("/.well-known/oauth-authorization-server", headers=cross_origin).status_code == 405
    assert browser.post("/auth/dev", headers=cross_origin).status_code == 403
    token_request(browser, {"client_id": "browser", "grant_type": "unavailable"}, error="unsupported_grant_type")
    assert web.conn.execute("SELECT 1 FROM sessions WHERE expires_at>0").fetchone() is None
    assert browser.get("/connections").status_code == 200
    assert web.conn.execute("SELECT 1 FROM sessions WHERE expires_at>0").fetchone()


def test_login_resumes_consent_and_keeps_states_and_callback_csp_separate(web, browser, params):
    first = browser.get(AUTHORIZE, params=params)
    assert first.status_code == 303 and urlsplit(first.headers["location"]).path == "/login"
    login = browser.get(first.headers["location"])
    resume = hidden(login, "resume")
    github_link = re.search(r'href="(/auth/github[^"]*)"', login.text)[1]
    github = browser.get(html.unescape(github_link))
    github_state, = parse_qs(urlsplit(github.headers["location"]).query)["state"]
    assert github_state == resume and github_state != params["state"]
    assert browser.get("/oauth_callback", params={"state": params["state"], "code": "unused"}).status_code == 400

    # The offline login fixture completes the same session transition as GitHub.
    resumed = browser.post("/auth/dev", data={"resume": resume})
    assert resumed.status_code == 303
    assert web.PENDING_COOKIE not in browser.cookies
    consent = browser.get(resumed.headers["location"])
    assert all(text in consent.text for text in ("Browser", "可读账户", "追加账本", "发起付费研究"))
    csp = consent.headers["content-security-policy"]
    form_action = next(rule.strip() for rule in csp.split(";") if rule.strip().startswith("form-action "))
    assert CLIENT_ORIGIN in form_action.split() and "https://other.example" not in form_action.split()
    assert CLIENT_ORIGIN not in login.headers["content-security-policy"]
    assert CLIENT_ORIGIN not in browser.get("/connections").headers["content-security-policy"]

    data = {"decision": "approve", "consent_id": hidden(consent, "consent_id")}
    assert browser.post(AUTHORIZE, data=data).status_code == 403
    assert browser.post(AUTHORIZE, data={**data, "csrf": hidden(consent, "csrf")},
                        headers={"Origin": CLIENT_ORIGIN}).status_code == 403
    assert browser.post(AUTHORIZE, data={**data, "csrf": hidden(consent, "csrf"),
                                         "consent_id": data["consent_id"] + "x"}).status_code == 400
    denied = callback_query(decide(browser, consent, "deny"), params)
    assert denied["error"] == ["access_denied"] and "code" not in denied
    assert web.conn.execute("SELECT 1 FROM oauth_codes").fetchone() is None


def test_home_login_ignores_abandoned_consent(web, browser, params):
    first = browser.get(AUTHORIZE, params=params)
    abandoned = hidden(browser.get(first.headers["location"]), "resume")
    home_login = browser.get(browser.get("/").headers["location"])
    assert 'href="/auth/github"' in home_login.text and 'name="resume"' not in home_login.text
    github = browser.get("/auth/github")
    state, = parse_qs(urlsplit(github.headers["location"]).query)["state"]
    assert state != abandoned
    assert web.PENDING_COOKIE in browser.cookies
    assert browser.post("/auth/dev").headers["location"] == "/"
    assert web.PENDING_COOKIE not in browser.cookies


@pytest.mark.parametrize("change", ["revoke_client", "remove_callback"])
def test_login_discards_invalidated_authorization(web, browser, params, change):
    first = browser.get(AUTHORIZE, params=params)
    resume = hidden(browser.get(first.headers["location"]), "resume")
    if change == "revoke_client":
        oauth_store.revoke_client(web.conn, params["client_id"])
    else:
        web.conn.execute("UPDATE oauth_clients SET redirect_uris='[]' WHERE client_id=?", (params["client_id"],))
    assert browser.post("/auth/dev", data={"resume": resume}).headers["location"] == "/"
    assert web.PENDING_COOKIE not in browser.cookies


def test_logout_clears_pending_authorization_and_login_state(web, browser, params):
    pending = browser.get(AUTHORIZE, params=params)
    browser.post("/auth/dev")
    csrf = hidden(browser.get("/connections"), "csrf")
    # Cookies from an unfinished login can outlive the tab that started it.
    browser.cookies.update(pending.cookies)
    browser.get("/auth/github")
    assert web.PENDING_COOKIE in browser.cookies and web.STATE_COOKIE in browser.cookies
    assert browser.post("/logout", data={"csrf": csrf}).status_code == 303
    assert web.PENDING_COOKIE not in browser.cookies and web.STATE_COOKIE not in browser.cookies


def test_authorization_validates_client_callback_and_request_before_consent(browser, params):
    assert browser.post("/auth/dev").status_code == 303
    for change in ({"client_id": "unregistered"}, {"redirect_uri": CALLBACK + "2"}):
        response = browser.get(AUTHORIZE, params=params | change)
        assert response.status_code == 400 and "location" not in response.headers
        assert "text/html" in response.headers["content-type"]
    for change in ({"response_type": "token"}, {"code_challenge_method": "plain"},
                   {"code_challenge": ""}, {"resource": params["resource"] + "/other"}):
        response = browser.get(AUTHORIZE, params=params | change)
        if response.is_redirect:
            query = callback_query(response, params)
            assert "error" in query and "code" not in query
        else:
            assert response.status_code == 400


def test_pkce_exchange_binds_inputs_and_refresh_rotates_with_same_grant(web, browser, params):
    assert browser.post("/auth/dev").status_code == 303
    scope = "mcp offline_access"
    exchange = code_request(browser, params | {"scope": scope})
    browser.cookies.clear()
    for mismatch in ({"code_verifier": VERIFIER[::-1]}, {"redirect_uri": REDIRECT}, {"client_id": "other"}):
        token_request(browser, exchange | mismatch, error="invalid_grant")
    wrong_resource = browser.post(TOKEN, data=exchange | {"resource": params["resource"] + "/other"})
    assert wrong_resource.status_code == 400
    assert wrong_resource.json()["error"] in ("invalid_target", "invalid_grant")
    tokens = token_request(browser, exchange | {"scope": scope, "client_secret": "", "client_assertion": ""})
    assert "scope" not in tokens
    token_request(browser, exchange, error="invalid_grant")

    resource = params["resource"]
    grant_id = oauth_store.token_grant(web.conn, tokens["access_token"], kind="access", resource=resource)
    assert grant_id is not None
    refresh = {"grant_type": "refresh_token", "client_id": "browser", "refresh_token": tokens["refresh_token"],
               "resource": resource, "scope": scope}
    token_request(browser, refresh | {"client_id": "other"}, error="invalid_grant")
    token_request(browser, refresh | {"resource": resource + "/other"}, error="invalid_target")
    implicit_resource = {key: value for key, value in refresh.items() if key != "resource"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = [pool.submit(browser.post, TOKEN, data=implicit_resource) for _ in range(2)]
        accepted, rejected = sorted((r.result(timeout=10) for r in requests), key=lambda r: r.status_code)
    assert accepted.status_code == 200
    assert rejected.status_code == 400 and rejected.json()["error"] == "invalid_grant"
    rotated = accepted.json()
    assert "scope" not in rotated
    assert rotated["refresh_token"] != tokens["refresh_token"] and rotated["access_token"] != tokens["access_token"]
    for kind in ("access", "refresh"):
        assert oauth_store.token_grant(web.conn, rotated[kind + "_token"], kind=kind, resource=resource) == grant_id
    token_request(browser, refresh, error="invalid_grant")


def test_public_token_endpoint_rejects_client_authentication(browser):
    for header in ("Basic dGVzdA==", " malformed", ""):
        response = browser.post(TOKEN, headers={"Authorization": header})
        assert response.status_code == 400 and response.json()["error"] == "invalid_client"
        assert "no-store" in response.headers["cache-control"]
        assert "www-authenticate" not in response.headers
    for field in ("client_secret", "client_assertion"):
        token_request(browser, {field: "supplied"}, error="invalid_client")


def test_retry_of_one_consent_reuses_only_its_code_and_grant(web, browser, params):
    browser.post("/auth/dev")
    consent = browser.get(AUTHORIZE, params=params)
    other = params | {"client_id": "other", "redirect_uri": "https://other.example/callback", "state": "second-tab"}
    other_consent = browser.get(AUTHORIZE, params=other)
    code = callback_query(decide(browser, consent, "approve"), params)["code"]
    assert callback_query(decide(browser, consent, "approve"), params)["code"] == code
    assert callback_query(decide(browser, other_consent, "approve"), other)["code"] != code
    assert web.conn.execute("SELECT COUNT(*) FROM oauth_codes").fetchone()[0] == 2
    assert web.conn.execute("SELECT COUNT(*) FROM api_keys WHERE kind='oauth'").fetchone()[0] == 2


@pytest.mark.parametrize("revoke", ["client", "grant"])
def test_owner_registration_and_revocation_stop_existing_credentials(web, browser, params, revoke):
    assert browser.get("/connections").headers["location"] == "/login"
    assert browser.post("/auth/dev").status_code == 303
    page = browser.get("/connections")
    csrf = hidden(page, "csrf")
    assert params["resource"] in page.text
    registration = {"name": "Registered", "redirect_uris": CALLBACK + "\n" + REDIRECT}
    assert browser.post("/connections/clients", data=registration).status_code == 403
    registered = browser.post("/connections/clients", data=registration | {"csrf": csrf})
    assert registered.status_code == 303
    client_id, = web.conn.execute("SELECT client_id FROM oauth_clients WHERE name='Registered'").fetchone()
    manager = browser.get("/connections")
    assert all(value in manager.text for value in (client_id, CALLBACK, REDIRECT))
    params = params | {"client_id": client_id}
    tokens = token_request(browser, code_request(browser, params))
    pending = code_request(browser, params)
    resource = params["resource"]
    grant_id = oauth_store.token_grant(web.conn, tokens["access_token"], kind="access", resource=resource)
    grant_ids = [row[0] for row in web.conn.execute("SELECT id FROM api_keys WHERE client=?", (client_id,))]
    assert grant_id in grant_ids
    if revoke == "client":
        action = "/connections/clients/revoke"
        assert browser.post(action, data={"client_id": client_id}).status_code == 403
        assert browser.post(action, data={"client_id": client_id, "csrf": csrf}).status_code == 303
        assert client_id not in browser.get("/connections").text
        response = browser.get(AUTHORIZE, params=params)
        assert response.status_code == 400 and "location" not in response.headers
    else:
        for key_id in grant_ids:
            action = f"/connections/grants/{key_id}/revoke"
            assert action in browser.get("/connections").text
            assert browser.post(action).status_code == 403
            assert browser.post(action, data={"csrf": csrf}).status_code == 303
            assert action not in browser.get("/connections").text
        assert web.conn.execute("SELECT revoked_at FROM api_keys WHERE id=?", (grant_id,)).fetchone()[0]
    assert oauth_store.token_grant(web.conn, tokens["access_token"], kind="access", resource=resource) is None
    token_request(browser, pending, error="invalid_grant")
    token_request(browser, {"grant_type": "refresh_token", "client_id": client_id, "resource": resource,
                            "refresh_token": tokens["refresh_token"]}, error="invalid_grant")


def test_management_revokes_client_with_slash_in_id(web, browser):
    client_id = "browser/desktop"
    oauth_store.register_client(web.conn, client_id, "Browser", [CALLBACK])
    browser.post("/auth/dev")
    page = browser.get("/connections")
    action = "/connections/clients/revoke"
    assert f'action="{action}"' in page.text
    fields = {name: hidden(page, name) for name in ("client_id", "csrf")}
    assert fields["client_id"] == client_id
    assert browser.post(action, data=fields).status_code == 303
    assert oauth_store.get_client(web.conn, client_id) is None


def test_management_only_revokes_mcp_grants_and_checks_registration(web, browser):
    mcp = api_keys.create(web.conn, "MCP assistant", role="assistant")
    owner = api_keys.create(web.conn, "Local owner", role="owner")
    browser.post("/auth/dev")
    page = browser.get("/connections")
    csrf = hidden(page, "csrf")
    assert "MCP assistant" in page.text and "Local owner" not in page.text and mcp not in page.text
    url = "/connections/grants/{}/revoke"
    browser.post(url.format(api_keys.authenticate(web.conn, owner).key_id), data={"csrf": csrf})
    assert api_keys.authenticate(web.conn, owner) is not None
    action = url.format(api_keys.authenticate(web.conn, mcp).key_id)
    assert browser.post(action).status_code == 403
    assert browser.post(action, data={"csrf": csrf}).status_code == 303
    assert api_keys.authenticate(web.conn, mcp) is None
    invalid = browser.post("/connections/clients", data={"name": "Browser", "redirect_uris": "https://*.example/cb",
                                                         "csrf": csrf})
    assert invalid.status_code == 400 and "通配" in invalid.text
    assert web.conn.execute("SELECT 1 FROM oauth_clients").fetchone() is None
