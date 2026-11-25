"""Run-local MCP tools over the worker's evidence, draft and review ledger."""
import argparse
import hashlib
import io
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from typing import BinaryIO, Literal, NoReturn

import anyio
from jsonschema import Draft202012Validator
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from a_share_research import db
from a_share_research.checking import check
from a_share_research.drafts import RepairFormatError, indexed_draft, merge_repair_patch, program_valuation
from a_share_research.evidence import EvidenceError
from a_share_research.material import Material, replay_material
from a_share_research.prompts import output_schema, render_prompt, schema_facts
from a_share_research.research_tools import TOOLS, EvidenceSession
from a_share_research.reviewing import PROTOCOL, ReviewFormatError, ReviewLedger, digest

log = logging.getLogger(__name__)
CONTEXT_URI = "research://run/context"
ERROR_KINDS = ("timeout", "provider_error", "pi_runtime", "incomplete:length", "incomplete", "empty_output", "invalid_output")


class AcquisitionStop(types.RequestParams):
    reason: Literal["time_limit", "tool_limit", "no_new_evidence", "round_limit", "fetch_limit", "model_limit"]


def _tool(name, description, fields):
    return types.Tool(name=name, description=description, input_schema={
        "type": "object", "properties": fields, "required": list(fields), "additionalProperties": False})


CONTROL_TOOLS = [
    _tool("submit_draft", "提交完整研究稿，由程序核查并绑定本次材料。", {"draft": {"type": "object"}}),
    _tool("review_decide", PROTOCOL, {
        "version": {"type": "integer", "const": 1}, "draft_sha256": {"type": "string"},
        "material_sha256": {"type": "string"}, **{k: {"type": "array", "items": {"type": "object"}}
                                                  for k in ("issues", "resolutions", "risk_resolutions")}}),
    _tool("submit_patch", "按原稿索引提交原子补丁，可含 ops/verdict/summary/gaps/valuation/stance/rebuttals。", {
        "patch": {"type": "object"}}),
    _tool("record_call", "runner-only：记录一次实际模型请求；仅供 runner 使用。", {
        "step": {"enum": ["explore", "compose", "review", "repair"]},
        "messages": {"type": "array"}, "tools": {"type": "array"},
        "response": {"type": ["object", "string", "null"]}, "usage": {"type": ["object", "null"]},
        "seconds": {"type": "number", "minimum": 0}, "error_kind": {"enum": [None, *ERROR_KINDS]},
        "identity": {"type": "object"}, "stop_reason": {"type": ["string", "null"]}}),
]


def _receipt(value, *, error=False):
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(value, ensure_ascii=False))],
        structured_content=value, is_error=error)


def explore_prompt(material: Material, *, material_block: str | None = None) -> str:
    latest = max(material.extra["catalog"].items(), key=lambda item: item[1].get("period", ""), default=None)
    cards = [m["title"] for m in material.extra.get("method_catalog", []) if "card" in m]
    return render_prompt("explore.j2", latest=latest if latest and latest[1].get("period", "") > material.doc.period else None,
                         cards=cards, material_block=material.prompt_block() if material_block is None else material_block,
                         **schema_facts(material))


def compose_prompt(material: Material, *, material_block: str | None = None) -> str:
    return render_prompt("compose.j2", material_block=material.prompt_block() if material_block is None else material_block,
                         **schema_facts(material))


def review_prompt(material: Material, report: dict, binding: dict, *,
                  material_block: str | None = None, valuation: str | None = None) -> str:
    return render_prompt("review.j2", binding=json.dumps(binding, ensure_ascii=False),
                         report=json.dumps(report, ensure_ascii=False, indent=1),
                         program_valuation=program_valuation(report, material) if valuation is None else valuation,
                         material_block=material.prompt_block() if material_block is None else material_block)


def repair_prompt(material: Material, report: dict, problems: list[dict], *, acquire: bool,
                  material_block: str | None = None, valuation: str | None = None) -> str:
    return render_prompt("repair.j2", acquire=acquire, problems=json.dumps(problems, ensure_ascii=False, indent=1),
                         indexed_report=json.dumps(indexed_draft(report), ensure_ascii=False, indent=1),
                         program_valuation=program_valuation(report, material) if valuation is None else valuation,
                         material_block=material.prompt_block() if material_block is None else material_block)


