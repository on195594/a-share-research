#!/usr/bin/env python3
"""Standard-library client for the local a-share tools service."""
import argparse
import ipaddress
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


class ClientError(ValueError):
    """Safe error message for the CLI."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def base_url(value):
    try:
        url = urllib.parse.urlsplit(value)
        address = ipaddress.ip_address(url.hostname or '')
        port = url.port
        if (url.scheme != 'http' or not address.is_loopback or url.username is not None
                or url.password is not None or url.path not in ('', '/') or url.query or url.fragment
                or port == 0):
            raise ValueError
    except ValueError:
        raise ClientError('ASHARE_URL 须为无路径、无凭据的本机回环 HTTP 地址') from None
    return value.rstrip('/')


def request(path, payload=None):
    url = base_url(os.environ.get('ASHARE_URL', 'http://127.0.0.1:8621'))
    token = os.environ.get('ASHARE_TOKEN', '')
    if not token or not token.isascii() or any(c.isspace() or ord(c) < 32 for c in token):
        raise ClientError('请设置有效的 ASHARE_TOKEN')
    data = None if payload is None else json.dumps(payload, allow_nan=False).encode()
    req = urllib.request.Request(url + path, data=data, headers={
        'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
    })
    # Do not send local credentials through an inherited HTTP proxy or a redirect.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=30) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ClientError('服务响应过大')
        result = json.loads(raw)
    except urllib.error.HTTPError as exc:
        # Never echo server bodies, URLs or request headers containing credentials.
        raise ClientError(f'服务拒绝请求（HTTP {exc.code}），请检查凭据、权限与参数') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ClientError('无法连接工具服务或请求超时；未自动重试') from None
    except (ValueError, UnicodeError):
        raise ClientError('服务返回无效 JSON') from None
    if not isinstance(result, dict):
        raise ClientError('服务响应须为 JSON 对象')
    if path == '/tools':
        if not isinstance(result.get('tools'), list):
            raise ClientError('工具列表响应格式不兼容')
    elif result.get('ok') is not True or not isinstance(result.get('result'), dict):
        raise ClientError('工具调用失败或响应格式不兼容')
    return result


def reject_constant(value):
    raise ValueError("non-standard JSON constant")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    actions.add_parser('tools')
    call = actions.add_parser('call')
    call.add_argument('name')
    call.add_argument('--json', default='{}', dest='arguments')
    args = parser.parse_args(argv)
    try:
        if args.action == 'tools':
            result = request('/tools')
        else:
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,99}', args.name):
                raise ClientError('工具名称格式无效')
            try:
                payload = json.loads(args.arguments, parse_constant=reject_constant)
                if not isinstance(payload, dict):
                    raise ValueError
            except ValueError:
                raise ClientError('--json 须为有效 JSON 对象') from None
            result = request('/tools/' + args.name, payload)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except ClientError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
