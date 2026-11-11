"""Stored report bodies and public projections; model content remains checking's responsibility.

Legacy snapshots and captured excerpts may omit identity fields. Version 2 writes
require their identity and judgment binding; loading never validates or rewrites them.
"""
import json
from typing import Literal, Required, TypedDict

from pydantic import ConfigDict, TypeAdapter

SCHEMA_VERSION = 2


class Document(TypedDict, total=False):
    code: str
    name: str
    title: str
    period: str
    announced: str
    url: str
    sha256: str
    retrieved_at: str


class Source(TypedDict):
    document: Document
    pages: list[int]


class ReviewBinding(TypedDict):
    draft_sha256: str
    material_sha256: str
    completed: bool
    pending_issue_ids: list[str]


class Failure(TypedDict):
    stage: str
    category: str


class Prior(TypedDict):
    report_id: int
    period: str
    question_added: bool


class Model(TypedDict):
    provider: str | None
    model: str | None
    reasoning: str


class Judgment(TypedDict):
    contract: int
    surface_sha256: str
    author: Model
    reviewer: Model


class Producer(TypedDict):
    kind: Literal['external']
    key_id: int
    submission_id: int
    account_aware: bool


class StopReason(TypedDict):
    stage: str
    category: str
    reason: str


class OpenQuestion(TypedDict):
    id: str
    where: str
    problem: str


class Report(TypedDict, total=False):
    __pydantic_config__ = ConfigDict(extra='forbid')

    schema_version: Required[int]  # Program: stored body schema; absent historical versions load as 1.
    code: str  # Program: company code.
    name: str  # Program: company name.
    period: str  # Program: latest covered period.
    document: Document  # Program: primary disclosure identity.
    pages: list[int]  # Program: pages acquired from the primary disclosure.
    table: list[dict]  # Program: registered evidence rows and calculations.
    market_date: str  # Program: frozen market date.
    calls: int  # Program: model call count.
    review_audit: dict  # Program: independently checked review ledger.
    review_binding: ReviewBinding  # Program: review-to-evidence digest binding.
    stop_reasons: list[StopReason]  # Program: acquisition stop records.
    question: str  # Program: frozen user research question.
    parent_report_id: int | None  # Program: continuation parent, when present.
    sources: dict[str, Source]  # Program: acquired source documents and page indices.
    gaps: list  # Model and program: unresolved evidence gaps; item types are checking's job, so a
    # malformed model list still reaches a failed report instead of aborting the write.
    tool_calls: int  # Program: evidence tool call count.
    announcement_searches: list[dict]  # Program: announcement search coverage.
    methods_used: list[dict]  # Program: retrieved research methods.
    replay_of: int | None  # Program: replay source task, when present.
    material_sha256: str  # Program: frozen material digest.
    judgment: Judgment  # Program: frozen judgment contract and configured model identities.
    producer: Producer  # Program: external author provenance, absent on historical worker reports.
    verdict: str  # Model: checked conclusion.
    summary: str  # Model: checked summary.
    risks: list[dict]  # Model: risk and counterevidence entries.
    business: list[dict]  # Model: business analysis entries.
    financial: list[dict]  # Model: financial analysis entries.
    checkpoints: list[dict]  # Model: evidence checkpoints and forecasts.
    valuation: dict  # Model: valuation assumptions or reason for not pricing.
    stance: dict  # Model: investment stance and triggers.
    prior_status: list[dict]  # Model: treatment of prior research questions.
    answer: list[dict]  # Model: response to the user's question.
    methods: list[dict]  # Model: correction-derived method entries.
    valuation_calc: dict | None  # Program: verified valuation and frozen scoring inputs; legacy captures may store null.
    stance_calc: dict  # Program: frozen stance scoring inputs.
    open_questions: list[OpenQuestion]  # Program: unresolved review questions allowed at publication.
    series: dict  # Program: historical financial series.
    prior: Prior  # Program: prior formal report reference.
    failure: Failure  # Program: failed report stage and category.
    facts: dict  # Program: verified model facts retained after failure.
    facts_reviewed: bool  # Program: whether retained facts received independent review.
    problems: list[dict]  # Program: checking and independent review problems.
    relative: dict  # Historical model field: retired 2026-11-02; preserved on read.


class CurrentReport(Report):
    schema_version: Literal[2]
    code: str
    name: str
    period: str
    judgment: Judgment


PUBLISHED_REQUIRED_FIELDS = ('verdict', 'summary', 'risks', 'business', 'financial')
PUBLISHED_FIELDS = (
    *PUBLISHED_REQUIRED_FIELDS,
    'checkpoints', 'valuation', 'stance', 'prior_status',
)
COMPACT_FIELDS = (
    'verdict', 'summary', 'relative', 'valuation', 'valuation_calc', 'stance', 'stance_calc',
    'answer', 'open_questions', 'prior', 'prior_status', 'risks', 'business', 'financial',
    'methods', 'checkpoints', 'judgment', 'producer',
)
FAILED_FIELDS = ('facts', 'facts_reviewed', 'problems', 'producer')
CARD_FIELDS = ('verdict', 'summary', 'risks', 'checkpoints', 'relative', 'valuation_calc',
               'stance', 'stance_calc', 'open_questions')

_ADAPTER = TypeAdapter(Report)
_CURRENT_ADAPTER = TypeAdapter(CurrentReport)


def validate(body: dict) -> None:
    """Reject undeclared fields and malformed structures, with Pydantic field paths."""
    (_CURRENT_ADAPTER if body.get('schema_version') == SCHEMA_VERSION else _ADAPTER).validate_python(body)


def load(text: str) -> Report:
    """Read without changing historical values or fabricating missing metadata."""
    body = json.loads(text)
    body.setdefault('schema_version', 1)
    return body


def json_schema() -> dict:
    """Schema of newly written reports, including fields retained for historical readers."""
    return _CURRENT_ADAPTER.json_schema()
