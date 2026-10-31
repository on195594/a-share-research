"""Run queued research one at a time, then batch steps; the only process holding model and market credentials."""

import json
import logging
import time

from a_share_research import db, model, price, research, screening, progress

log = logging.getLogger(__name__)


def recover(conn) -> None:
    """Requeue an interrupted worker run once; CLI runs belong to their own process."""
    with db.transaction(conn):
        rows = conn.execute("SELECT id, request FROM runs WHERE status='running' AND request_id IS NOT NULL").fetchall()
        for row in rows:
            request = json.loads(row["request"] or "{}") or {}
            if request.get("auto_resume_count", 0):
                db.update_run(conn, row["id"], status="failed", step=None, finished_at=db.now(),
                              error="研究中断（已自动恢复仍未完成，请重新开始）")
            else:
                request["auto_resume_count"] = 1
                db.update_run(conn, row["id"], status="queued", step="resumed", error=None, finished_at=None,
                              request=json.dumps(request, ensure_ascii=False))


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
    log.info("worker ready")
    while True:
        # A candidate batch queues 20 research runs; the owner's own submissions go ahead of them.
        row = conn.execute("SELECT id, kind FROM runs WHERE status = 'queued'"
                           " ORDER BY request_id LIKE 'batch-%', id LIMIT 1").fetchone()
        if row is None:
            comparison = conn.execute("SELECT id FROM research_progress WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if comparison:
                progress.execute(conn, comparison["id"])
                continue
            # Batch steps run only when no single-company task is waiting.
            if not screening.tick(conn):
                time.sleep(2)
            continue
        log.info("run %s started", row["id"])
        (price if row["kind"] == "price" else research).execute(conn, row["id"])
        log.info("run %s finished", row["id"])


if __name__ == "__main__":
    main()
