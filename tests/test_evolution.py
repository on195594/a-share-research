"""Persisted experiments over captured production reports, without model/network doubles."""
from copy import deepcopy
import gzip
import json
from pathlib import Path

import pytest

from a_share_research import db, evolution, reading

DATA = Path(__file__).parent / "data"
SOURCES = json.loads(gzip.decompress((DATA / "evolution-sources-20261106.json.gz").read_bytes()))["reports"]
NOTE = json.loads((DATA / "evolution-note-plan-20261106.json").read_text())
PLAN = json.loads((DATA / "evolution-plan-20261106.json").read_text())


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
