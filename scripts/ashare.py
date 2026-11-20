#!/usr/bin/env python3
"""Standard-library CLI for the a-share MCP endpoint."""
import argparse
import http.client
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request

_TOOL_NAME = re.compile(r'[a-z][a-z0-9_]*')
_VERSION = '2026-07-28'
_VERSION_KEY = 'io.modelcontextprotocol/protocolVersion'
_CAPABILITIES_KEY = 'io.modelcontextprotocol/clientCapabilities'
_UNKNOWN = '结果未知，请先查询回执，只能用原身份和原请求恢复'
_INPUT_LIMIT = 28 * 1024**2  # MCP request body budget, including Base64 evidence.
# A policy receipt includes the capture (up to 8 MiB), its result and metadata.
# MCP sends both structured JSON and a second, escaped text representation.
_RESPONSE_LIMIT = 64 * 1024**2


class ClientError(Exception):
    """A local diagnostic safe to show to a caller."""

    def __init__(self, message, exit_code=1):
        super().__init__(message)
        self.exit_code = exit_code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ResponseTooLarge(ValueError):
    """The response exceeded the caller's byte budget."""


def reject_constant(value):
    raise ValueError('non-standard JSON constant')


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


def parse_json(raw):
    return json.loads(raw, parse_constant=reject_constant, parse_float=finite_float, object_pairs_hook=unique_object)


def configuration(filename):
    """Read a private profile without putting its PAT in the process environment."""
    try:
        path = Path(filename)
        if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError
            raw = stream.read(16 * 1024 + 1)
        if len(raw) > 16 * 1024:
            raise ValueError
        settings = parse_json(raw)
        if not isinstance(settings, dict) or not isinstance(settings.get('url'), str):
            raise ValueError
        url = settings['url']
        if url.rstrip('/') in ('http://127.0.0.1:8621', 'http://127.0.0.1:8621/tools'):
            raise ClientError('参数错误：请升级旧 /tools 配置，url 改为公网 HTTPS /mcp 地址；本机直连请另填 connect')
        if set(settings) not in ({'url', 'token'}, {'url', 'token', 'connect'}):
            raise ValueError
        if (not re.fullmatch(r'https://(?:[A-Za-z0-9.-]+|\[[a-fA-F0-9:.]+\])(?::[0-9]+)?/mcp', url)
                or urllib.parse.urlsplit(url).port == 0):
            raise ValueError
        if 'connect' in settings:
            connect = settings['connect']
            address = isinstance(connect, str) and re.fullmatch(r'http://(?:127\.0\.0\.1|\[::1\]):([0-9]{1,5})', connect)
            if not address or not 1 <= int(address[1]) <= 65535:
                raise ClientError('参数错误：connect 仅支持 http://127.0.0.1:端口 或 http://[::1]:端口')
        token = settings['token']
        if not isinstance(token, str) or not re.fullmatch(r'[!-~]+', token):
            raise ClientError('凭据失效：请检查私有配置中的 PAT')
    except (OSError, ValueError, RecursionError):
        raise ClientError('参数错误：请检查私有配置的绝对路径、文件权限及 HTTPS /mcp 地址') from None
    return settings


def read_json_response(response, limit):
    size = response.headers.get('Content-Length')
    if size is not None:
        size = int(size)
        if size > limit:
            raise ResponseTooLarge
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ResponseTooLarge
    if size is not None and size != len(raw):
        raise ValueError('incomplete response')
    return parse_json(raw.decode('utf-8'))


def argument_error(detail, token):
    # Only project a validation field path, never free-form server diagnostics.
    message = detail.get('message')
    field = isinstance(message, str) and re.match(r'([a-zA-Z_][a-zA-Z0-9_.]{0,159}): ', message)
    if field and token not in field[1]:
        return ClientError(f'参数错误：请按工具定义核对字段 {field[1]}')
    return ClientError('参数错误：请用 tools 核对工具 name 和 arguments 字段')


def http_error(error, token):
    with error:
        if error.code in (401, 403):
            return ClientError(f'凭据失效或权限不足（HTTP {error.code}），请核对授权')
        if error.code == 429:
            return ClientError('发布暂停或服务限流，稍后用原请求重试', 21)
        if error.code in (400, 404, 503):
            try:
                body = read_json_response(error, limit=4096)
            except (OSError, http.client.HTTPException, ValueError, RecursionError):
                body = None
            detail = body.get('error') if isinstance(body, dict) else None
            if (error.code in (400, 404) and isinstance(detail, dict) and body.get('jsonrpc') == '2.0'
                    and detail.get('code') in (-32601, -32602)):
                return argument_error(detail, token)
            if error.code == 503 and isinstance(detail, dict):
                if detail.get('code') == 'auth_unavailable':
                    return ClientError('发布暂停或暂未受理：认证服务不可用，请稍后用原请求重试', 21)
                if detail.get('code') == -32603 and detail.get('data') == {'code': 'paused'}:
                    return ClientError('发布暂停，请稍后用原请求重试', 21)
        if error.code == 408 or error.code >= 500:
            return ClientError(_UNKNOWN, 22)
        return ClientError(f'参数错误：服务拒绝请求（HTTP {error.code}），请核对地址、协议及参数')


