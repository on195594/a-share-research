"""Persisted experiments over captured production reports, without model/network doubles."""
from copy import deepcopy
import cProfile
import gzip
import json
from pathlib import Path
import sqlite3

import pytest

from a_share_research import db, evolution, reading

DATA = Path(__file__).parent / "data"
SOURCES = json.loads(gzip.decompress((DATA / "evolution-sources-20261106.json.gz").read_bytes()))["reports"]
NOTE = json.loads((DATA / "evolution-note-plan-20261106.json").read_text())
PLAN = json.loads((DATA / "evolution-plan-20261106.json").read_text())
ISSUES = json.loads((DATA / "evolution-review-issues-20261107.json").read_text())


@pytest.fixture
def stored(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    for row in SOURCES:
        conn.execute("INSERT INTO runs(id,kind,code,name,status,created_at,material,request,identity) VALUES (?,?,?,?,?,?,?,?,?)",
                     (row["run_id"], row["kind"], row["code"], row["name"], row["status"], row["created_at"], row["material"], row["request"], row["identity"]))
        conn.execute("INSERT INTO reports(id,run_id,code,name,period,passed,body,created_at,body_sha256) VALUES (?,?,?,?,?,?,?,?,?)",
                     tuple(row[key] for key in ("id", "run_id", "code", "name", "period", "passed", "body", "created_at", "body_sha256")))
        conn.execute("INSERT INTO subscriptions(code,added_at) VALUES (?,?)", (row["code"][:6], row["created_at"]))
    yield conn
    conn.close()


def plan(sources):
    # Real model-generated and independently checked plan from the v2 controller.
    value = deepcopy(PLAN["plan"])
    assert {c["report_id"] for c in value["cases"]} == {s["report_id"] for s in sources}
    return value


def registered(conn):
    run_id = evolution.discover(conn)
    row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    state = evolution._state(row)
    state["plan"] = evolution.validate_plan(plan(state["sources"]), state["sources"])
    state["plan_sha256"] = evolution.digest(state["plan"])
    state["phase"] = "cases"
    evolution._save(conn, run_id, state)
    return run_id, state


def test_source_watermark_prevents_repeated_idle_discovery_and_diagnostics_are_not_sources(stored):
    before = [r["id"] for r in reading.latest_reports(stored)]
    run_id = evolution.discover(stored)
    assert run_id is not None
    assert evolution.discover(stored) is None
    row = stored.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    assert evolution.is_controller(row) and row["status"] == "done" and row["finished_at"] is None
    assert [r["id"] for r in reading.latest_reports(stored)] == before
    assert evolution.active_methods(stored) == []
    assert evolution.item(stored, run_id)["usage"][0]["calls"] == 0


def test_existing_source_set_is_checked_before_rebuilding_material_sources(stored):
    root = evolution.discover(stored)
    sources = evolution.item(stored, root)["sources"]
    expected = evolution.digest({"version": evolution.VERSION,
                                 "sources": [(s["report_id"], s["report_sha256"]) for s in sources]})
    assert stored.execute("SELECT request_id FROM runs WHERE id=?", (root,)).fetchone()[0] == "evolution:" + expected
    profile = cProfile.Profile()
    assert profile.runcall(evolution.discover, stored) is None
    assert evolution._source.__code__ not in [entry.code for entry in profile.getstats()]


def test_idle_cache_skips_report_reads_and_tick_keeps_its_existing_receipt(stored):
    root = evolution.discover(stored)
    state = evolution.item(stored, root)
    evolution._save(stored, root, state | {"phase": "deferred"})
    cache = evolution.DiscoveryCache()
    assert evolution.tick(stored, discovery=cache) is False
    queries = []
    stored.set_trace_callback(queries.append)
    try:
        assert evolution.tick(stored, discovery=cache) is False
    finally:
        stored.set_trace_callback(None)
    assert queries and any("data_version" in sql for sql in queries)
    assert not any("FROM reports" in sql or "u.material" in sql for sql in queries)
    assert stored.execute(f"SELECT count(*) FROM runs WHERE {evolution.ROOT}").fetchone()[0] == 1


@pytest.mark.parametrize("external", [False, True], ids=["same-connection", "external-connection"])
@pytest.mark.parametrize("legacy_hash", [False, True], ids=["stored-hash", "missing-legacy-hash"])
def test_idle_cache_detects_body_changes_even_without_updating_saved_hash(stored, external, legacy_hash):
    # Explicitly marked offline mutation of a captured report; never a research/return sample.
    if legacy_hash:
        stored.execute("UPDATE reports SET body_sha256=NULL")
    root = evolution.discover(stored)
    cache = evolution.DiscoveryCache()
    assert cache.discover(stored) is None
    body = json.loads(SOURCES[0]["body"])
    body["summary"] = "离线缓存互变测试：" + body["summary"]
    writer = stored
    if external:
        path = stored.execute("PRAGMA database_list").fetchone()[2]
        writer = sqlite3.connect(path, isolation_level=None)
    try:
        writer.execute("UPDATE reports SET body=? WHERE id=?", (json.dumps(body, ensure_ascii=False), SOURCES[0]["id"]))
    finally:
        if external:
            writer.close()
    new_root = cache.discover(stored)
    assert new_root is not None and new_root != root
    assert cache.discover(stored) is None
    assert stored.execute("SELECT count(*) FROM calls").fetchone()[0] == 0


def test_idle_cache_detects_version_and_subscription_changes(stored, monkeypatch):
    root = evolution.discover(stored)
    cache = evolution.DiscoveryCache()
    assert cache.discover(stored) is None
    monkeypatch.setattr(evolution, "VERSION", evolution.VERSION + "-offline-cache-test")
    assert cache.discover(stored) not in (None, root)
    stored.execute("DELETE FROM subscriptions WHERE company_key=?", (SOURCES[0]["code"][:6],))
    assert cache.discover(stored) is None  # Two remaining companies cannot open an experiment.


def test_idle_cache_does_not_publish_transactional_or_failed_reads(stored):
    root = evolution.discover(stored)
    cache = evolution.DiscoveryCache()
    assert cache.discover(stored) is None
    stored.execute("BEGIN")
    stored.execute("DELETE FROM subscriptions")
    assert cache.discover(stored) is None
    stored.execute("ROLLBACK")
    # An authorizer rejects actual report reads, rather than replacing discovery with a mock.
    def deny_reports(action, table, column, database, trigger):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == "reports" else sqlite3.SQLITE_OK
    stored.set_authorizer(deny_reports)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="prohibited|authorized"):
            cache.discover(stored)
    finally:
        stored.set_authorizer(None)
    assert cache.discover(stored) is None
    assert stored.execute(f"SELECT id FROM runs WHERE {evolution.ROOT}").fetchone()[0] == root


