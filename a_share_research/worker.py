"""Run queued research and batch steps, one at a time."""

import logging
import time

from a_share_research import consensus_backfill, db, price, research, runner_process, screening, scans, service

log = logging.getLogger(__name__)


AUTO_RESUME_ERROR = "服务重启后自动恢复"


def recover(conn) -> None:
    """Requeue an interrupted worker run once; CLI runs belong to their own process."""
    with db.transaction(conn):
        rows = conn.execute("SELECT id, error FROM runs WHERE status='running' "
                            "AND request_id IS NOT NULL").fetchall()
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


def next_run(conn):
    """Owner submissions precede batch research."""
    return conn.execute("SELECT id, kind FROM runs WHERE status='queued' AND kind IN ('research','price')"
                        " ORDER BY request_id IS NOT NULL, batch_id IS NOT NULL, id LIMIT 1").fetchone()


def execute_run(conn, row):
    handlers = {'research': research.execute, 'price': price.execute}
    handlers[row['kind']](conn, row['id'])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        runtime = runner_process.preflight()
    except RuntimeError:
        log.exception("worker 启动已阻断；修复 Pi 运行时后重启，队列保持原状")
        raise SystemExit(1)
    log.info("pi ready: version=%s node=%s provider=%s model=%s reasoning=%s source=%s",
             runtime["pi_version"], runtime["node_version"], runtime["provider"],
             runtime["model"], runtime["reasoning"], runtime["model_source"])
    reviewer = runtime["review"]
    log.info("review model: provider=%s model=%s reasoning=%s",
             reviewer["provider"], reviewer["model"], reviewer["reasoning"])
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
