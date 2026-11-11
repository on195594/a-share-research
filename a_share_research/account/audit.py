"""Read-only logical account-journal checks; evidence files are checked separately."""
import json
from datetime import datetime

from a_share_research import gate
from a_share_research.account.reconciliation import Event, Opening, identity, project


def _rows(conn, table):
    cursor = conn.execute(f'SELECT * FROM {table}')
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def inspect(conn) -> list[str]:
    """Detect inconsistent ownership, receipts and projections without repairing anything."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {'account_books', 'account_batches', 'account_journal', 'account_voids'}
    if not required & tables:
        return []
    if required - tables:
        return ['账户账本缺表：' + ', '.join(sorted(required - tables))]
    problems = []
    books = {r['account_id']: r for r in _rows(conn, 'account_books')}
    batches = {r['id']: r for r in _rows(conn, 'account_batches')}
    journal = {r['id']: r for r in _rows(conn, 'account_journal')}
    voids = _rows(conn, 'account_voids')
    void_ids = {r['event_id'] for r in voids}
    parsed = {}
    for row in journal.values():
        label = f"账户事件 {row['id']}"
        batch = batches.get(row['batch_id'])
        if row['account_id'] not in books or not batch or batch['account_id'] != row['account_id']:
            problems.append(label + '：账户/批次归属不一致')
        elif batch['status'] != 'posted':
            problems.append(label + '：事件属于未入账批次')
        try:
            event = Event.model_validate_json(row['event'])
            parsed[row['id']] = event
            if row['business_id'] != identity(event, row['account_id']):
                problems.append(label + '：业务身份不一致')
            if row['company_key'] != event.code or row['occurred_at'] != event.occurred_at.isoformat():
                problems.append(label + '：公司/时间与事件载荷不一致')
        except (ValueError, TypeError):
            problems.append(label + '：事件载荷非法')
    for row in voids:
        event, batch = journal.get(row['event_id']), batches.get(row['batch_id'])
        if not event or not batch or event['account_id'] != batch['account_id'] or batch['status'] != 'posted':
            problems.append(f"账户作废 {row['event_id']}：账户/批次归属或状态不一致")
        elif event['batch_id'] >= row['batch_id']:
            problems.append(f"账户作废 {row['event_id']}：不能作废本批或未来事件")
    for row in batches.values():
        label = f"账户批次 {row['id']}"
        if row['account_id'] not in books:
            problems.append(label + '：账户不存在')
        try:
            request, receipt = json.loads(row['request']), json.loads(row['receipt'])
            if not isinstance(request, dict) or not isinstance(receipt, dict):
                raise ValueError
            if gate.digest(request) != row['request_hash']:
                problems.append(label + '：请求哈希不一致')
            expected = {'receipt_id': row['id'], 'account_id': row['account_id'],
                        'request_hash': row['request_hash'], 'status': row['status'], 'mode': 'shadow'}
            if any(receipt.get(k) != v for k, v in expected.items()):
                problems.append(label + '：回执身份/状态不一致')
            added = sum(e['batch_id'] == row['id'] for e in journal.values())
            voided = sum(v['batch_id'] == row['id'] for v in voids)
            if receipt.get('added') != added or receipt.get('voided') != voided:
                problems.append(label + '：回执事件计数不一致')
            if row['status'] != 'posted' and (added or voided or receipt.get('before') != receipt.get('after')):
                problems.append(label + '：未入账回执改变账户')
        except (ValueError, TypeError):
            problems.append(label + '：请求或回执非法')
    for account, book in books.items():
        label = f'账户 {account}'
        active = [r for r in journal.values() if r['account_id'] == account and r['id'] not in void_ids]
        ids = [r['business_id'] for r in active]
        if len(ids) != len(set(ids)):
            problems.append(label + '：有效业务身份重复')
        try:
            opening = Opening.model_validate_json(book['opening'])
            projected = Opening.model_validate_json(book['projection'])
            events = [parsed[r['id']] for r in sorted(active, key=lambda r: r['id'])]
            if any(e.occurred_at < datetime.fromisoformat(book['opened_at']) for e in events):
                problems.append(label + '：事件早于期初')
            if project(opening, events) != projected:
                problems.append(label + '：事件重放与投影不一致')
        except (ValueError, TypeError, KeyError):
            problems.append(label + '：期初/事件无法重放')
        changing_batches = {r['batch_id'] for r in journal.values() if r['account_id'] == account}
        changing_batches |= {r['batch_id'] for r in voids
                             if batches.get(r['batch_id'], {}).get('account_id') == account}
        if book['version'] != len(changing_batches):
            problems.append(label + '：账本版本与入账批次不一致')
    return problems