def test_eligible_loads_only_latest_formal_report_for_each_current_company(stored):
    captured = SOURCES[0]
    # Same real capture in a newer report slot solely to exercise latest/formal selection.
    run = db.create_run(stored, captured["code"], request_id="offline-newer-report")
    db.update_run(stored, run, status="done", material=captured["material"])
    db.save_report(stored, run, captured["code"], captured["name"], captured["period"], True,
                   None, json.loads(captured["body"]))
    latest = stored.execute("SELECT id FROM reports WHERE run_id=?", (run,)).fetchone()[0]
    ids = [r["id"] for r in evolution._eligible(stored)]
    assert ids == sorted([latest, *[s["id"] for s in SOURCES[1:]]], reverse=True)
    db.update_run(stored, run, status="failed")
    assert [r["id"] for r in evolution._eligible(stored)] == sorted([s["id"] for s in SOURCES], reverse=True)


def test_discovery_version_change_is_one_source_set_not_one_task_per_report(stored, monkeypatch):
    root = evolution.discover(stored)
    assert root is not None
    row = stored.execute("SELECT identity FROM runs WHERE id=?", (root,)).fetchone()
    state = evolution._state(row)
    state.update(phase="deferred", reason="离线协议变体：保留旧来源的暂缓结果")
    evolution._save(stored, root, state)
    # A historical application identity does not invalidate the source-set receipt.
    identity = json.loads(stored.execute("SELECT identity FROM runs WHERE id=?", (root,)).fetchone()[0])
    identity["application_sha256"] = json.loads(SOURCES[0]["identity"])["application_sha256"]
    db.update_run(stored, root, identity=json.dumps(identity))
    assert evolution.discover(stored) is None

    # Explicit experiment-version mutation, not a release or a new real experiment.
    monkeypatch.setattr(evolution, "VERSION", evolution.VERSION + "-offline-test")
    new_root = evolution.discover(stored)
    assert new_root is not None and new_root != root
    new_state = evolution.item(stored, new_root)
    assert new_state is not None
    assert new_state["sources"] == state["sources"]
    assert evolution.discover(stored) is None
    assert stored.execute(f"SELECT COUNT(*) FROM runs WHERE {evolution.ROOT}").fetchone()[0] == 2
    assert stored.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    previous = evolution.item(stored, root)
    assert previous is not None and previous["phase"] == "deferred"
    assert evolution.active_methods(stored) == []


