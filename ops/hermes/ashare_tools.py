"""Standard-library transport and atomic local state for Hermes tools clients."""
import hashlib
import json
import uuid
import os
from pathlib import Path
import subprocess
import tempfile


class ToolsError(ValueError):
    """Safe task failure, without server bodies or credential paths."""

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


def tool(name: str, arguments: dict) -> dict:
    config = os.environ.get('ASHARE_HERMES_CONFIG', str(Path.home() / '.config/ashare/hermes-assistant.json'))
    try:
        result = subprocess.run(['ashare', '--config', config, 'call', name, '--json', json.dumps(arguments)],
                                capture_output=True, text=True, timeout=40, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        raise ToolsError(f'{name} 客户端不可用或超时；未自动重试') from None
    if result.returncode == 20:
        raise ToolsError('生效政策已变化；原请求确定未写入评估', code='policy_activation_changed')
    if result.returncode == 21:
        raise ToolsError(f'{name} 工具服务暂停或未受理；原请求可原样重试', code='service_unavailable')
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


def policy_receipt(name, saved, path, *, call=tool):
    """One certain non-write rejection may rebind; ambiguous outcomes never discard pending."""
    try:
        return call(name, saved['pending'])
    except ToolsError as exc:
        if exc.code != 'policy_activation_changed':
            raise
    original = saved['pending']
    digest = hashlib.sha256(original['request_id'].encode()).hexdigest()
    archive = path.with_name(path.name + '.rejected-' + digest + '.json')
    save_json(archive, dict(reason='policy_activation_changed', request=original))
    current = call('policy.current', {})
    activation = current.get('activation') or {}
    if (current.get('installed') is not True or not isinstance(activation, dict)
            or type(activation.get('id')) is not int or activation['id'] < 1
            or activation['id'] == original['expected_activation_id']):
        raise ToolsError('未发现新的实装生效政策；保留原请求')
    replacement = original | dict(request_id='hermes-rebound-' + uuid.uuid4().hex,
                                  expected_activation_id=activation['id'])
    save_json(path, saved | dict(pending=replacement))
    saved['pending'] = replacement
    # No loop: a second change leaves the replacement pending until another run.
    return call(name, replacement)


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
