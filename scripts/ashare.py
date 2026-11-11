#!/usr/bin/env python3
"""Standard-library client for the local a-share tools service."""
import argparse
import ipaddress
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


class ClientError(ValueError):
    """Safe error message for the CLI."""

    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


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
    try:
        data = None if payload is None else json.dumps(payload, allow_nan=False).encode()
    except (ValueError, RecursionError):
        raise ClientError('工具参数包含无效数字或嵌套过深') from None
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
        result = json.loads(raw, parse_constant=reject_constant, parse_float=finite_float, object_pairs_hook=unique_object)
    except urllib.error.HTTPError as exc:
        # Never echo server bodies, URLs or request headers containing credentials.
        raise ClientError(f'服务拒绝请求（HTTP {exc.code}），请检查凭据、权限与参数', status=exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ClientError('无法连接工具服务或请求超时；未自动重试') from None
    except (ValueError, UnicodeError, RecursionError):
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


def finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('nonfinite JSON number')
    return number


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def rpc_error(request_id, code, message):
    return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}}


class MCPBridge:
    """Sequential stdio adapter; HTTP remains the sole permission/write boundary."""

    def __init__(self):
        self.initialized = False
        self.ready = False

    def handle(self, message):
        if (not isinstance(message, dict) or message.get('jsonrpc') != '2.0'
                or not isinstance(message.get('method'), str)
                or 'result' in message or 'error' in message
                or ('id' in message and type(message['id']) not in (int, str))):
            return rpc_error(None, -32600, 'Invalid Request')
        method, params = message['method'], message.get('params', {})
        # Notifications never execute a tool or receive a response.
        if 'id' not in message:
            if method == 'notifications/initialized' and self.initialized and isinstance(params, dict):
                self.ready = True
            return None
        request_id = message['id']
        if not isinstance(params, dict):
            return rpc_error(request_id, -32602, 'params must be an object')
        if method == 'ping':
            result = {}
        elif method == 'initialize':
            info = params.get('clientInfo')
            if self.initialized:
                return rpc_error(request_id, -32600, 'Already initialized')
            if (not isinstance(params.get('protocolVersion'), str)
                    or not isinstance(params.get('capabilities'), dict) or not isinstance(info, dict)
                    or not all(isinstance(info.get(k), str) for k in ('name', 'version'))):
                return rpc_error(request_id, -32602, 'Invalid initialize parameters')
            version = params['protocolVersion']
            self.initialized = True
            result = {'protocolVersion': version if version in ('2025-06-18', '2025-11-25') else '2025-11-25',
                      'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'ashare', 'version': '1'}}
        elif not self.ready:
            return rpc_error(request_id, -32600, 'Initialize before calling tools')
        elif method == 'tools/list':
            if set(params) - {'_meta'}:
                return rpc_error(request_id, -32602, 'This server returns the full list; omit cursor')
            try:
                result = {'tools': [self.describe(tool) for tool in request('/tools')['tools']]}
            except ClientError as exc:
                return rpc_error(request_id, -32603, str(exc))
        elif method == 'tools/call':
            name, arguments = params.get('name'), params.get('arguments', {})
            if (set(params) - {'name', 'arguments', '_meta'} or not isinstance(name, str)
                    or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,99}', name)
                    or not isinstance(arguments, dict)):
                return rpc_error(request_id, -32602, 'Invalid tool name or arguments')
            try:
                payload = request('/tools/' + name, arguments)['result']
                result = {'content': [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False, allow_nan=False)}],
                          'structuredContent': payload, 'isError': payload.get('exit_code', 0) != 0}
            except ClientError as exc:
                if exc.status == 404:
                    return rpc_error(request_id, -32602, str(exc))
                result = {'content': [{'type': 'text', 'text': str(exc)}], 'isError': True}
        else:
            return rpc_error(request_id, -32601, 'Method not found')
        return {'jsonrpc': '2.0', 'id': request_id, 'result': result}

    @staticmethod
    def describe(tool):
        if (not isinstance(tool, dict)
                or not all(isinstance(tool.get(k), str) for k in ('name', 'description'))
                or not all(isinstance(tool.get(k), dict) for k in ('input_schema', 'output_schema'))
                or tool.get('access') not in ('read', 'write') or type(tool.get('idempotent')) is not bool):
            raise ClientError('工具列表响应格式不兼容')
        return {'name': tool['name'], 'description': tool['description'],
                'inputSchema': tool['input_schema'], 'outputSchema': tool['output_schema'],
                'annotations': {'readOnlyHint': tool['access'] == 'read', 'idempotentHint': tool['idempotent']}}


def serve_mcp(input_stream=None, output_stream=None):
    """Read UTF-8 JSON lines until EOF. Never retry or roll back an admitted HTTP call."""
    input_stream = input_stream if input_stream is not None else sys.stdin.buffer
    output_stream = output_stream if output_stream is not None else sys.stdout.buffer
    bridge = MCPBridge()
    limit = 32 * 1024 * 1024  # Includes the largest evidence upload, plus its envelope.
    while raw := input_stream.readline(limit + 1):
        oversized = len(raw) > limit
        try:
            if oversized:
                raise ValueError('oversized message')
            message = json.loads(raw.decode('utf-8'), parse_constant=reject_constant,
                                 parse_float=finite_float, object_pairs_hook=unique_object)
        except (ValueError, UnicodeError, RecursionError):
            response = rpc_error(None, -32700, 'Invalid JSON or message too large')
        else:
            response = bridge.handle(message)
        if response is not None:
            output_stream.write((json.dumps(response, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8'))
            output_stream.flush()
        if oversized:
            return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    actions.add_parser('tools')
    actions.add_parser('mcp', help='MCP stdio bridge to the local HTTP tools service')
    call = actions.add_parser('call')
    call.add_argument('name')
    call.add_argument('--json', default='{}', dest='arguments')
    args = parser.parse_args(argv)
    try:
        if args.action == 'mcp':
            return serve_mcp()
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
