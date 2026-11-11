"""Read-only V1/V5 inputs for one external producer cohort, never the frozen worker pool."""
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from a_share_research import forecasts, validation


def _reports(database: str, key_id: int, account_aware: bool, contract: int | None):
    if type(key_id) is not int or key_id < 1 or type(account_aware) is not bool:
        raise ValueError('必须指定有效生产者 Key 编号和账户知情布尔值')
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT r.id, r.code, r.name, r.created_at, r.coverage, r.body FROM reports r "
            "JOIN runs u ON u.id=r.run_id WHERE u.kind='external' AND u.status='done' "
            "AND u.replay_of IS NULL AND r.passed=1 "
            "AND json_extract(r.body,'$.producer.kind')='external' "
            "AND json_type(r.body,'$.producer.key_id')='integer' "
            "AND json_extract(r.body,'$.producer.key_id')=? "
            "AND json_type(r.body,'$.producer.account_aware')=? ORDER BY r.created_at,r.id",
            (key_id, 'true' if account_aware else 'false')).fetchall()
    selected = []
    for row in rows:
        body = json.loads(row['body'])
        item = dict(row) | {'body': body, 'contract': validation.judgment_contract(body)}
        selected.append(item)
    return validation.filter_contract(selected, contract)


def load_forecasts(database: str, *, key_id: int, account_aware: bool,
                   contract: int | None = None) -> list[dict]:
    """Filter producer and judgment contract before assigning earliest forecast identities.

    The result is compatible with validation.score_forecasts/summarize_forecasts.
    Call separately for each producer; do not concatenate cohorts before scoring.
    """
    out = []
    for row in _reports(database, key_id, account_aware, contract):
        body = row['body']
        table = body.get('table') or []
        base = forecasts.base_period(table)
        written = {item['id']: item for item in table}
        for index, item in enumerate(body.get('checkpoints') or []):
            forecast = item.get('forecast') if isinstance(item, dict) else None
            if base and forecast and not forecasts.problems(forecast, table):
                out.append(dict(report=row['id'], code=row['code'], name=row['name'], created=row['created_at'],
                                index=index, contract=row['contract'], base=base, forecast=forecast,
                                written=written[forecast['metric']], producer=body['producer']))
    return validation.mark_first(out)


def load_agenda(database: str, *, key_id: int, account_aware: bool,
                contract: int | None = None) -> list[dict]:
    """Load one producer's V5 continuations before agenda.sample applies its deduplication."""
    out = []
    for row in _reports(database, key_id, account_aware, contract):
        body = row['body']
        if isinstance(body.get('prior'), dict):
            out.append(dict(id=row['id'], code=row['code'], name=row['name'], source='external',
                            period=row['coverage'], completed=datetime.fromisoformat(row['created_at']),
                            prior_period=body['prior'].get('period'), contract=row['contract'],
                            prior_status=body.get('prior_status'), producer=body['producer']))
    return out