def test_plan_rejects_invented_report_and_missing_original_gap(stored):
    run_id = evolution.discover(stored)
    state = evolution.item(stored, run_id)
    good = plan(state["sources"])
    assert evolution.validate_plan(good, state["sources"]) == good
    bad = deepcopy(good)
    bad["cases"][0]["report_id"] = -1
    with pytest.raises(ValueError, match="真实报告"):
        evolution.validate_plan(bad, state["sources"])
    bad = deepcopy(good)
    next(c for c in bad["cases"] if c["role"] == "affected")["gap_indices"] = [99999]
    with pytest.raises(ValueError, match="真实缺口"):
        evolution.validate_plan(bad, state["sources"])


def attach_review_issues(conn):
    """Marked variant: real audit records from reports 63/65 attached to two captured reports."""
    targets = {}
    for row, captured in zip(SOURCES, ISSUES["reports"]):
        body = json.loads(row["body"])
        body["review_audit"] = {"version": 1, "issues": captured["issues"], "rounds": []}
        conn.execute("UPDATE reports SET body=? WHERE id=?", (json.dumps(body, ensure_ascii=False), row["id"]))
        targets[row["id"]] = captured["issues"][0]["id"]
    # Marked variant: older reports saved no review record at all.
    body = json.loads(SOURCES[2]["body"])
    body.pop("review_audit", None)
    conn.execute("UPDATE reports SET body=? WHERE id=?", (json.dumps(body, ensure_ascii=False), SOURCES[2]["id"]))
    return targets


def test_review_issues_reach_plan_and_reader_and_can_anchor_affected_cases(stored):
    targets = attach_review_issues(stored)
    state = evolution.item(stored, evolution.discover(stored))
    by_id = {s["report_id"]: s for s in state["sources"]}
    report_id, issue_id = next(iter(targets.items()))
    captured = ISSUES["reports"][0]["issues"][0]
    assert by_id[report_id]["review_issues"][0]["problem"] == captured["problem"]
    assert "evidence" not in by_id[report_id]["review_issues"][0]
    assert captured["problem"] in evolution._plan_prompt(state)
    overview = evolution.read_saved_evidence(stored, state["sources"], report_id)
    assert overview["review_issues"][0]["evidence"] == captured["evidence"]
    # A report without a review record stays distinct from one whose review found nothing.
    assert by_id[SOURCES[2]["id"]]["review_issues"] is None
    assert evolution.read_saved_evidence(stored, state["sources"], SOURCES[2]["id"])["review_issues"] is None
    assert evolution._review_issues({"review_audit": {"version": 1, "issues": [], "rounds": []}}) == []

    good = plan(state["sources"])
    affected = next(c for c in good["cases"] if c["role"] == "affected" and c["report_id"] == report_id)
    affected["gap_indices"] = []
    affected["review_issue_ids"] = [issue_id]
    assert evolution.validate_plan(good, state["sources"]) == good
    for invalid in (["issue-invented"], [issue_id, 1], issue_id):
        affected["review_issue_ids"] = invalid
        with pytest.raises(ValueError, match="复核问题编号"):
            evolution.validate_plan(good, state["sources"])
    affected["review_issue_ids"] = []
    with pytest.raises(ValueError, match="真实缺口下标或复核问题编号"):
        evolution.validate_plan(good, state["sources"])
    # An id from another report does not belong to this case.
    affected["review_issue_ids"] = [issue_id]
    next(c for c in good["cases"] if c["role"] == "normal")["review_issue_ids"] = [issue_id]
    with pytest.raises(ValueError, match="复核问题编号"):
        evolution.validate_plan(good, state["sources"])


