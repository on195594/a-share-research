import json
from pathlib import Path
import sqlite3
import sys
from contextlib import closing

import pytest

from a_share_research import db, eval_report


def test_report_compares_real_calls_by_company(tmp_path, monkeypatch, capsys):
    capture = Path(__file__).with_name("data") / "eval-calls-20261119.sql"
    with closing(sqlite3.connect(tmp_path / "app.db")) as conn:
        conn.executescript(capture.read_text())
    directory = tmp_path / "reports"
    monkeypatch.setattr(db, "DATA", tmp_path)
    monkeypatch.setenv("EVAL_REPORT_DIR", str(directory))
    monkeypatch.setattr(sys, "argv", ["eval_report", "245", "243"])
    eval_report.main()
    capsys.readouterr()

    monkeypatch.setattr(sys, "argv", ["eval_report", "246", "245"])
    eval_report.main()
    text = capsys.readouterr().out
    older, newer = sorted(directory.glob("*.json"))
    report = json.loads(newer.read_text())
    assert report["previous"] == older.name
    run, other = report["runs"]
    assert (run["code"], run["run_id"], run["status"], run["run_seconds"]) == ("002648", 246, "done", 227)
    assert (run["calls"], run["input"], run["output"], run["cacheRead"]) == (8, 104332, 7932, 219904)
    assert run["call_seconds"] == pytest.approx(214.341, abs=0.001)
    assert run["final_seconds"] == pytest.approx(126.724, abs=0.001)
    assert run["steps"] == {"explore": {"calls": 7, "seconds": pytest.approx(176.094, abs=0.001)},
                            "review": {"calls": 1, "seconds": pytest.approx(38.247, abs=0.001)}}
    assert other["steps"]["repair"]["calls"] == 5
    assert "8 (+1)" in text and "7 (+1)/176.094 (-17.691)" in text
    assert "104332 (+3022)" in text and "7932 (-1021)" in text and "219904 (+29184)" in text
