#!/usr/bin/env python3
"""Request an evidenced stop assessment; Hermes owns delivery, this client owns dedup state."""
from datetime import date, datetime
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import uuid
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ashare_tools import ToolsError, save_json, tool

STATE = Path(os.environ.get('ASHARE_STOP_ALERT_STATE', Path.home() / '.local/state/ashare-stop-alert/state.json'))


def notification_state(value):
    if not isinstance(value, dict) or set(value) - {'date', 'levels'}:
        raise ToolsError('止损通知状态不兼容；未覆盖')
    if 'date' in value:
        day = value['date']
        if not isinstance(day, str) or re.fullmatch('[0-9]{4}-[0-9]{2}-[0-9]{2}', day) is None:
            raise ToolsError('止损通知日期无效；未覆盖')
        date.fromisoformat(day)
    levels = value.get('levels', {})
    if not isinstance(levels, dict) or any(not isinstance(code, str) or not code.strip()
            or level not in ('near', 'stop1', 'stop2') for code, level in levels.items()):
        raise ToolsError('止损通知档位无效；未覆盖')
    return value


def load_state():
    value = json.loads(STATE.read_text()) if STATE.exists() else {}
    if isinstance(value, dict) and set(value) <= {'date', 'levels'}:
        value = dict(schema_version=1, state=value, pending=None)
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'state', 'pending'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1):
        raise ToolsError('止损任务记录不兼容；未覆盖')
    notification_state(value['state'])
    pending = value['pending']
    if pending is not None and (not isinstance(pending, dict)
            or set(pending) != {'request_id', 'expected_activation_id', 'previous_state'}
            or not isinstance(pending['request_id'], str) or not pending['request_id']
            or type(pending['expected_activation_id']) is not int or pending['expected_activation_id'] < 1
            or pending['previous_state'] != value['state']):
        raise ToolsError('止损待查请求不兼容；未覆盖')
    return value


def check() -> tuple[str, str, int]:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with STATE.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return '', '', 0
        saved = load_state()
        if saved['pending'] is None:
            current = tool('policy.current', {})
            activation = current.get('activation') or {}
            if (current.get('installed') is not True or not isinstance(activation, dict)
                    or type(activation.get('id')) is not int or activation['id'] < 1):
                raise ToolsError('尚无与实装一致的生效政策')
            saved['pending'] = dict(request_id='hermes-stop-' + uuid.uuid4().hex,
                expected_activation_id=activation['id'], previous_state=saved['state'])
            save_json(STATE, saved)
        receipt = tool('policy.stop_alert', saved['pending'])
        try:
            if (receipt['mode'] != 'service_stop_alert_capture'
                    or receipt['request_id'] != saved['pending']['request_id']
                    or receipt['activation_id'] != saved['pending']['expected_activation_id']
                    or receipt['input']['previous_state'] != saved['state']):
                raise ValueError
            result = receipt['result']
            if (set(result) != {'status', 'exit_code', 'stdout', 'stderr', 'state', 'alerts'}
                    or type(result['exit_code']) is not int or result['exit_code'] not in (0, 1)
                    or not isinstance(result['stdout'], str) or not isinstance(result['stderr'], str)
                    or result['status'] not in ('outside_session', 'no_holdings', 'unavailable', 'no_alerts', 'alerts')
                    or not isinstance(result['alerts'], list)):
                raise ValueError
            next_state = notification_state(result['state'])
            captured = datetime.fromisoformat(receipt['input']['now'])
            now = datetime.now(ZoneInfo('Asia/Shanghai'))
            if captured.tzinfo is None or captured > now:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ToolsError('止损回执不兼容；保留原请求供核对') from None
        if result['alerts'] and (now - captured).total_seconds() > 900:
            # A recovered old alert never advances the notification levels without delivery.
            save_json(STATE, dict(schema_version=1, state=saved['state'], pending=None))
            return '', '止损预警：已查回过期评估，未作盘中提醒；下次检查重新采集。\n', 1
        save_json(STATE, dict(schema_version=1, state=next_state, pending=None))
        output = result['stdout'].replace('按 a-stock-monitor 复核止损与 L3；', '按 a-share 的生效政策复核止损与 L3；')
        return output, result['stderr'], result['exit_code']


def main() -> int:
    if len(sys.argv) != 1:
        print('usage: a-share-stop-alert.py', file=sys.stderr)
        return 2
    try:
        output, error, status = check()
    except (ToolsError, OSError, ValueError):
        print('止损预警：检查未完成；请核对工具服务、政策、凭据和本地待查请求，原通知状态未确认推进。', file=sys.stderr)
        return 1
    if output:
        print(output, end='')
    if error:
        print(error, end='', file=sys.stderr)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