def test_cases_keep_frozen_official_start_and_only_candidate_gets_method(stored):
    root, state = registered(stored)
    children = [evolution._enqueue_case(stored, root, state) for _ in range(2)]
    assert len(set(children)) == 2
    requests = [json.loads(stored.execute("SELECT request FROM runs WHERE id=?", (child,)).fetchone()[0]) for child in children]
    baseline, candidate = [evolution.prepare_material(stored, request) for request in requests]
    assert baseline.extra["evolution_start_sha256"] == candidate.extra["evolution_start_sha256"]
    assert baseline.source_registry() == candidate.source_registry()
    assert baseline.table == candidate.table
    assert "experimental_method" not in baseline.extra
    assert candidate.extra["experimental_method"]["text"] == PLAN["plan"]["method"]
    assert candidate.extra["question"] == baseline.extra["question"]
    assert baseline.extra["question"] == requests[0]["question"]
    assert not baseline.extra.get("tools") and not candidate.extra.get("tools")
    assert evolution.tick(stored) is False  # Children await worker; no new model stage.
    assert evolution.active_methods(stored) == []
    changed = deepcopy(requests[0])
    changed["source"]["baseline_run_id"] = -1
    with pytest.raises(ValueError, match="冻结案例"):
        evolution.prepare_material(stored, changed)


def test_unmet_frozen_criteria_cannot_enter_active_methods(stored):
    root, state = registered(stored)
    # Protocol mutation: unfulfilled criteria never count as method adoption.
    reviews = [{"criterion_met": False, "no_regression": False, "grounded": False, "method_used": True}
               for _ in state["plan"]["cases"]]
    assert evolution.decision(state["plan"], reviews) == "rejected"
    state.update(phase="rejected", reviews=reviews)
    evolution._save(stored, root, state)
    assert evolution.active_methods(stored) == []
    assert evolution.item(stored, root)["finished_at"] is not None
    before = stored.total_changes
    evolution.execute(stored, root)  # A terminal old root cannot resample after a code/parser change.
    assert stored.total_changes == before
    assert stored.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    with pytest.raises(ValueError, match="全部事前案例"):
        evolution.decision(state["plan"], reviews[:1])


def test_mutation_adoption_requires_all_gates_and_respects_cutoff(stored):
    root, state = registered(stored)
    # Explicit protocol mutation of the real plan, never a claim of real method success.
    reviews = [{"criterion_met": True, "no_regression": True, "grounded": True, "method_used": True}
               for _ in state["plan"]["cases"]]
    assert evolution.decision(state["plan"], reviews) == "adopted"
    for gate in ("criterion_met", "no_regression", "grounded", "method_used"):
        changed = deepcopy(reviews)
        index = next(i for i, c in enumerate(state["plan"]["cases"]) if c["role"] == "affected")
        changed[index][gate] = False
        assert evolution.decision(state["plan"], changed) == "rejected"
    adopted_at = SOURCES[0]["created_at"]
    state.update(phase="adopted", reviews=reviews, adopted_at=adopted_at)
    evolution._save(stored, root, state)
    assert evolution.active_methods(stored, before="2000-01-01") == []
    assert evolution.active_methods(stored, before=adopted_at)[0]["evolution_id"] == root


