"""Derivation-time binding, atomic publishing and the read-only integrity check.

Inputs are a real cninfo PDF (工业富联 2026-045 buyback announcement) and the captured
贵州茅台 excerpts; corruption is injected by editing bytes or rows, never by mocking.
"""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from a_share_research import db, integrity, price, sources
from a_share_research.research import publish
from a_share_research.evidence import material_hash
from support.reports import MATERIAL
from support.continuation import RECORDS
from test_price import DATA as PRICES

PDF = Path(__file__).parent / "data" / "601138-buyback-announcement.pdf"
SHA = hashlib.sha256(PDF.read_bytes()).hexdigest()


def stored(tmp_path: Path) -> Path:
    return sources.store_pdf(tmp_path, PDF.read_bytes(), SHA)


def pair(pdf: Path) -> tuple:
    return tuple(sources._pages(pdf, SHA, mode) for mode in ("-layout", "-raw"))


def test_pdf_text_cache_is_reused_and_one_missing_mode_is_rebuilt(tmp_path):
    serial = stored(tmp_path / "serial")
    expected = pair(serial)
    parallel = stored(tmp_path / "second")
    assert pair(parallel) == expected
    for suffix in (".layout.json", ".raw.json"):
        assert parallel.with_suffix(suffix).read_bytes() == serial.with_suffix(suffix).read_bytes()
    stamps = [parallel.with_suffix(s).stat().st_mtime_ns for s in (".layout.json", ".raw.json")]
    assert pair(parallel) == expected
    assert stamps == [parallel.with_suffix(s).stat().st_mtime_ns for s in (".layout.json", ".raw.json")]
    parallel.with_suffix(".raw.json").unlink()
    assert pair(parallel) == expected
    assert parallel.with_suffix(".layout.json").stat().st_mtime_ns == stamps[0]


def test_page_text_cache_is_reused_only_for_the_same_pdf_and_extractor(tmp_path):
    pdf = stored(tmp_path)
    pages = sources._pages(pdf, SHA, "-layout")
    assert "实际回购金额            100,000.52万元" in pages[0]
    cache = pdf.with_suffix(".layout.json")
    identity = {"pdf_sha256": SHA, "extractor": sources.extractor(), "mode": "-layout"}
    assert json.loads(cache.read_text(encoding="utf-8")) == identity | {
        "pages_sha256": sources._pages_hash(pages), "pages": pages}

    def cached(**fields) -> dict:
        return identity | {"pages_sha256": sources._pages_hash(["cached"]), "pages": ["cached"]} | fields

    cache.write_text(json.dumps(cached()), encoding="utf-8")
    assert sources._pages(pdf, SHA, "-layout") == ["cached"]
    one_digit = [pages[0].replace("100,000.52万元", "100,000.53万元"), *pages[1:]]
    for stale in (cached(extractor="pdftotext version 0.1"), cached(pdf_sha256="0" * 64),
                  identity | {"pages_sha256": sources._pages_hash(pages), "pages": one_digit},
                  ["cached"]):  # the unbound list format written before this check existed
        cache.write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")
        assert sources._pages(pdf, SHA, "-layout") == pages
    intact = cache.read_bytes()
    for damaged in (intact[:len(intact) // 2], intact[:intact.index("回".encode()) + 1]):  # cut mid-write / mid-character
        cache.write_bytes(damaged)
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
        [], {"runs_with_material": 1, "calls": 0, "reports": 1, "reports_without_hash": 0, "pdfs": 1})

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
        [], {"runs_with_material": 0, "calls": 0, "reports": 1, "reports_without_hash": 1, "pdfs": 0})


