"""Captured announcement and research inputs for integrity and backup tests."""

import hashlib
import json
import sqlite3
from pathlib import Path

from a_share_research import db
from support.reports import MATERIAL

PDF = Path(__file__).resolve().parents[1] / "data" / "601138-buyback-announcement.pdf"
SHA = hashlib.sha256(PDF.read_bytes()).hexdigest()


def research_db(tmp_path: Path) -> tuple[sqlite3.Connection, int]:
    conn = db.connect(tmp_path / "app.db")
    run_id = db.create_run(conn, MATERIAL.code, status="running")
    snapshot = MATERIAL.snapshot()
    db.update_run(conn, run_id, material=json.dumps(snapshot, ensure_ascii=False),
                  identity=json.dumps({"material_sha256": snapshot["sha256"]}))
    return conn, run_id


def database_rows(path):
    with sqlite3.connect(path) as conn:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        result = {table: sorted(conn.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr) for table in tables}
        result['__user_version__'] = conn.execute('PRAGMA user_version').fetchone()[0]
        return result
