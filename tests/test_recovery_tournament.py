"""Recovery Tournament: the ranking agrees with the executor, and never invents
confidence.

Two things are worth testing. First, the tournament must never recommend a
repair `RecoveryEngine.recover()` would refuse -- a ranking that disagreed with
the thing that runs would be advice the system does not follow. Second, the
likelihood it prints must come from declared properties, not a number pulled
from nowhere.
"""

from __future__ import annotations

import pytest

from jarvis.core.models import ActionResult, RiskLevel, ToolSpec, Verification
from jarvis.core.recovery_tournament import (
    ALTERNATIVE,
    ASK,
    RETRY,
    SAFE,
    UNSAFE,
    RecoveryTournament,
)
from jarvis.registry.tools import ToolRegistry


def _result(ok=True, error=None, message="", verification=Verification.UNKNOWN):
    return ActionResult(ok=ok, tool="t", error=error, message=message,
                        verification=verification,
                        verified=verification is Verification.SUCCESS)


def _spec(name, risk=RiskLevel.READ, tags=(), reversible=True):
    return ToolSpec(name=name, risk=risk, tags=tuple(tags), reversible=reversible)


@pytest.fixture()
def registry():
    r = ToolRegistry()

    async def handler(ctx, **params):  # pragma: no cover - unused by ranking
        return _result()

    r.register(_spec("search_files"), handler)
    r.register(_spec("list_directory", risk=RiskLevel.LOW), handler)
    r.register(_spec("delete_file", risk=RiskLevel.DESTRUCTIVE, reversible=False), handler)
    return r


# -- the ranking follows the executor's own rules ----------------------------

def test_a_read_tool_is_retried_and_ranked_first(registry):
    tour = RecoveryTournament().evaluate(
        tool="search_files", spec=registry.get("search_files"),
        result=_result(ok=False, error="try again"),
        registry=registry)

    top = tour.recommended
    assert top is not None and top.kind == RETRY and top.tool == "search_files"
    assert top.safety == SAFE


def test_a_destructive_tool_is_never_recommended_for_retry(registry):
    """A retry of a destructive action could act on a world nobody approved.
    The tournament must rank it unsafe, full stop."""
    tour = RecoveryTournament().evaluate(
        tool="delete_file", spec=registry.get("delete_file"),
        result=_result(ok=False, error="temporary failure"),
        registry=registry)

    retry = next(c for c in tour.candidates if c.kind == RETRY)
    assert retry.safety == UNSAFE
    assert "destructive" in retry.safety_reason
    # With no safe alternative, the recommendation is None (only "ask" remains).
    assert tour.recommended is None
    assert "No repair is safe" in tour.to_dict()["note"]


def test_an_unregistered_tool_is_not_vouched_for(registry):
    tour = RecoveryTournament().evaluate(
        tool="mystery_tool", spec=None,
        result=_result(ok=False, error="boom"), registry=registry)

    retry = next(c for c in tour.candidates if c.kind == RETRY)
    assert retry.safety == UNSAFE
    assert "not a registered tool" in retry.safety_reason


def test_a_safe_alternative_is_offered_and_ranked_after_retry(registry):
    tour = RecoveryTournament().evaluate(
        tool="search_files", spec=registry.get("search_files"),
        result=_result(ok=False, error="spotlight unavailable"),
        registry=registry, alternatives=["list_directory"])

    kinds = [(c.kind, c.tool, c.rank) for c in tour.candidates]
    # Retry ranks above the alternative, which ranks above "ask".
    retry_rank = next(r for k, _t, r in kinds if k == RETRY and r < 999)
    alt = next(c for c in tour.candidates
               if c.kind == ALTERNATIVE and c.tool == "list_directory")
    ask = next(c for c in tour.candidates if c.kind == ASK)
    assert retry_rank < alt.rank < ask.rank


def test_a_destructive_alternative_is_ranked_unsafe(registry):
    tour = RecoveryTournament().evaluate(
        tool="search_files", spec=registry.get("search_files"),
        result=_result(ok=False, error="x"),
        registry=registry, alternatives=["delete_file"])

    alt = next(c for c in tour.candidates
               if c.kind == ALTERNATIVE and c.tool == "delete_file")
    assert alt.safety == UNSAFE


# -- the tournament cannot drift from the executor ---------------------------

