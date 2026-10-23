"""Run queued research one at a time; the only process holding model and market credentials."""

import logging
import time

from a_share_research import db, price, research

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
    log.info("worker ready")
    while True:
        row = conn.execute("SELECT id, kind FROM runs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
        if row is None:
            time.sleep(2)
            continue
        log.info("run %s started", row["id"])
        (price if row["kind"] == "price" else research).execute(conn, row["id"])
        log.info("run %s finished", row["id"])


if __name__ == "__main__":
    main()