class ResearchSession:
    """One worker run. A standalone session uses a frozen snapshot and only an in-memory ledger."""

    def __init__(self, conn: sqlite3.Connection, run_id: int, material: Material, ledger: ReviewLedger, *,
                 replay_sha256: str | None = None, effective_model_call_limit: int | None = None,
                 deadline: float | None = None, profile_sha256: str | None = None, standalone: bool = False):
        self.conn, self.run_id, self.material, self.ledger = conn, run_id, material, ledger
        self.replay_sha256 = replay_sha256
        if standalone and replay_sha256 is None:
            raise ValueError("独立会话需要来源 run 的冻结材料哈希")
        self.standalone = standalone
        self.effective_model_call_limit, self.deadline, self.profile_sha256 = effective_model_call_limit, deadline, profile_sha256
        self.evidence = None if self.replay_sha256 is not None else EvidenceSession(conn, run_id, material)
        self.stage = "compose" if self.evidence is None else "explore"
        self.draft: dict | None = None
        self.problems: list[dict] = []
        self.reviewed_binding: tuple[str, str] | None = None
        self.failure: dict | None = None
        self.last_error_kind: str | None = None
        self._chains: dict[str, tuple[int, str]] = {}
        self._material_block: str | None = None
        self._server = self._build_server()

    def flush(self):
        if self.evidence is not None:
            self.evidence.flush()

    def material_block(self):
        # Evidence tools can change tables, sources, methods and gaps; traces alone
        # are not part of the prompt. Invalidate after each tool execution.
        if self._material_block is None:
            self._material_block = self.material.prompt_block()
        return self._material_block

    def _draft_context(self):
        """Computed once per receipt and shared between its prompt and its fields."""
        return {"material_block": self.material_block(),
                "program_valuation": program_valuation(self.draft, self.material)}

    def _stage(self, stage):
        self.flush()
        self.stage = stage
        if not self.standalone:
            db.update_run(self.conn, self.run_id, step=stage)

    def _context(self):
        return {"run_id": self.run_id, "mode": "replay" if self.evidence is None else "run",
                "material_block": self.material_block(), "question": self.material.extra.get("question", ""),
                "prompt": compose_prompt(self.material, material_block=self.material_block()) if self.evidence is None
                else explore_prompt(self.material, material_block=self.material_block()),
                "output_schema": output_schema(self.material), "profile_sha256": self.profile_sha256,
                "effective_model_call_limit": self.effective_model_call_limit,
                "remaining_ms": max(0, int((self.deadline - time.monotonic()) * 1000)) if self.deadline is not None else None}

    def _repair_input(self):
        self.ledger.protect_risks(self.draft)
        self._stage("repair")
        self._chains.pop("repair", None)
        context = self._draft_context()
        return {"status": "repair", "problems": self.problems, "indexed_draft": indexed_draft(self.draft),
                "prompt": repair_prompt(self.material, self.draft, self.problems, acquire=self.evidence is not None,
                                        material_block=context["material_block"], valuation=context["program_valuation"]),
                **context}

    def _submit(self, draft):
        self.draft = draft
        self.reviewed_binding = None
        self.problems = check(draft, self.material)
        if self.problems:
            self.problems.extend(self.ledger.pending())
            if any(p.get("type") == "source_input" for p in self.problems):
                self._stage("check")
                return {"status": "failed", "problems": self.problems}
            return self._repair_input()
        self._stage("review")
        self._chains.pop("review", None)
        binding = self.ledger.request(draft, self.material.snapshot()["sha256"])
        context = self._draft_context()
        return {"status": "review", "draft": draft, "binding": binding, "material_block": context["material_block"],
                "prompt": review_prompt(self.material, draft, binding,
                                        material_block=context["material_block"], valuation=context["program_valuation"]),
                "program_valuation": context["program_valuation"]}

    def _review(self, args):
        if self.stage != "review" or self.draft is None or self.reviewed_binding is not None:
            raise ReviewFormatError("请先提交一份通过机械核查、等待复核的报告")
        sha = self.material.snapshot()["sha256"]
        self.problems = self.ledger.accept(args, self.draft, self.material.source_registry(), self.material.table, sha)
        self.reviewed_binding = (args["draft_sha256"], sha)
        return self._repair_input() if self.problems else {"status": "complete", "problems": []}

    def _record_call(self, args):
        step = args["step"]
        prompt = json.dumps({"messages": args["messages"], "tools": args["tools"]}, ensure_ascii=False)
        identity = args["identity"] | {"stop_reason": args["stop_reason"],
                                       "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}
        response, kind = args["response"], args["error_kind"]
        if kind:
            response = None
        elif isinstance(response, dict):
            response = json.dumps(response, ensure_ascii=False)
        with db.transaction(self.conn):
            call_id = db.log_call(self.conn, self.run_id, step, prompt,
                                  response=response, usage=args["usage"], seconds=args["seconds"],
                                  error=f"模型阶段未完成（{kind}）" if kind else None, identity=identity,
                                  base=self._chains.get(step), block=self.material_block())
        self._chains[step] = (call_id, prompt)
        self.last_error_kind = kind
        return {"call_id": call_id}

    def _verify_replay(self):
        if self.replay_sha256 is not None and self.material.snapshot()["sha256"] != self.replay_sha256:
            raise EvidenceError("重放结束时材料哈希与冻结快照不一致")

    def result(self) -> dict:
        """Return the final draft for the worker's existing delivery gate, including failed reports."""
        self.flush()
        self._verify_replay()
        if self.failure:
            raise RuntimeError(self.failure["message"])
        reviewed = self.reviewed_binding is not None and self.reviewed_binding == (
            digest(self.draft), self.material.snapshot()["sha256"])
        problems = self.problems
        if self.reviewed_binding is None and not problems and self.stage == "review" and "review" in self._chains:
            # Pi may reject tool arguments locally, before review_decide reaches the worker.
            problems = self.ledger.pending() + [{"where": "review", "type": "review_format", "problem": "复核未提交有效的整轮决议"}]
        if self.draft is None or not reviewed and not problems:
            raise ValueError("研究未提交稿件或终稿缺少绑定本次材料的独立复核")
        return {"draft": self.draft, "problems": problems, "reviewed": reviewed}

    def _call(self, name, args):
        if name == "record_call":
            return _receipt(self._record_call(args))
        if name == "review_decide":
            return _receipt(self._review(args))
        if name == "submit_draft":
            return _receipt(self._submit(args["draft"]))
        if name == "submit_patch":
            if self.draft is None or self.stage != "repair":
                raise RepairFormatError("须先提交原稿并取得待修补问题")
            return _receipt(self._submit(merge_repair_patch(self.draft, args["patch"], self.problems, self.material)))
        try:
            value, failed = self.evidence.call(name, args, self.stage)
            return _receipt(value, error=failed)
        finally:
            self._material_block = None

    async def _abort(self, category, exc) -> NoReturn:
        self.failure = {"category": category, "stage": self.stage, "message": str(exc)}
        self._fatal_error = exc
        self._scope.cancel()
        await anyio.sleep_forever()

    def _build_server(self):
        catalog = {tool.name: tool for tool in CONTROL_TOOLS if tool.name != "record_call" or not self.standalone}
        evidence_tools = {s["name"]: types.Tool(name=s["name"], description=s["description"], input_schema=s["parameters"])
                          for s in TOOLS} if self.evidence is not None else {}
        catalog.update(evidence_tools)
        validators = {name: Draft202012Validator(tool.input_schema) for name, tool in catalog.items()}
        resource = types.Resource(uri=CONTEXT_URI, name="run_context", mime_type="application/json")

        async def list_tools(ctx, params):
            return types.ListToolsResult(tools=list(catalog.values()))

        async def call_tool(ctx, params):
            name, args = params.name, params.arguments or {}
            if name not in catalog:
                raise MCPError(types.INVALID_PARAMS, f"工具 {name} 不可用；请用 tools/list 查询本次工具集")
            try:
                if problem := next(validators[name].iter_errors(args), None):
                    message = f"{problem.json_path}: {problem.message}"
                    if name == "record_call":
                        raise ValueError("调用审计字段无效：" + problem.json_path)
                    if name == "review_decide":
                        raise ReviewFormatError(message)
                    return _receipt({"result": {"error": message}, "evidence_delta": 0, "charged": False}
                                    if name in evidence_tools else {"error": message}, error=True)
                return self._call(name, args)
            except ReviewFormatError as exc:
                if self.stage == "review" and self.reviewed_binding is None:
                    self.problems = self.ledger.pending() + [{"where": "review", "type": "review_format", "problem": str(exc)}]
                return _receipt({"error": str(exc)}, error=True)
            except RepairFormatError as exc:
                return _receipt({"error": str(exc)}, error=True)
            except EvidenceError as exc:
                await self._abort("source_input", exc)
            except Exception:
                log.exception("run %s failed in %s", self.run_id, name)
                await self._abort("pipeline_error", RuntimeError("研究服务执行失败，请核对阶段记录"))

        async def list_resources(ctx, params):
            return types.ListResourcesResult(resources=[resource])

        async def read_resource(ctx, params):
            if str(params.uri) != CONTEXT_URI:
                raise MCPError(types.INVALID_PARAMS, "当前 run 的上下文位于 " + CONTEXT_URI)
            try:
                value = self._context()
            except EvidenceError as exc:
                await self._abort("source_input", exc)
            self._stage(self.stage)
            return types.ReadResourceResult(contents=[types.TextResourceContents(
                uri=resource.uri, mime_type=resource.mime_type, text=json.dumps(value, ensure_ascii=False))])

        server = Server("a-share-research", on_list_tools=list_tools, on_call_tool=call_tool,
                        get_tool_input_schema=lambda name: catalog[name].input_schema if name in catalog else None,
                        on_list_resources=list_resources, on_read_resource=read_resource)
        if self.evidence is not None:
            async def stop(ctx, params):
                if self.stage not in ("explore", "repair"):
                    raise MCPError(types.INVALID_PARAMS, "当前阶段未在取证")
                self.evidence.stop(params.reason, self.stage)
                return types.EmptyResult()

            server.add_request_handler("research/acquisition_stop", AcquisitionStop, stop)
        return server

    async def serve(self, read_stream, write_stream):
        """Serve MCP until EOF or a fatal run error; the latter also remains on failure for the worker."""
        try:
            with anyio.CancelScope() as self._scope:
                await self._server.run(read_stream, write_stream, self._server.create_initialization_options())
            if self.failure:
                raise self._fatal_error
            self._verify_replay()
        except EvidenceError as exc:
            self.failure = {"category": "source_input", "stage": self.stage, "message": str(exc)}
            if not self.standalone:
                self.conn.execute(
                    "UPDATE runs SET identity=json_set(coalesce(identity, '{}'), '$.failure', json(?)) WHERE id=?",
                    (json.dumps(self.failure, ensure_ascii=False), self.run_id))
            raise
        finally:
            self.flush()


async def serve_stdio(session: ResearchSession, child_stdout: BinaryIO, child_stdin: BinaryIO, *,
                      terminate: Callable[[], None]):
    """Serve a Popen child's binary pipes. EOF from the child ends the session."""
    failure = None
    try:
        with (io.TextIOWrapper(child_stdout, encoding="utf-8") as rd,
              io.TextIOWrapper(child_stdin, encoding="utf-8", line_buffering=True) as wr):
            async with stdio_server(stdin=anyio.wrap_file(rd), stdout=anyio.wrap_file(wr)) as streams:
                try:
                    await session.serve(*streams)
                except anyio.get_cancelled_exc_class():
                    # A failed pipe task cancels the session; its group carries the actual error.
                    raise
                except BaseException as exc:
                    failure = exc
                    # Release the SDK's blocking pipe reader on failure or worker deadline.
                    terminate()
    finally:
        # Killing the peer can also break an SDK shutdown write. Preserve the run's actual cause.
        if failure is not None:
            raise failure from None


def main():
    parser = argparse.ArgumentParser(description="在内存中用冻结研究材料试稿、复核，历史库只读。")
    parser.add_argument("--run-id", "--replay", dest="run_id", type=int, required=True, metavar="N")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    async def run():
        uri = (db.DATA / "app.db").resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            material = replay_material(conn, args.run_id)
            sha = conn.execute("SELECT json_extract(material, '$.sha256') FROM runs WHERE id=?", (args.run_id,)).fetchone()[0]
            session = ResearchSession(conn, args.run_id, material, ReviewLedger(), replay_sha256=sha, standalone=True)
            async with stdio_server() as streams:
                await session.serve(*streams)

    anyio.run(run)


if __name__ == "__main__":
    main()
