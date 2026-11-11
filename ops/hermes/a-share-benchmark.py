#!/usr/bin/env python3
"""Fetch one retained monthly benchmark through the tools service; Hermes delivers stdout."""
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import sys
import uuid
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ashare_tools import ToolsError, save_json, tool

STATE = Path(os.environ.get('ASHARE_BENCHMARK_STATE', Path.home() / '.local/state/ashare-benchmark/state.json'))


def load_state():
    value = json.loads(STATE.read_text()) if STATE.exists() else {'schema_version': 1, 'pending': None}
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'pending'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1):
        raise ToolsError('月度对比待查记录不兼容；未覆盖')
    pending = value['pending']
    if pending is not None and (not isinstance(pending, dict)
            or set(pending) != {'request_id', 'expected_activation_id', 'end'}
            or not isinstance(pending['request_id'], str) or not pending['request_id']
            or type(pending['expected_activation_id']) is not int or pending['expected_activation_id'] < 1):
        raise ToolsError('月度对比待查请求不兼容；未覆盖')
    if pending is not None:
        try:
            if datetime.strptime(pending['end'], '%Y-%m-%d').date().isoformat() != pending['end']:
                raise ValueError
        except (ValueError, TypeError):
            raise ToolsError('月度对比待查日期不兼容；未覆盖') from None
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
            active = current.get('activation') or {}
            if (current.get('installed') is not True or not isinstance(active, dict)
                    or type(active.get('id')) is not int or active['id'] < 1):
                raise ToolsError('尚无与实装一致的生效政策')
            saved['pending'] = dict(request_id='hermes-benchmark-' + uuid.uuid4().hex, expected_activation_id=active['id'],
                                    end=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat())
            save_json(STATE, saved)
        receipt = tool('policy.benchmark', saved['pending'])
        try:
            if (receipt['mode'] != 'service_benchmark_capture'
                    or receipt['request_id'] != saved['pending']['request_id']
                    or receipt['activation_id'] != saved['pending']['expected_activation_id']):
                raise ValueError
            if type(receipt['activation_id']) is not int or receipt['input']['end'] != saved['pending']['end']:
                raise ValueError
            result = receipt['result']
            if (not isinstance(result, dict)
                    or set(result) != {'status', 'exit_code', 'stdout', 'stderr', 'metrics', 'failures'}
                    or result['status'] not in ('ready', 'no_events', 'unavailable')
                    or type(result['exit_code']) is not int
                    or result['exit_code'] != (0 if result['status'] == 'ready' else 1)
                    or not isinstance(result['stdout'], str) or not isinstance(result['stderr'], str)
                    or not isinstance(result['failures'], list)
                    or (result['status'] == 'ready' and not isinstance(result['metrics'], dict))
                    or (result['status'] != 'ready' and (result['metrics'] is not None or not result['stderr']))):
                raise ValueError
            captured = datetime.fromisoformat(receipt['input']['now'])
            now = datetime.now(ZoneInfo('Asia/Shanghai'))
            if captured.tzinfo is None or captured.utcoffset() is None or captured > now:
                raise ValueError
            day = receipt['input']['end']
        except (KeyError, TypeError, ValueError):
            raise ToolsError('月度对比回执不兼容；保留原请求供核对') from None
        # Resolve old requests without presenting their report as today's capture.
        save_json(STATE, {'schema_version': 1, 'pending': None})
        if day != now.date().isoformat():
            return '', '持仓月度对比：已查回其他日期的评估回执，未作为今日月度对比发送；下次重新采集。\n', 1
        output = ('📊 A股账本月度对比\n' + result['stdout']) if result['status'] == 'ready' else result['stdout']
        return output, result['stderr'], result['exit_code']


def main() -> int:
    if len(sys.argv) != 1:
        print('usage: a-share-benchmark.py', file=sys.stderr)
        return 2
    try:
        output, error, status = check()
    except (ToolsError, OSError, ValueError):
        print('持仓月度对比：检查未完成；请核对工具服务、政策、凭据和本地待查请求。', file=sys.stderr)
        return 1
    if output: print(output, end='')
    if error: print(error, end='', file=sys.stderr)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