def test_price_reference_and_run_status_commit_together(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    run_id = db.create_run(conn, "600900.SH", kind="price", name="长江电力", status="running")
    body = price.compute(PRICES["stock"], PRICES["benchmark"]) | {"as_of": "2026-10-20", "expires": "2026-10-27"}
    conn.execute("CREATE TRIGGER fail_finish BEFORE UPDATE OF status ON runs"
                 " BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        price.publish(conn, run_id, "600900.SH", "长江电力", body)
    assert conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 0
    conn.execute("DROP TRIGGER fail_finish")
    price.publish(conn, run_id, "600900.SH", "长江电力", body)
    assert conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()[0] == "done"


def test_exploration_progress_keeps_the_identity_hash_in_step(tmp_path):
    # A worker killed mid-exploration leaves the last progress snapshot; that is not corruption.
    conn, run_id = research_db(tmp_path)
    progressed = MATERIAL.snapshot()
    progressed["data"]["market_date"] = "2026-10-21"
    progressed["sha256"] = material_hash(progressed["data"])
    db.save_material(conn, run_id, progressed)
    conn.close()
    assert integrity.check(tmp_path / "app.db", tmp_path / "pdf")[0] == []


def test_wrongly_typed_json_is_listed_and_the_check_carries_on(tmp_path):
    conn, run_id = research_db(tmp_path)
    publish(conn, run_id, MATERIAL, "2025-12-31", True, "值得继续研究", {"code": MATERIAL.code})
    second = db.create_run(conn, MATERIAL.code, status="running")
    publish(conn, second, MATERIAL, "2025-12-31", True, "值得继续研究", {"code": MATERIAL.code})
    conn.execute("UPDATE reports SET body = 'null' WHERE id = 1")
    conn.execute("UPDATE runs SET identity = '[]' WHERE id = ?", (run_id,))
    conn.close()
    stored(tmp_path / "pdf")
    problems, counts = integrity.check(tmp_path / "app.db", tmp_path / "pdf")
    assert problems == [f"任务 {run_id}：运行身份无法解析", "报告 1：正文与写入时的哈希不符",
                        "报告 1：正文不是 JSON 对象"]
    assert counts["reports"] == 2 and counts["pdfs"] == 1


def test_material_save_deduplicates_full_snapshot_but_repairs_identity_and_keeps_diagnostics(tmp_path):
    from copy import deepcopy
    conn, run_id = research_db(tmp_path)
    snapshot = deepcopy(MATERIAL.snapshot())
    before = conn.total_changes
    assert db.save_material(conn, run_id, snapshot) == snapshot['sha256']
    assert conn.total_changes == before
    # A matching body cannot hide stale run identity; preserve unrelated metadata while repairing it.
    db.update_run(conn, run_id, identity=json.dumps({'material_sha256': None, 'replay_of': 12}))
    before = conn.total_changes
    db.save_material(conn, run_id, snapshot)
    assert conn.total_changes == before + 2
    identity = json.loads(conn.execute('SELECT identity FROM runs WHERE id=?', (run_id,)).fetchone()[0])
    assert identity == {'material_sha256': snapshot['sha256'], 'replay_of': 12}
    # Diagnostic changes are part of the full snapshot even when financial evidence is unchanged.
    snapshot['data']['extra'] = {'stop_reasons': [{'stage': 'repair', 'reason': 'tool_limit'}]}
    snapshot['sha256'] = material_hash(snapshot['data'])
    before = conn.total_changes
    db.save_material(conn, run_id, snapshot)
    assert conn.total_changes == before + 2
    stored = json.loads(conn.execute('SELECT material FROM runs WHERE id=?', (run_id,)).fetchone()[0])
    assert stored == json.loads(json.dumps(snapshot))
    db.save_material(conn, run_id, snapshot)
    assert conn.total_changes == before + 2
    conn.close()


@pytest.mark.parametrize("column", ["replay_of", "coverage"])
def test_derived_column_mismatch_is_reported_read_only(tmp_path, column):
    conn = db.connect(tmp_path / "app.db")
    record = next(record for record in RECORDS if record["report"]["id"] == 143)
    report = record["report"]
    run = db.create_run(conn, report["code"])
    db.update_run(conn, run, identity=json.dumps(record["run"]["identity"]))
    db.save_report(conn, run, report["code"], report["name"], report["period"], report["passed"],
                   report["verdict"], report["body"])
    assert integrity.check(tmp_path / "app.db", tmp_path / "pdf")[0] == []
    if column == "replay_of":
        conn.execute("UPDATE runs SET replay_of = NULL WHERE id = ?", (run,))
        expected = f"任务 {run}：replay_of 与运行身份 JSON 不一致"
    else:
        conn.execute("UPDATE reports SET coverage = NULL")
        expected = "报告 1：coverage 与正文重算不一致"
    conn.close()
    before = (tmp_path / "app.db").read_bytes()
    assert integrity.check(tmp_path / "app.db", tmp_path / "pdf")[0] == [expected]
    assert (tmp_path / "app.db").read_bytes() == before


def test_coverage_check_reports_all_mismatches_in_one_query(tmp_path):
    from support.continuation import load

    conn = db.connect(tmp_path / "app.db")
    load(conn)
    conn.execute("UPDATE reports SET coverage = NULL")
    statements = []
    conn.set_trace_callback(statements.append)
    problems, _ = integrity._check_rows(conn)
    assert [p for p in problems if "coverage" in p] == [
        f"报告 {r['report']['id']}：coverage 与正文重算不一致"
        for r in sorted(RECORDS, key=lambda r: r['report']['id'])]
    assert len([s for s in statements if "json_each" in s]) == 1
    conn.close()
