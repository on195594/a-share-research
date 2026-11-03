"""Opt-in adapter for an existing no-agent scheduler; never installs or sends notifications."""
import argparse
from datetime import datetime
import fcntl
import json
import logging
import os
import re
from pathlib import Path
import sys
from urllib.parse import urlparse

import requests

from a_share_research.db import SHANGHAI

log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('submit', 'status'))
    parser.add_argument('--month', required=True, help='Scheduled Shanghai YYYY-MM; never infer a missed month')
    parser.add_argument('--execute', action='store_true', help='Explicitly permit the API request; submit may incur model costs')
    parser.add_argument('--receipt', type=Path, help='Scheduler-private stdout deduplication file; required for execute')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', args.month):
        parser.error('月份须为 YYYY-MM')
    if args.execute and args.mode == 'submit' and args.month != datetime.now(SHANGHAI).strftime('%Y-%m'):
        parser.error('只处理当前上海月份，不补跑旧月份')
    if not args.execute:
        log.info('dry-run: %s %s; no request; execution time and spending policy must be approved before enabling', args.mode, args.month)
        return
    if args.receipt is None:
        parser.error('--execute requires --receipt outside the repository')
    base = os.environ.get('A_STOCK_RESEARCH_URL', '').rstrip('/')
    token = os.environ.get('A_STOCK_RESEARCH_TOKEN', '')
    url = urlparse(base)
    if not token or url.username or url.password or not (
            url.scheme == 'https' or url.scheme == 'http' and url.hostname in ('127.0.0.1', 'localhost')):
        parser.error('需要已有的 Research URL/token；仅 HTTPS 或本机 HTTP，不配置或输出凭据')
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    # The lock and receipt only suppress duplicate stdout; the server owns admission idempotency.
    with args.receipt.open('a+', encoding='utf-8') as receipt:
        os.chmod(args.receipt, 0o600)
        fcntl.flock(receipt, fcntl.LOCK_EX)
        receipt.seek(0)
        saved = receipt.read().strip()
        try:
            response = requests.request('POST' if args.mode == 'submit' else 'GET',
                base + '/api/batches/monthly' + ('' if args.mode == 'submit' else '/' + args.month),
                headers={'Authorization': 'Bearer ' + token},
                json={'month': args.month} if args.mode == 'submit' else None,
                timeout=(10, 30), allow_redirects=False)
            if response.status_code != 200:
                raise ValueError(f'HTTP {response.status_code}')
            batch = response.json()['batch']
        except (requests.RequestException, ValueError, KeyError) as exc:
            log.error('月批请求失败（%s），不自动重试；先查询服务端状态', type(exc).__name__)
            raise SystemExit(1) from None
        if not batch or batch['status'] not in ('frozen', 'tracking', 'failed', 'proposed'):
            return  # Queued/in-progress is not a completion notification.
        event = 'failed' if batch['status'] == 'failed' else 'needs_owner' if batch['status'] == 'proposed' else 'completed'
        if event == 'completed' and (batch.get('failed_research') or batch.get('ranking_error') or batch.get('error')):
            event = 'completed_with_gaps'
        key = f"{args.month}:{batch['id']}:{event}"
        if saved == key:
            return
        sys.stdout.write(json.dumps({'month': args.month, 'event': event, 'batch': batch}, ensure_ascii=False) + '\n')
        sys.stdout.flush()
        receipt.seek(0)
        receipt.truncate()
        receipt.write(key)
        receipt.flush()
        os.fsync(receipt.fileno())


if __name__ == '__main__':
    main()
