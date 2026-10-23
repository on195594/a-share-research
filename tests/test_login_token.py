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
        "web.conn.execute('UPDATE sessions SET expires_at = ?', (time.time() - 1,))\n"
        "assert web.current_session(request) is None\n",
        token,
    ], env=env, check=True, timeout=15)
