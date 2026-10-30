"""Run queued research one at a time, then batch steps; the only process holding model and market credentials."""

import logging
import time

from a_share_research import db, price, research, screening, progress

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = db.connect()
    # A web run left 'running' by a previous worker cannot be resumed; the user can start it again.
    # Command-line runs (no request_id) belong to their own process and are left alone.
    conn.execute(
        "UPDATE runs SET status = 'failed', step = NULL, error = '研究中断（服务重启），请重新开始', finished_at = ?"
        " WHERE status = 'running' AND request_id IS NOT NULL", (db.now(),),
    )
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
