"""Does POST /command execute an action twice when the same command is sent twice?

This file exists to *measure* the service boundary, not to assume it. A disabled
button in a client is not a guarantee about the backend: the UI in
``macos/Vantage/Sources/Views/TodayView.swift`` guards its own double-press with a
``creatingEvent`` state flag, but any other caller -- the phone, the dashboard,
``curl``, a retrying HTTP client -- reaches ``/command`` without that flag.

Measured on this tree (2026-11-19), with a counting handler installed on the real
tool and the request driven through the real FastAPI route:

* two identical POSTs run the tool handler **twice**;
* two **concurrent** identical POSTs also run it twice;
* the route accepts no request id or idempotency key -- the body model has no
  such field, and an extra field is silently dropped;
* ``MissionStep.idempotency_key`` exists and is persisted, but nothing in the
  command pipeline ever sets or consults it to suppress a repeat: each duplicate
  POST creates a *separate* mission whose step carries an empty key;
* an ``Idempotency-Key`` request header is ignored.

**This is a measured gap, not a passing guarantee.** The tests below are written
so they pass by documenting what the code does today. ``test_requirement_...`` is
marked ``xfail(strict=True)``: it encodes the *desired* property (one execution
per duplicate). When the gap is closed that test will XPASS and strict mode will
make the suite fail, which is the signal to delete the xfail and assert the fix
for real.

No fix is proposed here; this file measures only.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.api.app import app, get_core
from jarvis.api.schemas import CommandBody
from jarvis.core.models import ActionResult, CommandRequest

TEST_TOKEN = __import__("os").environ["JARVIS_API_TOKEN"]
HEADERS = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}

#: A real, harmless, READ-tier tool the rules planner reaches from the sentence
#: below (verified by parsing it against the live registry). It needs no
#: argument, is not confirmation-gated, and touches nothing outside memory, so a
#: double execution is observable and safe to count.
TOOL = "list_workspaces"
SENTENCE = "list the workspaces"
SESSION = "idempotency-probe"


@pytest.fixture(autouse=True)
def _handlers_restored():
    """Put the app core's real handlers back after each test.

    ``get_core()`` is the process-wide instance the routes dispatch through, and
    a counting stub left installed would change what every later test sees.
    """
    core = get_core()
    saved = dict(core.tools._handlers)
    yield core
    core.tools._handlers.clear()
    core.tools._handlers.update(saved)


def _counting_handler(calls: dict, *, barrier: asyncio.Event | None = None):
    """A handler that records each invocation and succeeds.

    With ``barrier`` set, the handler waits until both concurrent requests have
    entered it, so the test forces real overlap instead of hoping the event loop
    interleaves two fast calls.
    """
    async def handler(ctx, **kw):
        calls["n"] += 1
        if barrier is not None:
            if calls["n"] >= 2:
                barrier.set()
            try:
                await asyncio.wait_for(barrier.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
        return ActionResult(ok=True, tool=TOOL, message=f"{TOOL} call {calls['n']}",
                            verified=True)
    return handler


def _client() -> TestClient:
    # The workspace-origin base URL every route test uses (see
    # tests/test_command_center_acceptance.py); the security middleware refuses
    # a non-loopback Host.
    return TestClient(app, base_url="http://127.0.0.1:8765", headers=HEADERS)


def _body(extra: dict | None = None) -> dict:
    body = {"text": SENTENCE, "session_id": SESSION, "source": "api"}
    body.update(extra or {})
    return body


# ---------------------------------------------------------------------------
# 1. Is there even a way for a caller to identify a duplicate?
# ---------------------------------------------------------------------------

def test_the_command_contract_accepts_no_request_id_or_idempotency_key():
    """The route cannot dedupe on an id it never reads.

    ``/command`` builds its ``CommandRequest`` from five fields only
    (jarvis/api/app.py:1056-1072). Neither the HTTP body model
    (jarvis/api/schemas.py:20) nor the internal request
    (jarvis/core/models.py:390) has a request-id or idempotency field, so there
    is no value a retrying client could send that the backend would use to
    recognise "this is the same request again". ``session_id`` is the only
    caller-controlled correlation value, and it is used for conversation, not
    deduplication.
    """
    assert "request_id" not in CommandBody.model_fields
    assert "idempotency_key" not in CommandBody.model_fields
    assert "request_id" not in CommandRequest.model_fields
    assert "idempotency_key" not in CommandRequest.model_fields

    # Sending them anyway is silently ignored (pydantic's default extra=ignore),
    # not rejected -- so a client trying to be safe gets no error and no dedupe.
    calls = {"n": 0}
    get_core().tools._handlers[TOOL] = _counting_handler(calls)
    client = _client()
    first = client.post("/command", json=_body({"request_id": "REQ-fixed",
                                                "idempotency_key": "K-fixed"}))
    second = client.post("/command", json=_body({"request_id": "REQ-fixed",
                                                 "idempotency_key": "K-fixed"}))
    assert first.status_code == 200 and second.status_code == 200
    assert calls["n"] == 2, (
        "a client-supplied identity unexpectedly suppressed a repeat -- re-measure "
        "before trusting the gap documented in this file")


# ---------------------------------------------------------------------------
# 2. The central measurement: the same command twice, in sequence.
# ---------------------------------------------------------------------------

def test_two_identical_posts_execute_the_tool_twice(_handlers_restored):
    """MEASURED GAP: the side effect runs twice for two identical commands.

    The counting handler is the side effect. Two POSTs with the same text, the
    same session, and the same device both return 200 and both reach the
    handler. The responses even carry *different* ``request_id`` and
    ``mission_id`` values, which is the evidence that the backend treated the
    second call as new work rather than as a repeat of the first.
    """
    core = _handlers_restored
    calls = {"n": 0}
    core.tools._handlers[TOOL] = _counting_handler(calls)
    client = _client()

    first = client.post("/command", json=_body())
    second = client.post("/command", json=_body())

    assert first.status_code == 200 and second.status_code == 200
    assert calls["n"] == 2, "the tool did not run twice; re-measure the finding"

    one, two = first.json(), second.json()
    assert [a["tool"] for a in one["actions"]] == [TOOL]
    assert [a["tool"] for a in two["actions"]] == [TOOL]
    assert one["request_id"] != two["request_id"]
    assert one["mission_id"] and two["mission_id"]
    assert one["mission_id"] != two["mission_id"], (
        "the second command was folded into the first mission; the duplicate "
        "suppression has changed and this file must be updated")


# ---------------------------------------------------------------------------
# 3. The mission idempotency key is stored, not consulted.
# ---------------------------------------------------------------------------

def test_mission_steps_carry_an_empty_idempotency_key_and_are_never_deduped(
        _handlers_restored):
    """``MissionStep.idempotency_key`` exists but does not suppress anything here.

    The pipeline that creates a mission for a ``/command`` writes each step with
    no key at all (jarvis/core/orchestrator.py:1303-1311 does not pass one), and
    the field's own contract (jarvis/core/missions.py:156-157) says it exists so
    that *a resumed mission* does not repeat a side effect -- a different, later
    question from two arriving commands. Resumption is covered by
    ``tests/test_mission_runner.py::test_a_completed_step_is_not_run_again_on_resume``;
    it says nothing about duplicate inbound requests, and this test shows the
    command path does not populate the key at all.
    """
    core = _handlers_restored
    calls = {"n": 0}
    core.tools._handlers[TOOL] = _counting_handler(calls)
    client = _client()

    responses = [client.post("/command", json=_body()).json() for _ in range(2)]
    assert calls["n"] == 2

    for response in responses:
        mission = core.missions.get(response["mission_id"])
        assert mission is not None
        assert mission.steps, "the command produced no mission step to inspect"
        assert all(step.idempotency_key == "" for step in mission.steps), (
            "the command pipeline started writing idempotency keys; re-measure "
            "whether it now suppresses duplicates")

    # Two commands, two missions: nothing correlated them as one request.
    assert len({r["mission_id"] for r in responses}) == 2


# ---------------------------------------------------------------------------
# 4. The standard HTTP idempotency header is ignored.
# ---------------------------------------------------------------------------

def test_an_idempotency_key_header_changes_nothing(_handlers_restored):
    """A client following the ``Idempotency-Key`` convention gets no protection.

    No route in ``jarvis/api`` reads that header (the same is true of
    ``X-Request-Id``), so it cannot participate in a dedupe that does not exist.
    """
    core = _handlers_restored
    calls = {"n": 0}
    core.tools._handlers[TOOL] = _counting_handler(calls)
    client = _client()
    keyed = {**HEADERS, "Idempotency-Key": "K-1", "X-Request-Id": "REQ-1"}

    first = client.post("/command", headers=keyed, json=_body())
    second = client.post("/command", headers=keyed, json=_body())

    assert first.status_code == 200 and second.status_code == 200
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# 5. Concurrency: two identical requests already in flight.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_identical_commands_both_execute(_handlers_restored):
    """MEASURED GAP: overlap does not suppress the second execution either.

    The orchestrator takes no lock around ``handle``/``_execute_plan``
    (jarvis/core/orchestrator.py:1079 and :1270 onward), and nothing coalesces
    identical in-flight requests. The handler here blocks until both requests
    have arrived, so the overlap is forced rather than incidental.
    """
    core = _handlers_restored
    calls = {"n": 0}
    barrier = asyncio.Event()
    core.tools._handlers[TOOL] = _counting_handler(calls, barrier=barrier)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://127.0.0.1:8765",
                                 headers=HEADERS) as client:
        first, second = await asyncio.gather(
            client.post("/command", json=_body()),
            client.post("/command", json=_body()),
        )

    assert first.status_code == 200 and second.status_code == 200
    assert calls["n"] == 2, (
        "concurrent duplicates were coalesced -- re-measure before trusting the "
        "gap documented in this file")
    assert first.json()["mission_id"] != second.json()["mission_id"]


# ---------------------------------------------------------------------------
# 6. The desired property, encoded so the fix flips this file loudly.
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    strict=True,
    reason=("MEASURED GAP: /command has no request id and no dedupe, "
            "so a duplicate POST runs the tool twice. Remove this xfail and the "
            "self-check in the test when the backend gains a mechanism that "
            "proves one execution per command."),
)
def test_requirement_a_duplicate_command_executes_exactly_once(_handlers_restored):
    """The acceptance criterion the directive asks for, not yet met.

    This is deliberately the *only* place the desired behaviour is asserted. It
    fails today because ``calls["n"]`` is 2; under ``strict`` xfail that failure
    is reported as ``xfailed`` rather than as a suite error. If the backend is
    fixed, this XPASSes and strict mode turns it into a failure, which is the
    prompt to replace this file's gap documentation with a real guarantee.
    """
    core = _handlers_restored
    calls = {"n": 0}
    core.tools._handlers[TOOL] = _counting_handler(calls)
    client = _client()

    client.post("/command", json=_body())
    client.post("/command", json=_body())

    assert calls["n"] == 1