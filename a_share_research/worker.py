"""Run queued research one at a time, then batch steps; the only process holding model and market credentials."""

import logging
import time

from a_share_research import consensus_backfill, db, model, price, research, screening, scans

log = logging.getLogger(__name__)


AUTO_RESUME_ERROR = "服务重启后自动恢复"


def recover(conn) -> None:
    """Requeue an interrupted worker run once; CLI runs belong to their own process."""
    with db.transaction(conn):
        rows = conn.execute("SELECT id, error, EXISTS(SELECT 1 FROM batches b WHERE b.request_id LIKE 'monthly:%' "
                            "AND runs.request_id = 'batch-' || b.id || '-' || runs.code) AS monthly "
                            "FROM runs WHERE status='running' AND request_id IS NOT NULL").fetchall()
        for row in rows:
            if row["monthly"]:
                db.update_run(conn, row["id"], status="failed", step=None, finished_at=db.now(),
                              error="自动月批研究中断，不自动重跑；请检查已记录调用")
            elif row["error"] == AUTO_RESUME_ERROR:
                db.update_run(conn, row["id"], status="failed", step=None, finished_at=db.now(),
                              error="研究中断（已自动恢复仍未完成，请重新开始）")
            else:
                db.update_run(conn, row["id"], status="queued", step="resumed", finished_at=None,
                              error=AUTO_RESUME_ERROR)


def next_run(conn):
    """A candidate batch queues 20 research runs; the owner's own submissions go ahead of them."""
    return conn.execute("SELECT id, kind FROM runs WHERE status = 'queued' AND kind != 'diagnostic'"
                        " ORDER BY request_id LIKE 'batch-%', id LIMIT 1").fetchone()


def next_diagnostic(conn):
    """Diagnostics run only after research, scans and batch steps found nothing to do."""
    return conn.execute("SELECT id FROM runs WHERE status = 'queued' AND kind = 'diagnostic' ORDER BY id LIMIT 1").fetchone()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        runtime = model.preflight()
    except RuntimeError:
        log.exception("worker 启动已阻断；修复 Pi 运行时后重启，队列保持原状")
        raise SystemExit(1)
    log.info("pi ready: version=%s node=%s provider=%s model=%s source=%s",
             runtime["pi_version"], runtime["node_version"], runtime["provider"],
             runtime["model"], runtime["model_source"])
    conn = db.connect()
    recover(conn)
    screening.recover(conn)
    scans.recover(conn)
    log.info("worker ready")
    while True:
        row = next_run(conn)
        if row is None:
            scan = conn.execute("SELECT id FROM scans WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if scan:
                scans.execute(conn, scan['id'])
                continue
            # Batch steps run only when no single-company task is waiting.
            if not screening.tick(conn):
                if diagnostic := next_diagnostic(conn):
                    research.execute(conn, diagnostic["id"])
                    continue
                try:
                    consensus_backfill.tick()
                except Exception:
                    log.exception("report_rc 空闲续补失败；不影响研究队列，下轮冷却后重试")
                time.sleep(2)
            continue
        log.info("run %s started", row["id"])
        (price if row["kind"] == "price" else research).execute(conn, row["id"])
        log.info("run %s finished", row["id"])


if __name__ == "__main__":
    main()
