"""Standard-library transport and atomic local state for Hermes tools clients."""
import json
import os
from pathlib import Path
import subprocess
import tempfile


class ToolsError(ValueError):
    """Safe task failure, without server bodies or credential paths."""


def tool(name: str, arguments: dict) -> dict:
    config = os.environ.get('ASHARE_HERMES_CONFIG', str(Path.home() / '.config/ashare/hermes-assistant.json'))
    try:
        result = subprocess.run(['ashare', '--config', config, 'call', name, '--json', json.dumps(arguments)],
                                capture_output=True, text=True, timeout=40, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        raise ToolsError(f'{name} 客户端不可用或超时；未自动重试') from None
    if result.returncode:
        raise ToolsError(f'{name} 未完成；请检查工具服务、凭据、账户来源与生效政策')
    try:
        response = json.loads(result.stdout)
        data = response['result']['data']
        if response.get('ok') is not True or not isinstance(data, dict):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ToolsError(f'{name} 返回格式不兼容') from None
    return data


def save_json(path: Path, value: dict) -> None:
    """Retain a pending request before transport; never partially overwrite its predecessor."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