def test_the_default_policy_matches_the_recovery_engine(registry):
    """The tournament's fallback policy must reach the same verdicts as
    `RecoveryEngine.retry_policy`. A divergence would let the ranking recommend
    a retry `recover()` refuses, or vice versa."""
    from jarvis.core.recovery import RecoveryEngine
    from jarvis.core.recovery_tournament import _default_retry_policy

    engine = RecoveryEngine()
    cases = [
        ("search_files", registry.get("search_files"), "read"),
        ("delete_file", registry.get("delete_file"), "destructive"),
        ("list_directory", registry.get("list_directory"), "low"),
        ("mystery", None, "unregistered"),
    ]
    for name, spec, _label in cases:
        result = _result(ok=False, error="try again")
        engine_verdict = engine.retry_policy(spec, result)
        default_verdict = _default_retry_policy(spec, result)
        assert engine_verdict[0] == default_verdict[0], (
            f"{name}: engine says {engine_verdict}, tournament says {default_verdict}")


# -- the ranking is deterministic and explained ------------------------------

def test_the_ranking_is_deterministic(registry):
    a = RecoveryTournament().evaluate(
        tool="search_files", spec=registry.get("search_files"),
        result=_result(ok=False, error="x"),
        registry=registry, alternatives=["list_directory"]).to_dict()
    b = RecoveryTournament().evaluate(
        tool="search_files", spec=registry.get("search_files"),
        result=_result(ok=False, error="x"),
        registry=registry, alternatives=["list_directory"]).to_dict()
    assert a == b


def test_every_candidate_carries_its_reasoning(registry):
    tour = RecoveryTournament().evaluate(
        tool="search_files", spec=registry.get("search_files"),
        result=_result(ok=False, error="x"),
        registry=registry, alternatives=["list_directory"])

    for candidate in tour.candidates:
        assert candidate.safety_reason, f"{candidate.tool} has no safety reason"
        assert candidate.likelihood, f"{candidate.tool} has no likelihood"
        assert candidate.cost, f"{candidate.tool} has no cost"


def test_a_transient_error_is_called_out_as_more_likely(registry):
    transient = RecoveryTournament().evaluate(
        tool="search_files", spec=registry.get("search_files"),
        result=_result(ok=False, error="connection refused"), registry=registry)
    retry = next(c for c in transient.candidates if c.kind == RETRY)
    assert "transient" in retry.likelihood
    assert "did not look transient" not in retry.likelihood


def test_a_non_transient_error_is_not_overstated(registry):
    steady = RecoveryTournament().evaluate(
        tool="search_files", spec=registry.get("search_files"),
        result=_result(ok=False, error="permission denied: EACCES"), registry=registry)
    retry = next(c for c in steady.candidates if c.kind == RETRY)
    assert "did not look transient" in retry.likelihood, (
        "a non-transient error was ranked as if it would clear on its own")


# -- using the live engine, not just the fallback ----------------------------

def test_the_tournament_uses_the_injected_engine_policy(registry):
    """When given a real engine, the tournament ranks against it rather than its
    own copy, so the two cannot disagree in production."""
    from jarvis.core.recovery import RecoveryEngine

    engine = RecoveryEngine()
    tour = RecoveryTournament(engine=engine).evaluate(
        tool="delete_file", spec=registry.get("delete_file"),
        result=_result(ok=False, error="x"), registry=registry)
    retry = next(c for c in tour.candidates if c.kind == RETRY)
    assert retry.safety == UNSAFE
    assert engine.retry_policy(registry.get("delete_file"), _result(ok=False))[0] is False

# -- over the wire ------------------------------------------------------------

def test_the_tournament_ranks_a_real_failure_over_http():
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}

    response = client.post("/recovery/tournament", headers=headers, json={
        "tool": "search_files",
        "error": "spotlight is unavailable, try again",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["candidates"], "no candidates for a real retryable tool"
    rec = body["recommended"]
    assert rec is not None and rec["kind"] == RETRY, body
    assert "transient" in rec["likelihood"]


def test_a_destructive_failure_has_no_recommendation_over_http():
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}

    response = client.post("/recovery/tournament", headers=headers, json={
        "tool": "delete_file", "error": "permission denied"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["recommended"] is None
    assert "No repair is safe" in body["note"]


def test_an_unknown_tool_is_a_422():
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}
    response = client.post("/recovery/tournament", headers=headers, json={"tool": ""})
    assert response.status_code == 422