def test_archived_rejection_is_idempotent_history_and_never_adopted(stored):
    for row in NOTE["diagnostic_runs"]:
        keys = tuple(row)
        stored.execute(f"INSERT INTO runs({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", tuple(row.values()))
    args = dict(title="半年报附注基线", method=NOTE["method"], reason=NOTE["results"],
                source_sha256=NOTE["results_sha256"], source_label=NOTE["source"] + "/results.md",
                case_run_ids=[row["id"] for row in NOTE["diagnostic_runs"]])
    archived = evolution.record_archived_rejection(stored, **args)
    assert evolution.record_archived_rejection(stored, **args) == archived
    assert evolution.item(stored, archived)["phase"] == "rejected"
    assert evolution.item(stored, archived)["archived"] is True
    from support.templates import ENV
    detail = evolution.item(stored, archived)
    html = ENV.get_template('improvements.html').render(item=detail, items=[])
    assert '分组：对照' in html and '研究问题</summary>' in html
    assert NOTE['diagnostic_runs'][0]['code'] in html
    assert 'E2' in html and '缺口变窄' in html and '<th scope="col">判据</th>' in html
    assert '未保存逐家公司结构化判据' in html
    # Older captured rows hold JSON null, not SQL NULL, in request.
    variant = NOTE['diagnostic_runs'][0]['id']
    saved = stored.execute('SELECT request FROM runs WHERE id=?', (variant,)).fetchone()[0]
    stored.execute("UPDATE runs SET request='null' WHERE id=?", (variant,))
    assert evolution.item(stored, archived)['archived_runs'][0]['source'] == {}
    stored.execute('UPDATE runs SET request=? WHERE id=?', (saved, variant))
    assert evolution.active_methods(stored) == []
    root = evolution.discover(stored)
    history = evolution.item(stored, root)["history"]
    assert history[0]["id"] == archived and history[0]["phase"] == "rejected"
    invalid = args | {"source_sha256": "0" * 64, "case_run_ids": [SOURCES[0]["run_id"]]}
    with pytest.raises(ValueError, match="诊断任务"):
        evolution.record_archived_rejection(stored, **invalid)


def test_planner_reads_only_frozen_official_pages_and_original_table(stored):
    root = evolution.discover(stored)
    state = evolution.item(stored, root)
    source = state["sources"][0]
    before = stored.total_changes
    overview = evolution.read_saved_evidence(stored, state["sources"], source["report_id"])
    assert overview["report"]["table"]
    key, entry = next((key, value) for key, value in overview["sources"].items() if value["read_pages"])
    page = entry["read_pages"][0]
    result = evolution.read_saved_evidence(stored, state["sources"], source["report_id"], source=key, page=page, count=1)
    assert result["matches"][0]["source"] == key and result["matches"][0]["page"] == page
    material = evolution.replay_material(stored, source["run_id"])
    assert result["matches"][0]["text"] == material.source_registry()[key]["pages"].get(page, "")
    assert stored.total_changes == before
    with pytest.raises(ValueError, match="冻结的报告"):
        evolution.read_saved_evidence(stored, state["sources"], -1)
    with pytest.raises(ValueError, match="来源不存在"):
        evolution.read_saved_evidence(stored, state["sources"], source["report_id"], source="D99999")


def test_failed_controller_cannot_restart_after_worker_recovery_limit(stored):
    root, state = registered(stored)
    db.update_run(stored, root, status="failed", error="研究中断（已自动恢复仍未完成，请重新开始）")
    assert evolution.tick(stored) is True
    view = evolution.item(stored, root)
    assert view["phase"] == "deferred" and view["interrupted_phase"] == "cases"
    assert view["status"] == "failed" and view["finished_at"]
    assert evolution.tick(stored) is False


def test_application_change_stops_frozen_cases_before_any_model_call(stored):
    from a_share_research import model
    root, state = registered(stored)
    old = json.loads(SOURCES[0]["identity"])["application_sha256"]
    assert old != model.application_identity()["application_sha256"]
    state["case_application_sha256"] = old
    evolution._save(stored, root, state)
    evolution.execute(stored, root)
    assert evolution.item(stored, root)["phase"] == "deferred"
    assert stored.execute("SELECT COUNT(*) FROM calls WHERE run_id=?", (root,)).fetchone()[0] == 0
    assert evolution.active_methods(stored) == []


def test_candidate_and_adopted_method_use_the_same_prompt_carrier(stored):
    from a_share_research.research_tools import method_entry
    root, state = registered(stored)
    children = [evolution._enqueue_case(stored, root, state) for _ in range(2)]
    requests = [json.loads(stored.execute("SELECT request FROM runs WHERE id=?", (child,)).fetchone()[0]) for child in children]
    baseline, candidate = [evolution.prepare_material(stored, request) for request in requests]
    method = candidate.extra["experimental_method"]
    candidate.extra["method_catalog"] = [method_entry(method)]
    adopted = deepcopy(baseline)
    adopted.extra["method_catalog"] = [method_entry(method | {"method_id": "evolution:" + str(root), "adopted_at": state["created_at"]})]
    assert candidate.prompt_block() == adopted.prompt_block()
    assert method_entry(method)["method"]["text"] == state["plan"]["method"]
    assert state["plan"]["scope"] not in candidate.prompt_block()


