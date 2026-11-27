"""Research MCP over the wire, using the unmodified production snapshot for 600519 run 7."""

import asyncio
import gzip
import hashlib
import json
from contextlib import asynccontextmanager, closing
from pathlib import Path

import pytest
from mcp import ClientSession, types
from mcp.shared.exceptions import MCPError
from mcp.shared.memory import create_client_server_memory_streams

from a_share_research import db
from a_share_research.drafts import program_valuation
from a_share_research.evidence import EvidenceError
from a_share_research.material import Material, replay_material
from a_share_research.research_mcp import AcquisitionStop, ModelError, ResearchSession, repair_prompt
from a_share_research.reviewing import ReviewLedger, digest
from support.reports import report
from support.valuation import STORED, with_probabilities

pytestmark = pytest.mark.anyio

# From prepare-4881924-20261117-180142/original/app.db. The v1 snapshot contains
# the annual report pages and TuShare rows only, without requests or account data.
SNAPSHOT = Path(__file__).parent / "data/600519-frozen-run-7.json.gz"
FROZEN_HASH = "eb89b09535970a12e43268794245a7290ed6cc765546fd7043cd2111e7856aa9"
EVIDENCE_TOOLS = {
    "industry_trend", "macro", "commodities", "commodity", "research_methods", "announcements",
    "peers", "disclosures", "read_disclosure", "financials", "valuation", "calculate",
}
CONTROL_TOOLS = {"submit_draft", "submit_patch", "review_decide", "record_call"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def conn(tmp_path):
    with closing(db.connect(tmp_path / "app.db")) as connection:
        yield connection


def bind(conn, *, replay=False):
    snapshot = json.loads(gzip.decompress(SNAPSHOT.read_bytes()))
    material = Material.from_snapshot(snapshot)
    run_id = db.create_run(conn, material.code, status="running")
    db.save_material(conn, run_id, snapshot)
    return ResearchSession(conn, run_id, material, ReviewLedger(),
                           replay_sha256=snapshot["sha256"] if replay else None)


@asynccontextmanager
async def connected(session):
    async with create_client_server_memory_streams() as (client_io, server_io):
        server = asyncio.create_task(session.serve(*server_io))
        try:
            async with ClientSession(*client_io, read_timeout_seconds=5) as client:
                await client.initialize()
                yield client
        finally:
            await client_io[1].aclose()
            await server


def draft():
    data = report()
    data["valuation"] = with_probabilities(next(r["valuation"] for r in STORED if r["code"] == "600519.SH"))
    data["valuation"]["rationale"] = data["financial"]
    # This snapshot already contains the interim filing, so its next period is Q3.
    data["checkpoints"][0]["forecast"]["period"] = data["stance"]["add_trigger"]["period"] = "2026-09-30"
    return data


async def test_discovery_calculation_and_failed_cache_unlock(conn):
    run = bind(conn)
    args = {"label": "经营现金流差额", "operation": "difference",
            "left": {"ref": "T.op_cash", "period": "2025-12-31"},
            "right": {"ref": "T.op_cash", "period": "2024-12-31"}}
    dependent = {"label": "现金流差额占营收", "operation": "ratio_pct",
                 "left": {"ref": "C.2"}, "right": {"ref": "T.revenue", "period": "2025-12-31"}}

    async with connected(run) as client:
        resources = (await client.list_resources()).resources
        assert [str(resource.uri) for resource in resources] == ["research://run/context"]
        context = json.loads((await client.read_resource(resources[0].uri)).contents[0].text)
        assert context["run_id"] == run.run_id and context["material_block"] == run.material.prompt_block()
        listing = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert listing.keys() == EVIDENCE_TOOLS | CONTROL_TOOLS
        assert "runner-only" in listing["record_call"].description.lower()
        first = await client.call_tool("calculate", args)
        assert not first.is_error
        assert first.structured_content["result"]["values"] == {"result": "-309.42"}
        assert first.structured_content["evidence_delta"] == 1 and first.structured_content["charged"]
        for charge in (True, False):
            failure = await client.call_tool("calculate", dependent)
            assert failure.is_error and "C.2" in failure.content[0].text
            assert failure.structured_content["evidence_delta"] == 0 and failure.structured_content["charged"] is charge
        same_value = await client.call_tool("calculate", args | {"label": "现金流差额"})
        assert same_value.structured_content["result"]["id"] == "C.2"
        assert same_value.structured_content["evidence_delta"] == 0 and same_value.structured_content["charged"]
        ratio = await client.call_tool("calculate", dependent)
        assert not ratio.is_error and ratio.structured_content["evidence_delta"] == 1
        assert ratio.structured_content["result"]["values"] == {"result": "-17.98"}
        stop = types.Request[AcquisitionStop, str](method="research/acquisition_stop", params=AcquisitionStop(reason="tool_limit"))
        for _ in range(2):
            await client.send_request(stop, types.EmptyResult)

    saved = replay_material(conn, run.run_id)
    assert saved.table[-1] == ratio.structured_content["result"]
    assert [t["error"] for t in saved.extra["tools"]] == [False, True, False, False]
    assert not saved.extra["gaps"]
    assert saved.extra["stop_reasons"] == [{"stage": "explore", "category": "acquisition_stop", "reason": "tool_limit"}]


@pytest.mark.parametrize("defect", ["draft_hash", "issue_location"])
async def test_replay_repair_and_review_bound_to_frozen_material(conn, defect):
    run = bind(conn, replay=True)
    good = draft()
    prices = program_valuation(good, run.material)
    assert "hurdle_price" in prices
    async with connected(run) as client:
        assert {t.name for t in (await client.list_tools()).tools} == CONTROL_TOOLS
        with pytest.raises(MCPError, match="不可用"):
            await client.call_tool("disclosures", {"code": "600519"})
        submission = await client.call_tool("submit_draft", {"draft": good | {"verdict": ""}})
        assert submission.structured_content["status"] == "repair"
        bad = good | {"verdict": ""}
        assert repair_prompt(run.material, bad, submission.structured_content["problems"], acquire=False,
                             material_block=run.material.prompt_block(), valuation=program_valuation(bad, run.material)) \
            == {k: submission.structured_content[k] for k in ("system_prompt", "prompt")}
        assert submission.structured_content["program_valuation"] == prices
        assert any(p["where"] == "verdict" for p in submission.structured_content["problems"])

        patch = await client.call_tool("submit_patch", {"patch": {"verdict": good["verdict"]}})
        review = patch.structured_content
        assert review["status"] == "review" and review["program_valuation"] == prices
        assert review["binding"]["draft_sha256"] == digest(good)
        assert review["binding"]["material_sha256"] == FROZEN_HASH
        decision = {"version": 1, "draft_sha256": digest(good), "material_sha256": FROZEN_HASH,
                    "issues": [], "resolutions": [], "risk_resolutions": []}
        if defect == "draft_hash":
            invalid = decision | {"draft_sha256": "0" * 64}
        else:
            invalid = decision | {"issues": [{"type": "question", "where": "risks[0] / risks[1]",
                                               "problem": "现金流是否持续下降？", "importance": "影响结论", "evidence": []}]}
        rejected = await client.call_tool("review_decide", invalid)
        assert rejected.is_error and not run.ledger.rounds
        if defect == "issue_location":
            assert "issues[0]" in rejected.content[0].text
        accepted = await client.call_tool("review_decide", decision)
        assert not accepted.is_error and accepted.structured_content["status"] == "complete"
    assert run.result() == {"draft": good, "reviewed": True, "problems": []}
    assert run.material.snapshot()["sha256"] == FROZEN_HASH
    assert replay_material(conn, run.run_id).snapshot()["sha256"] == FROZEN_HASH


async def test_replay_eof_detects_changed_evidence(conn):
    run = bind(conn, replay=True)
    with pytest.raises(EvidenceError):
        async with connected(run) as client:
            await client.list_tools()
            # Simulate an erroneous host-side change after the freeze.
            del run.material.pages[9]


async def test_source_conflict_disconnects_and_keeps_held_page(conn):
    run = bind(conn)
    material = run.material
    # A fetched copy now presents page 10 as page 9. The held evidence is intact.
    material.doc.pages = [material.pages.get(i, "") for i in range(1, max(material.pages) + 1)]
    material.doc.raw_pages = [material.raw_pages.get(i, "") for i in range(1, max(material.pages) + 1)]
    material.doc.pages[8] = material.pages[10]
    before = material.source_registry()
    disconnect = None
    with pytest.raises(EvidenceError):
        async with connected(run) as client:
            try:
                await client.call_tool("read_disclosure", {"source": "D0", "page": 9, "count": 1})
            except MCPError as exc:
                disconnect = exc.code
    assert disconnect == types.CONNECTION_CLOSED and run.failure["category"] == "source_input"
    identity = conn.execute("SELECT identity FROM runs WHERE id=?", (run.run_id,)).fetchone()[0]
    assert json.loads(identity)["failure"] == run.failure
    saved = replay_material(conn, run.run_id)
    assert saved.source_registry() == before
    assert len(saved.extra["tools"]) == 1 and saved.extra["tools"][0]["error"]


async def test_mechanical_source_conflict_returns_failed_report(conn):
    run = bind(conn)
    # A misbound catalog entry is discovered by the mechanical check itself.
    run.material.extra["catalog"]["D0"]["period"] = "2024-12-31"
    async with connected(run) as client:
        result = await client.call_tool("submit_draft", {"draft": draft()})
        assert not result.is_error and result.structured_content["status"] == "failed"
    final = run.result()
    assert not final["reviewed"] and final["problems"][0]["type"] == "source_input"


async def test_runner_audit_round_trip_including_failed_completion(conn):
    run = bind(conn)
    async with connected(run) as client:
        block = run.material.prompt_block()
        messages = [{"role": "user", "content": block}]
        tools = [(await client.list_tools()).tools[0].model_dump(by_alias=True, exclude_none=True)]
        identity = {"requested": {"provider": "openai-codex", "model": "gpt-6.2-sol"},
                    "actual": {"provider": "openai-codex", "model": "gpt-6.2-sol"}}
        payload = {"step": "explore", "messages": messages, "tools": tools, "response": "继续核对",
                   "identity": identity, "usage": {"input": 500, "output": 5}, "seconds": 0.125,
                   "stop_reason": "stop", "error_kind": None}
        logged = await client.call_tool("record_call", payload)
        first = logged.structured_content["call_id"]
        next_messages = messages + [{"role": "assistant", "content": [{"type": "text", "text": "继续核对"}]}]
        failed = await client.call_tool("record_call", payload | {
            "messages": next_messages, "response": {"errorMessage": "private provider detail"},
            "error_kind": "provider_error", "stop_reason": "error",
        })
        second = failed.structured_content["call_id"]

    for call_id, sent in ((first, messages), (second, next_messages)):
        prompt = json.dumps({"messages": sent, "tools": tools}, ensure_ascii=False)
        assert db.call_prompt(conn, call_id) == prompt
        row = conn.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
        assert row["run_id"] == run.run_id and row["step"] == "explore" and row["seconds"] == 0.125
        assert json.loads(row["usage"]) == payload["usage"]
        audit = json.loads(row["identity"])
        assert audit["actual"] == identity["actual"] and audit["prompt_sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
        if call_id == first:
            assert row["response"] == "继续核对" and row["prompt_block"] and audit["stop_reason"] == "stop"
        else:
            assert row["prompt_base"] == first and row["response"] is None and audit["stop_reason"] == "error"
            assert row["error"] and "private provider detail" not in row["error"]


async def test_terminal_model_error_preserves_the_last_provider_attempt(conn):
    run = bind(conn)
    async with connected(run) as client:
        context = json.loads((await client.read_resource("research://run/context")).contents[0].text)
        call = await client.call_tool("record_call", {
            "step": "explore", "messages": [{"role": "user", "content": context["prompt"]}], "tools": [],
            "response": None, "identity": {}, "usage": None, "seconds": 0.125,
            "stop_reason": "error", "error_kind": "provider_error",
        })
        assert not call.is_error and run.last_error_kind == "provider_error"
        rows = conn.execute("SELECT * FROM calls WHERE run_id=?", (run.run_id,)).fetchall()
        assert len(rows) == 1 and "provider_error" in rows[0]["error"]
        request = types.Request[ModelError, str](method="research/model_error", params=ModelError(kind="timeout"))
        await client.send_request(request, types.EmptyResult)
        assert run.last_error_kind == "timeout"
        assert conn.execute("SELECT * FROM calls WHERE run_id=?", (run.run_id,)).fetchall() == rows
