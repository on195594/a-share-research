#!/usr/bin/env python3
"""Fetch one retained daily brief through the tools service; Hermes delivers stdout."""
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

STATE = Path(os.environ.get('ASHARE_DAILY_BRIEF_STATE', Path.home() / '.local/state/ashare-daily-brief/state.json'))


def load_state():
    value = json.loads(STATE.read_text()) if STATE.exists() else {'schema_version': 1, 'pending': None}
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'pending'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1):
        raise ToolsError('日报待查记录不兼容；未覆盖')
    pending = value['pending']
    if pending is not None and (not isinstance(pending, dict)
            or set(pending) != {'request_id', 'expected_activation_id'}
            or not isinstance(pending['request_id'], str) or not pending['request_id']
            or type(pending['expected_activation_id']) is not int or pending['expected_activation_id'] < 1):
        raise ToolsError('日报待查请求不兼容；未覆盖')
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
            saved['pending'] = dict(request_id='hermes-daily-' + uuid.uuid4().hex, expected_activation_id=active['id'])
            save_json(STATE, saved)
        receipt = tool('policy.daily_brief', saved['pending'])
        try:
            if (receipt['mode'] != 'service_daily_brief_capture'
                    or receipt['request_id'] != saved['pending']['request_id']
                    or receipt['activation_id'] != saved['pending']['expected_activation_id']):
                raise ValueError
            result = receipt['result']
            if (set(result) != {'status', 'exit_code', 'stdout', 'stderr', 'snapshot', 'dividends'}
                    or result['status'] not in ('ready', 'silent') or type(result['exit_code']) is not int
                    or result['exit_code'] != 0 or not isinstance(result['stdout'], str) or result['stderr'] != ''):
                raise ValueError
            captured = datetime.fromisoformat(receipt['input']['now'])
            now = datetime.now(ZoneInfo('Asia/Shanghai'))
            if captured.tzinfo is None or captured > now:
                raise ValueError
            day = receipt['input']['brief_today']
            if datetime.strptime(day, '%Y-%m-%d').date().isoformat() != day:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ToolsError('日报回执不兼容；保留原请求供核对') from None
        # A completed prior-day request is resolved, but is never sent as today's brief.
        save_json(STATE, {'schema_version': 1, 'pending': None})
        if day != now.date().isoformat():
            return '', '持仓日报：已查回其他日期的评估回执，未作为今日日报发送；下次重新采集。\n', 1
        return result['stdout'].replace('（运行 dividend-check 核对）', '（通过 a-share 核对券商流水）'), '', 0


def main() -> int:
    if len(sys.argv) != 1:
        print('usage: a-share-daily-brief.py', file=sys.stderr)
        return 2
    try:
        output, error, status = check()
    except (ToolsError, OSError, ValueError):
        print('持仓日报：检查未完成；请核对工具服务、政策、凭据和本地待查请求。', file=sys.stderr)
        return 1
    if output: print(output, end='')
    if error: print(error, end='', file=sys.stderr)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
