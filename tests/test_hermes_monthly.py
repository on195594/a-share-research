"""Hermes monthly tool arguments, full status notifications and their durable dedup key."""
import json
from pathlib import Path

import pytest

from test_ops_hermes import load


def test_monthly_request_and_full_status_use_the_same_month(ashare_cli, tmp_path, monkeypatch, capsys):
    module = load('a-share-monthly')
    monkeypatch.setattr(module, 'STATE', tmp_path / 'monthly')
    captured = json.loads((Path(__file__).parent / 'data/monthly-research-status.json').read_text())
    batch = captured['batches'][0]
    month = batch['created_at'][:7]
    failed = next(row for row in captured['runs'] if row['id'] in {r['run_id'] for r in batch['q20']}
                  and row['status'] == 'failed')
    monkeypatch.setattr(module.sys, 'argv', ['a-share-monthly.py', 'submit', month])
    ashare_cli.reply('research_batch_monthly_request', code=22)
    module.main()
    notice = capsys.readouterr().out
    assert notice.strip() == f'【月批】结果未知，请先查询 {month} 的批次状态'
    ashare_cli.reply('research_batch_monthly_status', {'batch': None})
    assert module.status(month) == ''
    assert (module.STATE / f'{month}.receipt').read_text() == ''

    ashare_cli.reply('research_batch_monthly_request', {
        'batch_id': batch['id'], 'status': 'queued', 'created': True,
    })
    assert f"已发起 {month} 月度候选批次 #{batch['id']}" in module.submit(month)
    ashare_cli.reply('research_batch_monthly_request', {
        'batch_id': batch['id'], 'status': batch['status'], 'created': False,
    })
    assert f"{month} 已有批次 #{batch['id']}" in module.submit(month)
    ashare_cli.reply('research_batch_monthly_status', {'batch': batch | {'failed_research': [failed]}})
    assert f"{failed['code']} {failed['error']}" in module.status(month)
    assert module.status(month) == ''
    assert ashare_cli.calls == [
        ('research_batch_monthly_request', {'month': month}),
        ('research_batch_monthly_status', {'month': month, 'compact': False}),
        ('research_batch_monthly_request', {'month': month}),
        ('research_batch_monthly_request', {'month': month}),
        ('research_batch_monthly_status', {'month': month, 'compact': False}),
        ('research_batch_monthly_status', {'month': month, 'compact': False}),
    ]


@pytest.mark.parametrize(("code", "expected"), [(21, "发布暂停或服务繁忙"), (1, "调用失败")])
def test_monthly_submit_cli_errors(ashare_cli, monkeypatch, capsys, code, expected):
    module = load('a-share-monthly')
    month = '2026-11'
    monkeypatch.setattr(module.sys, 'argv', ['a-share-monthly.py', 'submit', month])
    ashare_cli.reply('research_batch_monthly_request', code=code)
    module.main()
    notice = capsys.readouterr().out
    assert f"{month} 受理未完成" in notice and expected in notice
    assert ashare_cli.calls == [('research_batch_monthly_request', {'month': month})]