def request(settings, method, params):
    """One POST, without a proxy, redirect, session, or automatic replay."""
    params = params | {'_meta': {_VERSION_KEY: _VERSION, _CAPABILITIES_KEY: {}}}
    message = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}
    url = settings['url']
    headers = {'Authorization': 'Bearer ' + settings['token'], 'Host': urllib.parse.urlsplit(url).netloc,
               'Accept': 'application/json', 'Content-Type': 'application/json',
               # Cloudflare refuses the default Python-urllib agent at the edge (HTTP 403).
               'User-Agent': 'ashare-cli/' + _VERSION}
    if 'connect' in settings:
        url = settings['connect'] + '/mcp'
    headers['MCP-Protocol-Version'] = _VERSION
    headers['Mcp-Method'] = method
    if method == 'tools/call':
        headers['Mcp-Name'] = params['name']

    try:
        data = json.dumps(message, allow_nan=False).encode()
    except (ValueError, RecursionError):
        raise ClientError('参数错误：无效 JSON 或嵌套过深') from None
    req = urllib.request.Request(url, data=data, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        try:
            response = opener.open(req, timeout=30)
        except urllib.error.HTTPError as exc:
            raise http_error(exc, settings['token']) from None
        with response:
            reply = read_json_response(response, _RESPONSE_LIMIT)
        if (not isinstance(reply, dict) or reply.get('jsonrpc') != '2.0'
                or type(reply.get('id')) is not int or reply['id'] != 1
                or ('result' in reply) == ('error' in reply)
                or not isinstance(reply.get('error', reply.get('result')), dict)):
            raise ValueError('invalid JSON-RPC response')
    except ResponseTooLarge:
        raise ClientError('回执超限，请用支持更大回执的 MCP 客户端，沿用原身份和原请求查回', 23) from None
    except (OSError, http.client.HTTPException, ValueError, RecursionError) as exc:
        if isinstance(exc, ConnectionRefusedError) or isinstance(getattr(exc, 'reason', None), ConnectionRefusedError):
            raise ClientError('发布暂停或暂未受理：连接被拒，请稍后用原请求重试', 21) from None
        raise ClientError(_UNKNOWN, 22) from None
    return reply


def tool_error(code, name):
    if code == 'policy_activation_changed':
        return ClientError('政策异常：生效政策已变化，原请求确定未写入', 20)
    if code == 'paused':
        return ClientError('发布暂停，请稍后用原请求重试', 21)
    if code == 'busy':
        return ClientError('发布暂停或暂未受理：名额已满，请稍后用原请求重试', 21)
    if code in ('unauthorized', 'forbidden'):
        return ClientError('凭据失效或权限不足，请核对授权')
    if code in ('conflict', 'not_found') and name.startswith('policy_'):
        return ClientError('政策异常，请核对当前政策、请求编号与评估状态')
    if code in ('conflict', 'not_found'):
        return ClientError('参数错误，请核对工具定义、请求编号与当前记录')
    return ClientError(_UNKNOWN, 22)


def cli_result(settings, method, params):
    reply = request(settings, method, params)
    if 'error' in reply:
        error = reply['error']
        if error.get('code') == -32603 and error.get('data') == {'code': 'paused'}:
            raise tool_error('paused', '')
        raise ClientError(_UNKNOWN, 22)
    result = reply['result']
    try:
        if result.get('resultType', 'complete') != 'complete':
            raise ValueError
        if method == 'tools/list':
            if not isinstance(result.get('tools'), list):
                raise ValueError
            tools = []
            for tool in result['tools']:
                annotations = tool['annotations']
                extra = tool['_meta']['win.keyi.stock/registry']
                tools.append(dict(name=tool['name'], description=tool['description'], input_schema=tool['inputSchema'],
                                  output_schema=tool['outputSchema'], access='read' if annotations['readOnlyHint'] else 'write',
                                  idempotent=annotations['idempotentHint'], version=extra['version'],
                                  collects=extra['collects'], errors=extra['errors']))
            return {'tools': tools}
        if result.get('isError') is True:
            block, = result['content']
            if block['type'] != 'text' or result.get('structuredContent') is not None:
                raise ValueError
            detail = parse_json(block['text'])['error']
            if detail['code'] == 'invalid_arguments':
                raise argument_error(detail, settings['token'])
            raise tool_error(detail['code'], params['name'])
        if result.get('isError', False) is not False or not isinstance(result.get('structuredContent'), dict):
            raise ValueError
        return {'ok': True, 'result': result['structuredContent']}
    except (KeyError, TypeError, ValueError, RecursionError):
        raise ClientError(_UNKNOWN, 22) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(Path.home() / '.config/ashare/client.json'), help='私有 JSON 配置的绝对路径')
    actions = parser.add_subparsers(dest='action', required=True)
    actions.add_parser('tools')
    call = actions.add_parser('call')
    call.add_argument('name')
    call.add_argument('--json', default='{}', dest='arguments', help='工具参数 JSON 对象，- 表示从标准输入读取')
    args = parser.parse_args(argv)
    try:
        settings = configuration(args.config)
        if args.action == 'tools':
            result = cli_result(settings, 'tools/list', {})
        else:
            if not _TOOL_NAME.fullmatch(args.name):
                raise ClientError('参数错误：工具名称无效')
            try:
                raw = args.arguments
                if raw == '-':
                    raw = sys.stdin.buffer.read(_INPUT_LIMIT + 1)
                    if len(raw) > _INPUT_LIMIT:
                        raise ClientError('参数错误：标准输入超过 28 MiB，请缩小 JSON')
                payload = parse_json(raw)
                if not isinstance(payload, dict):
                    raise ValueError
            except OSError:
                raise ClientError('参数错误：标准输入读取失败，请核对输入文件或管道') from None
            except (ValueError, RecursionError):
                raise ClientError('参数错误：--json 须为有效 JSON 对象') from None
            result = cli_result(settings, 'tools/call', {'name': args.name, 'arguments': payload})
        sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + '\n')
        return 0
    except ClientError as exc:
        sys.stderr.write(str(exc) + '\n')
        return exc.exit_code


if __name__ == '__main__':
    sys.exit(main())
