"""Standard-library transport for the scheduled Hermes client."""
import json
import os
from pathlib import Path
import subprocess


class ToolsError(ValueError):
    """Safe task failure, without server bodies or credential paths."""

    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def config_path() -> Path:
    return Path(os.environ.get('ASHARE_HERMES_CONFIG', Path.home() / '.config/ashare/hermes-assistant.json'))


def tool(name: str, arguments: dict, *, timeout: float = 40) -> dict:
    unknown = f'{name} 结果未知，请用原身份与原请求恢复'
    try:
        result = subprocess.run(['ashare', '--config', str(config_path()), 'call', name, '--json', json.dumps(arguments)],
                                capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ToolsError(unknown, retryable=True) from None
    except UnicodeError:
        raise ToolsError(unknown) from None
    except OSError:
        raise ToolsError(f'{name} 客户端未启动，请检查 ashare 安装') from None
    if result.returncode == 21:
        raise ToolsError(f'{name} 发布暂停或服务繁忙，恢复后可原样重试', retryable=True)
    # A signal can also interrupt the CLI after the service accepted the call.
    if result.returncode == 22 or result.returncode < 0:
        raise ToolsError(unknown, retryable=True)
    if result.returncode:
        raise ToolsError(f'{name} 调用失败，请核对凭据是否失效、工具权限与请求参数')
    try:
        response = json.loads(result.stdout)
        data = response['result']['data']
        if response.get('ok') is not True or not isinstance(data, dict):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ToolsError(f'{name} 回执格式不兼容，{unknown}') from None
    return data
