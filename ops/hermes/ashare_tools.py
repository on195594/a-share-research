"""Standard-library transport and atomic local state for Hermes tools clients."""
import hashlib
import json
import uuid
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlsplit


class ToolsError(ValueError):
    """Safe task failure, without server bodies or credential paths."""

    def __init__(self, message, *, code='failed', transient=False):
        super().__init__(message)
        self.code = code
        # A lost or timed-out reply, safe to repeat only for idempotent calls.
        self.transient = transient


def config_path() -> Path:
    return Path(os.environ.get('ASHARE_HERMES_CONFIG', Path.home() / '.config/ashare/hermes-assistant.json'))


def client_identity() -> tuple[str, str]:
    """Bind recoverable writes to the configured MCP endpoint and grant, independent of JSON formatting."""
    try:
        config = json.loads(config_path().read_text())
        identity = [config['url'], config['token']]
        if not all(isinstance(value, str) and value for value in identity):
            raise ValueError
        url = urlsplit(config['url'])
        if not url.hostname or url.username is not None or url.password is not None:
            raise ValueError
        public_base = f'https://{url.netloc}' if url.scheme == 'https' else ''
    except (OSError, ValueError, KeyError, TypeError):
        raise ToolsError('凭据配置不可读或不完整，请核对 Hermes 私有配置后用原请求恢复') from None
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest(), public_base


def tool(name: str, arguments: dict, *, timeout: float = 40) -> dict:
    unknown = f'{name} 结果未知，请用原身份与原请求恢复'
    try:
        result = subprocess.run(['ashare', '--config', str(config_path()), 'call', name, '--json', json.dumps(arguments)],
                                capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ToolsError(unknown, code='outcome_unknown', transient=True) from None
    except UnicodeError:
        raise ToolsError(unknown, code='outcome_unknown') from None
    except OSError:
        raise ToolsError(f'{name} 客户端未启动，请检查 ashare 安装') from None
    if result.returncode == 20:
        raise ToolsError('政策异常：生效政策已变化，原请求确定未写入，请核对当前生效政策', code='policy_activation_changed')
    if result.returncode == 21:
        raise ToolsError(f'{name} 发布暂停或服务繁忙，恢复后可原样重试', code='service_unavailable')
    if result.returncode == 22 or result.returncode < 0:
        raise ToolsError(unknown, code='outcome_unknown', transient=True)
    if result.returncode == 23:
        raise ToolsError(f'{name} 回执超限，请用支持更大回执的 MCP 客户端，沿用原身份和原请求查回', code='outcome_unknown')
    if result.returncode:
        raise ToolsError(f'{name} 调用失败，请核对凭据是否失效、工具权限与请求参数')
    try:
        response = json.loads(result.stdout)
        data = response['result']['data']
        if response.get('ok') is not True or not isinstance(data, dict):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ToolsError(f'{name} 回执格式不兼容，{unknown}', code='outcome_unknown') from None
    return data


def bound_policy_call(name, saved, *, call=tool):
    if 'binding' not in saved:
        raise ToolsError('待查政策请求缺少身份绑定，请先用原 assistant 身份与旧脚本查回')
    if saved['binding'] != client_identity()[0]:
        raise ToolsError('服务或凭据已变化，请恢复原身份查回待查政策请求')
    return call(name, saved['pending'])


def policy_receipt(name, saved, path, *, call=tool):
    """One certain non-write rejection may rebind; ambiguous outcomes never discard pending."""
    try:
        return bound_policy_call(name, saved, call=call)
    except ToolsError as exc:
        if exc.code != 'policy_activation_changed':
            raise
    original = saved['pending']
    digest = hashlib.sha256(original['request_id'].encode()).hexdigest()
    archive = path.with_name(path.name + '.rejected-' + digest + '.json')
    save_json(archive, dict(reason='policy_activation_changed', request=original))
    current = call('policy_current', {})
    activation = current.get('activation') or {}
    if (current.get('installed') is not True or not isinstance(activation, dict)
            or type(activation.get('id')) is not int or activation['id'] < 1
            or activation['id'] == original['expected_activation_id']):
        raise ToolsError('政策异常：未发现新的实装生效政策，请核对政策后恢复原请求')
    replacement = original | dict(request_id='hermes-rebound-' + uuid.uuid4().hex,
                                  expected_activation_id=activation['id'])
    save_json(path, saved | dict(pending=replacement))
    saved['pending'] = replacement
    # No loop: a second change leaves the replacement pending until another run.
    return bound_policy_call(name, saved, call=call)


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
