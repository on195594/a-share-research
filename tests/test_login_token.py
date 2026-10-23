import os
import subprocess
import sys


def test_minted_session_is_accepted_until_it_expires(tmp_path):
    env = os.environ | {"DATA_DIR": str(tmp_path), "DEV_LOGIN": "1", "PUBLIC_BASE_URL": "http://127.0.0.1:8617",
                        "SESSION_SECRET": "local-login-token-" * 3}
    token = subprocess.run([sys.executable, "-m", "a_share_research.login_token", "--minutes", "1"],
                           env=env, check=True, timeout=15, capture_output=True, text=True).stdout.strip()
    subprocess.run([
        sys.executable, "-c",
        "import sys, time\n"
        "from starlette.requests import Request\n"
        "from a_share_research import web\n"
        "request = Request({'type': 'http', 'headers': [(b'cookie', b'session=' + sys.argv[1].encode())]})\n"
        "assert web.current_session(request) == web._hash(sys.argv[1])\n"
        "from a_share_research.login_token import mint\n"
        "from fastapi import HTTPException\n"
        "agent = mint(web.conn, 1, agent=True)\n"
        "agent_cookie = Request({'type': 'http', 'headers': [(b'cookie', b'session=' + agent.encode())]})\n"
        "assert web.current_session(agent_cookie) is None\n"
        "def bearer(token): return Request({'type': 'http', 'headers': [(b'authorization', b'Bearer ' + token.encode())]})\n"
        "assert web.api_session(bearer(agent)) == 'agent:' + web._hash(agent)\n"
        "for denied in (request, bearer(sys.argv[1])):\n"
        "    try: web.api_session(denied)\n"
        "    except HTTPException as exc: assert exc.status_code == 401\n"
        "    else: raise AssertionError('browser sessions must not authorize API calls')\n"
        "web.api_revoke(bearer(agent))\n"
        "try: web.api_session(bearer(agent))\n"
        "except HTTPException as exc: assert exc.status_code == 401\n"
        "else: raise AssertionError('revoked token must fail')\n"
        "web.conn.execute('UPDATE sessions SET expires_at = ?', (time.time() - 1,))\n"
        "assert web.current_session(request) is None\n",
        token,
    ], env=env, check=True, timeout=15)