REVIEW = json.loads(gzip.decompress((DATA / "evolution-review-path-20261106.json.gz").read_bytes()))
REVIEW_REPORTS = {arm: entry["body"] for arm, entry in REVIEW["reports"].items()}


def test_actual_review_nested_path_and_mutated_evidence():
    result = evolution.validate_review_evidence(REVIEW["response"], REVIEW_REPORTS)
    assert result["no_regression"] is False
    ref = next(r for r in result["evidence"] if r["location"] == "valuation.rationale[0]")
    body = REVIEW_REPORTS[ref["arm"]]
    assert ref["quote"] in evolution._lookup(body, ref["location"])
    assert evolution._lookup(body, ref["location"] + ".text") == evolution._lookup(body, ref["location"])
    for location in ("valuation.rationale[999]", "valuation.rationale[-1]", "valuation.rationale[00]",
                     "valuation.rationale[0].__class__", "valuation.rationale[0+0]", "valuation.rationale[0];exit()"):
        assert evolution._lookup(body, location) == ""
    wrong_location = deepcopy(result)
    wrong_location["evidence"][-1]["location"] = "financial[0]"
    with pytest.raises(ValueError, match="逐字原文"):
        evolution.validate_review_evidence(wrong_location, REVIEW_REPORTS)
    wrong_quote = deepcopy(result)
    wrong_quote["evidence"][-1]["quote"] += "（此处为互变测试追加的非原文）"
    with pytest.raises(ValueError, match="逐字原文"):
        evolution.validate_review_evidence(wrong_quote, REVIEW_REPORTS)


def test_actual_review_table_and_valuation_paths_and_mutated_evidence():
    captured = json.loads(gzip.decompress((DATA / "evolution-review-table-path-20261107.json.gz").read_bytes()))
    response, reports = captured["response"], captured["reports"]
    # Production root 179 stopped when this legitimate response failed location validation.
    assert evolution.validate_review_evidence(response, reports)["no_regression"] is False
    assert response["method_used"] is True and response["criterion_met"] is False
    # One saved failed judgment is sufficient to reject, even if other cases passed.
    reviews = [{"criterion_met": True, "no_regression": True, "grounded": True, "method_used": True}
               for _ in PLAN["plan"]["cases"]]
    reviews[0] = response
    assert evolution.decision(PLAN["plan"], reviews) == "rejected"
    table = reports["candidate"]
    assert evolution._lookup(table, "table[id=C.37].calculation.requested_label").startswith("聚乙烯三季度")
    assert evolution._lookup(table, "table[id=C.36]") != evolution._lookup(table, "table[id=C.37]")
    for location in ("table[id=C.99999]", "table[id=C.37].__class__", "table[id=]", "table[id=C.37;exit()]",
                     "answer[id=C.37]x", "valuation_calc[id=C.37]"):
        assert evolution._lookup(table, location) == ""
    moved = deepcopy(response)
    ref = next(r for r in moved["evidence"] if "C.37" in r["location"])
    ref["location"] = "table[id=C.36].calculation.requested_label"
    with pytest.raises(ValueError, match="逐字原文"):
        evolution.validate_review_evidence(moved, reports)
    altered = deepcopy(response)
    next(r for r in altered["evidence"] if "C.37" in r["location"])["quote"] += "（互变测试追加）"
    with pytest.raises(ValueError, match="逐字原文"):
        evolution.validate_review_evidence(altered, reports)


