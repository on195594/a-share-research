"""Browser request builders for local web tests."""

from starlette.requests import Request

from a_share_research.login_token import mint


def page_request(path, *, cookie="", accept="text/html"):
    headers = [(b"cookie", cookie.encode()), (b"accept", accept.encode())]
    return Request({"type": "http", "method": "GET", "path": path, "headers": headers,
                    "query_string": b"", "scheme": "http", "server": ("localhost", 80)})


def browser_request(web):
    token = mint(web.conn, 1)
    return Request({'type': 'http', 'method': 'POST', 'scheme': 'http', 'path': '/', 'query_string': b'',
                    'headers': [(b'cookie', f'session={token}'.encode())], 'server': ('127.0.0.1', 8627)}), web.csrf_token(web._hash(token))
