"""Captured production/K2 reports and their database loader."""
import json
from pathlib import Path

from a_share_research import db

ROOT = Path(__file__).resolve().parents[2]
RECORDS = json.loads((ROOT / "tests/data/continuation-records.json").read_text())["records"]
BODY = {r["report"]["id"]: r["report"]["body"] for r in RECORDS}


def load(conn) -> None:
    for record in RECORDS:
        run, rep = record["run"], record["report"]
        conn.execute("INSERT INTO runs(id,request_id,kind,code,status,created_at,identity,request,replay_of) VALUES (?,?,?,?,?,?,?,?,?)",
                     (run["id"], run["request_id"], run["kind"], rep["code"], run["status"], rep["created_at"],
                      json.dumps(run["identity"]) if run["identity"] else None, json.dumps(run["request"], ensure_ascii=False),
                      (run["identity"] or {}).get("replay_of")))
        conn.execute("INSERT INTO reports(id,run_id,code,name,period,passed,verdict,body,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (rep["id"], run["id"], rep["code"], rep["name"], rep["period"], rep["passed"], rep["verdict"],
                      json.dumps(rep["body"], ensure_ascii=False), rep["created_at"]))
        conn.execute(f"UPDATE reports AS r SET coverage = {db.COVERAGE} WHERE id = ?", (rep["id"],))