def test_review_prompt_carries_frozen_original_and_inherited_calculations(stored):
    state = evolution.item(stored, evolution.discover(stored))
    assert state is not None
    state["plan"] = plan(state["sources"])
    case = state["plan"]["cases"][0]
    before = stored.total_changes
    prompt = evolution._review_prompt(stored, state, 0, [])
    payload = json.loads(prompt[prompt.index("\n{") + 1:])
    captured = next(r for r in SOURCES if r["id"] == case["report_id"])
    body = json.loads(captured["body"])
    calculations = [r for r in json.loads(captured["material"])["data"]["table"] if r.get("calculation")]
    assert calculations and payload["starting_calculations"] == calculations
    assert payload["source_report"]["answer"] == body["answer"]
    assert "table" not in payload["source_report"]  # Do not resend full industry replay data.
    assert payload["source_review_issues"] == evolution._review_issues(body)
    assert "baseline 已有同等答案时，criterion_met=false" in prompt
    assert "复用本身不是增量" in prompt
    assert "正常案例（role=normal）按冻结的无回退判据决定 criterion_met" in prompt
    assert "不强求新增推导或 method_used=true" in prompt
    assert "不证明首轮遗漏被避免" in prompt
    assert stored.total_changes == before
    assert stored.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    stored.execute("UPDATE reports SET body=? WHERE id=?", (json.dumps(body | {"summary": "互变"}, ensure_ascii=False), case["report_id"]))
    with pytest.raises(ValueError, match="冻结摘要"):
        evolution._review_prompt(stored, state, 0, [])
    with pytest.raises(ValueError, match="冻结摘要"):
        evolution._plan_review_prompt(stored, state)
    stored.execute("UPDATE reports SET body=? WHERE id=?", (captured["body"], case["report_id"]))
    # Explicit frozen-material mismatch; never silently compare a different starting table.
    next(s for s in state["sources"] if s["report_id"] == case["report_id"])["material_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="材料与冻结摘要"):
        evolution._review_prompt(stored, state, 0, [])
    with pytest.raises(ValueError, match="材料与冻结摘要"):
        evolution._plan_review_prompt(stored, state)


def test_plan_review_receives_selected_frozen_references_without_tool_calls(stored):
    state = evolution.item(stored, evolution.discover(stored))
    assert state is not None
    state["plan"] = plan(state["sources"])
    before = stored.total_changes
    prompt = evolution._plan_review_prompt(stored, state)
    payload = json.loads(prompt[prompt.index("\n{") + 1:])
    refs = payload["case_references"]
    assert [s["report_id"] for s in payload["sources"]] == [c["report_id"] for c in state["plan"]["cases"]]
    assert [(r["report_id"], r["role"]) for r in refs] == [
        (c["report_id"], c["role"]) for c in state["plan"]["cases"]]
    for index, ref in enumerate(refs):
        captured = next(r for r in SOURCES if r["id"] == ref["report_id"])
        body = json.loads(captured["body"])
        rows = json.loads(captured["material"])["data"]["table"]
        assert ref["source_report"]["answer"] == body["answer"]
        assert "table" not in ref["source_report"] and "series" not in ref["source_report"]
        assert ref["starting_calculations"] == [r for r in rows if r.get("calculation")]
        assert ref["source_review_issues"] == evolution._review_issues(body)
        case_prompt = evolution._review_prompt(stored, state, index, [])
        case_payload = json.loads(case_prompt[case_prompt.index("\n{") + 1:])
        for key in ("source_report", "starting_calculations", "source_review_issues"):
            assert case_payload[key] == ref[key]
    assert "仅对受影响案例（role=affected）" in prompt
    assert "正常案例（role=normal）仅核对冻结的无回退判据" in prompt
    assert "不以工具调用次数代替核验" in prompt
    assert "提供了材料不等于已经核验" in prompt
    assert "approved" not in payload  # The evidence projection does not make the model's decision.
    assert stored.total_changes == before
    assert stored.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0


@pytest.mark.parametrize("failed_gate", [None, "criterion_met", "no_regression", "grounded"])
def test_normal_case_requires_its_own_criteria_not_novelty_or_method_use(failed_gate):
    # Explicit protocol variants of a saved review, not observed model outcomes.
    reviews = [deepcopy(REVIEW["response"]) for _ in PLAN["plan"]["cases"]]
    for case, review in zip(PLAN["plan"]["cases"], reviews):
        review.update(criterion_met=True, no_regression=True, grounded=True,
                      method_used=case["role"] == "affected")
        if case["role"] == "normal" and failed_gate is not None:
            review[failed_gate] = False
    assert evolution.decision(PLAN["plan"], reviews) == ("adopted" if failed_gate is None else "rejected")


def test_resolved_review_repairs_are_visible_for_attribution_not_new_contributions(stored):
    targets = attach_review_issues(stored)  # Explicit association variant of saved issues, not a new experiment.
    state = evolution.item(stored, evolution.discover(stored))
    assert state is not None
    state["plan"] = plan(state["sources"])
    plan_prompt = evolution._plan_review_prompt(stored, state)
    plan_payload = json.loads(plan_prompt[plan_prompt.index("\n{") + 1:])
    references = {r["report_id"]: r for r in plan_payload["case_references"]}
    for index, case in enumerate(state["plan"]["cases"]):
        if case["report_id"] not in targets:
            continue
        prompt = evolution._review_prompt(stored, state, index, [])
        payload = json.loads(prompt[prompt.index("\n{") + 1:])
        issue = payload["source_review_issues"][0]
        captured = next(r["issues"][0] for r in ISSUES["reports"] if r["issues"][0]["id"] == targets[case["report_id"]])
        assert issue == captured and issue["status"] == "resolved"
        assert references[case["report_id"]]["source_review_issues"] == [captured]
        assert issue["resolution"]["evidence"]
        assert "原复核补入" in prompt
        assert "已有计算答案或复核补正" in prompt
    assert "baseline 已给出同等答案则不通过" in evolution._plan_prompt(state)
    assert "缺少可核验的补正前输入" in evolution._plan_prompt(state)
    assert stored.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0


def test_saved_review_requires_exact_prompt_and_preserves_failed_judgment(stored):
    root, state = registered(stored)
    call_id = db.log_call(stored, root, "evolution_case_review", REVIEW["prompt"],
                          response=json.dumps(REVIEW["response"], ensure_ascii=False))
    count = stored.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    result, reused = evolution._saved_review(stored, root, REVIEW["prompt"])
    assert reused == call_id and result["no_regression"] is False
    assert evolution._saved_review(stored, root, REVIEW["prompt"] + "\n") == (None, None)
    assert stored.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == count
    assert evolution.decision(state["plan"], [result] * 3, adoption_blocked=True) == "rejected"
    # Explicit protocol mutation: even all passing reviews cannot adopt across versions.
    passing = result | {"no_regression": True}
    assert evolution.decision(state["plan"], [passing] * 3, adoption_blocked=True) == "deferred"


def test_actual_large_review_projection_preserves_verified_values_and_rejects_mutation():
    captured = json.loads(gzip.decompress((DATA / "evolution-large-review-20261106.json.gz").read_bytes()))
    for body in captured["reports"].values():
        original = deepcopy(body)
        projected = evolution.review_report(body)
        assert body == original
        assert "series" not in projected
        assert len(json.dumps(projected, ensure_ascii=False).encode()) < len(json.dumps(body, ensure_ascii=False).encode()) / 4
        for old, new in zip(body["table"], projected["table"]):
            assert {k: v for k, v in old.items() if k != "source"} == {k: v for k, v in new.items() if k != "source"}
            if old.get("source", {}).get("api") == "industry_constituents":
                assert new["source"] == {k: v for k, v in old["source"].items() if k not in ("dataset", "eligible", "sessions", "members")}
            else:
                assert old == new
        damaged = deepcopy(body)
        row = next(r for r in damaged["table"] if r.get("source", {}).get("api") == "industry_constituents")
        period = next(iter(row["values"]))
        row["values"][period] = str(float(row["values"][period]) + 1)
        with pytest.raises(ValueError, match="复算不符"):
            evolution.review_report(damaged)


def test_web_summaries_match_full_experiments_without_loading_children(stored):
    root, _ = registered(stored)
    before = stored.total_changes
    full = evolution.item(stored, root)
    statements = []
    stored.set_trace_callback(statements.append)
    summaries = evolution.list_summaries(stored, limit=1)
    stored.set_trace_callback(None)
    assert len(summaries) == 1
    for key in ('id', 'created_at', 'phase', 'plan', 'reason'):
        assert summaries[0][key] == full.get(key)
    assert not {'usage', 'cases', 'archived_runs'} & summaries[0].keys()
    assert len(statements) == 1 and 'FROM calls' not in statements[0]
    assert stored.total_changes == before
