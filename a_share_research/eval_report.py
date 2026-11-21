"""Report evaluation-run model calls and compare them with the previous report."""

import argparse
from contextlib import closing
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys

from a_share_research import db

log = logging.getLogger(__name__)
CALL_METRICS = ("calls", "call_seconds", "input", "output", "cacheRead")
TABLE_METRICS = ("calls", "run_seconds", "call_seconds", "final_seconds", "input", "output", "cacheRead")


def summarize(conn: sqlite3.Connection, run_id: int) -> dict | None:
    run = conn.execute("""
        SELECT id AS run_id, company_key AS code, status,
               unixepoch(finished_at) - unixepoch(started_at) AS run_seconds,
               (SELECT seconds FROM calls WHERE run_id = runs.id AND step = 'explore'
                ORDER BY id DESC LIMIT 1) AS final_seconds
        FROM runs WHERE id = ?
    """, (run_id,)).fetchone()
    if run is None:
        return None
    stages = conn.execute("""
        SELECT step, count(*) AS calls, coalesce(sum(seconds), 0.0) AS call_seconds,
               coalesce(sum(json_extract(usage, '$.input')), 0) AS input,
               coalesce(sum(json_extract(usage, '$.output')), 0) AS output,
               coalesce(sum(json_extract(usage, '$.cacheRead')), 0) AS cacheRead
        FROM calls WHERE run_id = ? GROUP BY step ORDER BY step
    """, (run_id,)).fetchall()
    return dict(run) | {key: sum(stage[key] for stage in stages) for key in CALL_METRICS} | {
        "steps": {stage["step"]: {"calls": stage["calls"], "seconds": stage["call_seconds"]} for stage in stages}
    }


def _cell(value, before) -> str:
    if value is None:
        return "-"
    shown = f"{value:.3f}" if isinstance(value, float) else str(value)
    if before is None:
        return shown
    change = value - before
    return f"{shown} ({change:+.3f})" if isinstance(change, float) else f"{shown} ({change:+d})"


def table(runs: list[dict], previous: dict[str, dict]) -> str:
    rows = []
    step_names = sorted({step for run in runs + list(previous.values()) for step in run["steps"]})
    rows.append(["Code", "Run", "Status", "Calls", "Run(s)", "Model(s)", "Final(s)", "Input", "Output", "CacheRead",
                 *(f"{step}(n/s)" for step in step_names)])
    for run in runs:
        prior = previous.get(run["code"], {})
        row = [run["code"], str(run["run_id"]), run["status"],
               *(_cell(run[key], prior.get(key)) for key in TABLE_METRICS)]
        for name in step_names:
            step = run["steps"].get(name, {})
            before = prior.get("steps", {}).get(name, {})
            row.append("/".join(_cell(step.get(key, 0), before.get(key, 0) if prior else None)
                                for key in ("calls", "seconds")) if step or before else "-")
        rows.append(row)
    widths = [max(map(len, column)) for column in zip(*rows)]
    return "\n".join("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip() for row in rows)


def report_runs(run_ids: list[int]) -> None:
    database = (db.DATA / "app.db").absolute()
    directory = Path(os.environ.get("EVAL_REPORT_DIR", "~/.local/share/a-share-research-evaluations/eval-reports")).expanduser()
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            with db.transaction(conn, immediate=False):
                runs = []
                for run_id in dict.fromkeys(run_ids):
                    if (run := summarize(conn, run_id)) is None:
                        log.warning("Run %s not found in %s", run_id, database)
                    else:
                        runs.append(run)
        if not runs:
            return
        directory.mkdir(parents=True, exist_ok=True)
        baseline = max(directory.glob("*.json"), default=None)
        previous = json.loads(baseline.read_text())["runs"] if baseline else []
        by_code = {run["code"]: run for run in sorted(previous, key=lambda run: run["run_id"])}
        timestamp = datetime.now(UTC)
        path = directory / timestamp.strftime("%Y%m%dT%H%M%S.%fZ.json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"generated_at": timestamp.isoformat(),
                                         "previous": baseline.name if baseline else None, "runs": runs}, indent=2) + "\n")
        temporary.replace(path)
        sys.stdout.write(table(runs, by_code) + "\n")
        log.info("Eval report %s%s", path, f" compared with {baseline.name}" if baseline else "")
    except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        log.warning("Could not write evaluation report: %s", exc)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_ids", metavar="RUN_ID", type=int, nargs="+")
    report_runs(parser.parse_args().run_ids)


if __name__ == "__main__":
    main()
