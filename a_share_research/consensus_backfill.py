"""Resume shared report_rc coverage only while the existing worker is idle.

One missing trading day per tick, no sleeps or new service. Progress and cooldown
live in the existing disposable TuShare cache, not the application database.
"""

import json
import logging
import time
from contextlib import closing
from datetime import datetime, timedelta

from a_share_research import db, sources

log = logging.getLogger(__name__)
STATUS_KEY = "report_rc:backfill-v1"
_next_tick = 0.0


def status() -> dict:
    with closing(sources._report_rc_connect()) as conn:
        row = conn.execute("SELECT data FROM tushare_cache WHERE key=?", (STATUS_KEY,)).fetchone()
    return {"backfill": json.loads(row[0]) if row else None, "pacing": sources.report_rc_status()}


def tick() -> bool:
    """At most one upstream report request; durable cooldown survives restarts."""
    global _next_tick
    if time.monotonic() < _next_tick:
        return False
    _next_tick = time.monotonic() + sources.REPORT_RC_MIN_INTERVAL
    state = status()
    at = datetime.now(db.SHANGHAI)
    retry = datetime.fromisoformat(state["pacing"]["retry_at"])
    previous = state["backfill"] or {}
    if retry > at or (previous.get("next_check_at") and datetime.fromisoformat(previous["next_check_at"]) > at):
        return False

    as_of = (at.date() - timedelta(days=1)).isoformat()
    window = sources.report_rc_window(as_of, max_calls=1)
    pacing = sources.report_rc_status()
    # A complete window needs only an hourly check. Calendar/provider failures
    # without a report reservation back off too, rather than spinning every tick.
    if not window["missing_days"] and not window.get("reason"):
        delay = 3600
    elif not window["trading_days"]:
        delay = 300
    else:
        delay = sources.REPORT_RC_MIN_INTERVAL
    retry = max(datetime.fromisoformat(pacing["retry_at"]), at + timedelta(seconds=delay))
    snapshot = {key: value for key, value in window.items() if key != "rows"}
    complete = window["trading_days"] > 0 and not window["missing_days"] and not window["truncated_days"] and not window.get("reason")
    snapshot.update(status="complete" if complete else "partial" if window["covered_days"] else "unavailable",
                    as_of=as_of, checked_at=db.now(), next_check_at=retry.isoformat())
    with closing(sources._report_rc_connect()) as conn:
        conn.execute("INSERT OR REPLACE INTO tushare_cache VALUES (?,?,?)",
                     (STATUS_KEY, json.dumps(snapshot, ensure_ascii=False), db.now()))
    log.info("report_rc backfill: covered=%s/%s empty=%s missing=%s truncated=%s attempts=%s budget=%s/%s next=%s reason=%s",
             window["covered_days"], window["trading_days"], len(window["empty_days"]), len(window["missing_days"]),
             len(window["truncated_days"]), window.get("upstream_attempts", 0), pacing["used"], pacing["limit"],
             retry.isoformat(), window.get("reason"))
    return True


if __name__ == "__main__":
    print(json.dumps(status(), ensure_ascii=False, indent=2))
