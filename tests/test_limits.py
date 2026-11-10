"""Layered runtime guards, independent of judgment semantics."""

from a_share_research.limits import (
    ACQUISITION_SECONDS, CALL_LIMIT, CALL_TIMEOUT_SECONDS, DRAFT_CALLS, FETCH_LIMIT,
    MAX_CALLS, MONTHLY_MAX_CALLS, REPAIR_CALL_RESERVE, ROUND_LIMIT, RUN_SECONDS,
)


def test_model_call_budgets():
    # Runner.__init__: the default cap must not silently shrink the authorized monthly cap.
    assert MONTHLY_MAX_CALLS <= MAX_CALLS
    # Acquisition.write: each cap must allow evidence before the <= DRAFT_CALLS stop.
    assert min(ROUND_LIMIT, MONTHLY_MAX_CALLS - DRAFT_CALLS, MAX_CALLS - DRAFT_CALLS) >= 1
    # Acquisition.write/review/revise: draft + declined tool turn + review + format correction must fit.
    assert DRAFT_CALLS >= 1 + 1 + 2


def test_round_guard_stops_before_the_model_call_guard_fails_the_run():
    # Runner.call: after ordinary rounds, review and two repairs must not hit the default hard cap.
    assert MAX_CALLS >= ROUND_LIMIT + 6


def test_acquisition_budget():
    # Acquisition.tool_result: the total tool cap must allow the configured external fetch allowance.
    assert 0 < FETCH_LIMIT <= CALL_LIMIT
    # Acquisition.stopped: repair adds a positive reserve instead of replacing exploration's allowance.
    assert CALL_LIMIT + REPAIR_CALL_RESERVE > CALL_LIMIT
    # Acquisition.stopped/Runner.call: acquisition must stop before the overall deadline.
    assert 0 < ACQUISITION_SECONDS < RUN_SECONDS


def test_time_left_after_acquisition():
    # Runner.call caps each call at the time left in the run, so the two values only have to leave one
    # full-length call after acquisition stops; with less, the draft itself would be cut short.
    assert RUN_SECONDS - ACQUISITION_SECONDS >= CALL_TIMEOUT_SECONDS
