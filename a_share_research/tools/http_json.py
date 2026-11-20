"""Bounded JSON object inputs shared by the tools HTTP transports."""
import json
import math

from starlette.requests import Request

# Space for a 20 MiB evidence object, base64 encoding and the request envelope.
MAX_BODY_SIZE = 28 * 1024 * 1024


class BodyTooLarge(ValueError):
    def __init__(self):
        super().__init__('请求体超过 28 MiB，请缩小后重试')


async def read_body(request: Request) -> bytes:
    length = request.headers.get('content-length', '')
    if length.isascii() and length.isdecimal() and int(length) > MAX_BODY_SIZE:
        raise BodyTooLarge()
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_SIZE:
            raise BodyTooLarge()
        body.extend(chunk)
    return bytes(body)


def parse_object(body: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'JSON 字段 {key!r} 重复')
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f'JSON 数值 {value} 必须为有限数')

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            invalid_constant(value)
        return number

    try:
        value = json.loads(body, object_pairs_hook=unique, parse_constant=invalid_constant, parse_float=finite_float)
    except RecursionError:
        raise ValueError('JSON 嵌套过深') from None
    if not isinstance(value, dict):
        raise ValueError('请求须为 JSON 对象')
    return value
