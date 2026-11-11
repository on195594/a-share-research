"""Run queued research one at a time, then batch steps; the only process holding model and market credentials."""

import logging
import time

from a_share_research import consensus_backfill, db, model, price, research, screening, scans, service, external_review

log = logging.getLogger(__name__)


AUTO_RESUME_ERROR = "服务重启后自动恢复"


def recover(conn) -> None:
    """Requeue an interrupted worker run once; CLI runs belong to their own process."""
    with db.transaction(conn):
        rows = conn.execute("SELECT id, error FROM runs WHERE status='running' "
                            "AND request_id IS NOT NULL AND kind != 'external'").fetchall()
        for row in rows:
            if service.is_monthly_research(conn, row["id"]):
                db.update_run(conn, row["id"], status="failed", step=None, finished_at=db.now(),
                              error="自动月批研究中断，不自动重跑；请检查已记录调用")
            elif row["error"] == AUTO_RESUME_ERROR:
                db.update_run(conn, row["id"], status="failed", step=None, finished_at=db.now(),
                              error="研究中断（已自动恢复仍未完成，请重新开始）")
            else:
                db.update_run(conn, row["id"], status="queued", step="resumed", finished_at=None,
                              error=AUTO_RESUME_ERROR)


def next_run(conn, *, external_enabled: bool = False):
    """Owner submissions precede external review, which precedes batch research."""
    allow_external = external_enabled and not external_review.protected()
    return conn.execute("SELECT id, kind FROM runs WHERE status='queued' AND kind IN ('research','price','external')"
                        " AND (kind != 'external' OR ?)"
                        " ORDER BY request_id IS NOT NULL,"
                        " CASE WHEN kind='external' THEN 1 WHEN batch_id IS NOT NULL THEN 2 ELSE 0 END,id LIMIT 1",
                        (allow_external,)).fetchone()


def execute_run(conn, row):
    handlers = {'research': research.execute, 'price': price.execute, 'external': external_review.execute}
    handlers[row['kind']](conn, row['id'])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        runtime = model.preflight()
    except RuntimeError:
        log.exception("worker 启动已阻断；修复 Pi 运行时后重启，队列保持原状")
        raise SystemExit(1)
    log.info("pi ready: version=%s node=%s provider=%s model=%s reasoning=%s source=%s",
             runtime["pi_version"], runtime["node_version"], runtime["provider"],
             runtime["model"], runtime.get("reasoning", "high"), runtime["model_source"])
    if runtime.get("review"):
        log.info("review model: provider=%s model=%s reasoning=%s", *runtime["review"].values())
    conn = db.connect()
    recover(conn)
    external_review.recover(conn)
    screening.recover(conn)
    scans.recover(conn)
    log.info("worker ready")
    while True:
        row = next_run(conn, external_enabled=external_review.enabled())
        if row is None:
            scan = conn.execute("SELECT id FROM scans WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if scan:
                scans.execute(conn, scan['id'])
                continue
            # Batch steps run only when no single-company task is waiting.
            if not screening.tick(conn):
                try:
                    consensus_backfill.tick()
                except Exception:
                    log.exception("report_rc 空闲续补失败；不影响研究队列，下轮冷却后重试")
                time.sleep(2)
            continue
        log.info("run %s started", row["id"])
        execute_run(conn, row)
        log.info("run %s finished", row["id"])


if __name__ == "__main__":
    main()
