"""Derivation-time binding, atomic publishing and the read-only integrity check.

Inputs are a real cninfo PDF (工业富联 2026-045 buyback announcement) and the captured
贵州茅台 excerpts; corruption is injected by editing bytes or rows, never by mocking.
"""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from a_share_research import db, integrity, sources
from a_share_research.research import publish
from test_check import MATERIAL

PDF = Path(__file__).parent / "data" / "601138-buyback-announcement.pdf"
SHA = hashlib.sha256(PDF.read_bytes()).hexdigest()


def stored(tmp_path: Path) -> Path:
    return sources.store_pdf(tmp_path, PDF.read_bytes(), SHA)


def test_page_text_cache_is_reused_only_for_the_same_pdf_and_extractor(tmp_path):
    pdf = stored(tmp_path)
    pages = sources._pages(pdf, SHA, "-layout")
    assert "工业富联" in pages[0]
    cache = pdf.with_suffix(".layout.json")
    identity = {"pdf_sha256": SHA, "extractor": sources.extractor(), "mode": "-layout"}
    assert json.loads(cache.read_text(encoding="utf-8")) == identity | {"pages": pages}

    cache.write_text(json.dumps(identity | {"pages": ["cached"]}), encoding="utf-8")
    assert sources._pages(pdf, SHA, "-layout") == ["cached"]
    for stale in (identity | {"extractor": "pdftotext version 0.1", "pages": ["cached"]},
                  identity | {"pdf_sha256": "0" * 64, "pages": ["cached"]},
                  ["cached"]):  # the unbound list format written before this check existed
        cache.write_text(json.dumps(stale), encoding="utf-8")
        assert sources._pages(pdf, SHA, "-layout") == pages
    cache.write_text('{"pdf_sha256": "', encoding="utf-8")  # truncated by a crash mid-write
    assert sources._pages(pdf, SHA, "-layout") == pages


def test_stored_pdf_that_no_longer_matches_is_replaced_not_reused(tmp_path):
    pdf = stored(tmp_path)
    damaged = bytearray(PDF.read_bytes())
    damaged[len(damaged) // 2] ^= 0xFF
    pdf.write_bytes(bytes(damaged))
    assert sources.store_pdf(tmp_path, PDF.read_bytes(), SHA) == pdf
    assert pdf.read_bytes() == PDF.read_bytes()


def research_db(tmp_path: Path) -> tuple[sqlite3.Connection, int]:
    conn = db.connect(tmp_path / "app.db")
    run_id = db.create_run(conn, MATERIAL.code, status="running")
    snapshot = MATERIAL.snapshot()
    db.update_run(conn, run_id, material=json.dumps(snapshot, ensure_ascii=False),
                  identity=json.dumps({"material_sha256": snapshot["sha256"]}))
    return conn, run_id


def test_report_and_run_status_commit_together(tmp_path):
    conn, run_id = research_db(tmp_path)
    conn.execute("CREATE TRIGGER fail_finish BEFORE UPDATE OF status ON runs"
                 " BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        publish(conn, run_id, MATERIAL, "2025-12-31", True, "值得继续研究", {"code": MATERIAL.code})
    assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()[0] == "running"
    conn.execute("DROP TRIGGER fail_finish")
    publish(conn, run_id, MATERIAL, "2025-12-31", True, "值得继续研究", {"code": MATERIAL.code})
    assert conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()[0] == "done"


def test_integrity_check_lists_each_injected_corruption_and_writes_nothing(tmp_path):
    conn, run_id = research_db(tmp_path)
    material_sha = MATERIAL.snapshot()["sha256"]
    publish(conn, run_id, MATERIAL, "2025-12-31", True, "值得继续研究",
            {"code": MATERIAL.code, "material_sha256": material_sha})
    pdf_dir = tmp_path / "pdf"
    stored(pdf_dir)
    conn.close()
    assert integrity.check(tmp_path / "app.db", pdf_dir) == (
        [], {"runs_with_material": 1, "reports": 1, "reports_without_hash": 0, "pdfs": 1})

    conn = sqlite3.connect(tmp_path / "app.db", isolation_level=None)
    conn.execute("UPDATE reports SET body = replace(body, '600519', '600518')")
    conn.execute("UPDATE runs SET material = replace(material, '销售费用变动', '销售费用变化')")
    second = db.create_run(conn, MATERIAL.code)
    snapshot = MATERIAL.snapshot()
    conn.execute("UPDATE runs SET material = ?, identity = ? WHERE id = ?",
                 (json.dumps(snapshot, ensure_ascii=False), json.dumps({"material_sha256": "0" * 64}), second))
    conn.close()
    pdf = pdf_dir / f"{SHA}.pdf"
    pdf.write_bytes(pdf.read_bytes()[:-1])
    before = (tmp_path / "app.db").read_bytes()

    problems, counts = integrity.check(tmp_path / "app.db", pdf_dir)
    assert problems == [
        f"任务 {run_id}：材料快照内容与其哈希不符",
        f"任务 {second}：运行身份记录的材料哈希与快照不符",
        "报告 1：正文与写入时的哈希不符",
        f"PDF {SHA}.pdf：内容与文件名哈希不符（下次取用时会重新下载覆盖）",
    ]
    assert counts["runs_with_material"] == 2
    assert (tmp_path / "app.db").read_bytes() == before
    assert integrity.check(tmp_path / "app.db", pdf_dir)[0] == problems


def test_report_pointing_at_another_material_snapshot_is_listed(tmp_path):
    conn, run_id = research_db(tmp_path)
    publish(conn, run_id, MATERIAL, "2025-12-31", False, None, {"code": MATERIAL.code, "material_sha256": "f" * 64})
    conn.close()
    problems, _ = integrity.check(tmp_path / "app.db", tmp_path / "pdf")
    assert problems == [f"报告 1：引用的材料快照与任务 {run_id} 记录不符"]


def test_database_without_the_body_hash_column_is_checked_read_only(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.executescript(db.SCHEMA.replace(
        ",\n    body_sha256 TEXT  -- sha256 of body as written; NULL for reports saved before it was recorded", ""))
    conn.execute("INSERT INTO runs (id, code, created_at) VALUES (1, '600519.SH', 'now')")
    conn.execute("INSERT INTO reports (run_id, code, period, passed, body, created_at)"
                 " VALUES (1, '600519.SH', '2025-12-31', 1, '{}', 'now')")
    conn.commit()
    conn.close()
    assert integrity.check(tmp_path / "old.db", tmp_path / "none") == (
        [], {"runs_with_material": 0, "reports": 1, "reports_without_hash": 1, "pdfs": 0})
