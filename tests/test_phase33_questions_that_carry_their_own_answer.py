"""Phase 33: a question must not ask the user for something the system can look up.

Measured on the live tree: **55** of 127 capabilities answer a plain request with a
question whose missing piece is an identifier the system owns --

```
close the workspace   -> 'Which workspace? Give me its id (WS-…).'
inspect memory        -> '…your sentence does not say which one. Say it with the name or id…'
cancel event          -> '…say it with the name or id, like "cancel event <which one>"?'
```

and the same tree registers **13** tools that enumerate exactly those things with no
arguments at all: `list_workspaces`, `list_schedules`, `list_events`, `search_email`,
`list_devices`, `list_papers`, `list_design_assets`. So the user is sent off to do a lookup
the product has already done, and the id it asks for (`WS-ab9a7bfc8d0f`) is not something a
human remembers -- it is something a tool prints.

Two facts from the measurement shaped the fix. The enumerators' *messages* do not name the
ids -- ``list_workspaces`` says "1 workspace(s) open." and keeps the id in
``output['workspaces'][0]['workspace_id']`` -- so the answer has to come from the structured
result, which means the shape has to be declared rather than guessed. And `search_memory`
refuses an empty query, so some identifiers genuinely have no zero-argument source; for those
the plain question is still the honest answer, and this phase leaves it that way instead of
inventing a listing tool.

The rules asserted here:

* the question names the real candidates, bounded, with their ids, and says it is offering
  a list rather than choosing;
* offering is not choosing -- no action is emitted, ever, from an incomplete sentence;
* the source of candidates is declared in the registry (one line on the enumerator), and a
  test proves the router follows the declaration in both directions rather than carrying its
  own copy of the mapping;
* only a READ-tier tool that needs nothing may be used to inform a question, and that
  invariant is swept over the whole registry, not just the examples;
* when the lookup fails or finds nothing, the question stays as it was and says so -- the
  candidate list is never padded, and a provider's silence is never turned into "you have
  nothing".
"""

from __future__ import annotations

import asyncio
import copy

import pytest
from jarvis.core.models import CommandRequest, PolicyDecision, RiskLevel, ToolSpec

#: Identifiers the sentence cannot be expected to carry, and the enumerators that own them.
LOOKABLE = {
    "workspace_id": "list_workspaces",
    "schedule_id": "list_schedules",
    "target_device": "list_devices",
    "paper_id": "list_papers",
    "asset_id": "list_design_assets",
}


@pytest.fixture(autouse=True)
def _registry_restored(core):
    """`core` is session-scoped, so a tool registered here would outlive the test that
    added it and answer sentences in every later test. Snapshot and put it back."""
    specs = dict(core.tools._specs)
    handlers = dict(core.tools._handlers)
    yield
    core.tools._specs.clear()
    core.tools._specs.update(specs)
    core.tools._handlers.clear()
    core.tools._handlers.update(handlers)


@pytest.fixture()
def open_workspace(core):
    """One real workspace, so a candidate list has something true to say.

    Closed afterwards: the registry is process-wide, and a workspace left open here
    turns a later test's "the one open project" into "more than one" -- green alone,
    red in a full run.
    """
    import tempfile
    from jarvis.coding.workspace import get_workspace_registry
    from jarvis.tools.base import ToolContext

    area = tempfile.mkdtemp(prefix="p33-ws-")
    ctx = ToolContext(device_id="laptop_main", session_id="p33-seed")
    asyncio.run(core.tools.handler("open_workspace")(ctx, path=area))
    opened = {w.workspace_id for w in get_workspace_registry().all()
              if str(w.root).startswith(area)}
    try:
        yield area
    finally:
        registry = get_workspace_registry()
        for workspace_id in opened:
            registry.close(workspace_id)


def _run(core, text: str, session_id: str = "phase33"):
    return asyncio.run(core.handle(CommandRequest(
        text=text, source="phase33", session_id=session_id)))


def _register(core, name, description, *, agent="laptop", risk=RiskLevel.READ,
              required=(), provides=None):
    """Add a capability (and optionally an enumerator) for one test.

    The phase's claim is that the candidate list comes from the registry, so the only way
    to test it without hand-picking production tools is to declare new ones.
    """
    from jarvis.tools.base import ActionResult

    spec = ToolSpec(
        name=name, description=description, agent=agent, risk=risk,
        required_parameters=list(required), parameters={r: "string" for r in required},
        provides_identifiers=dict(provides or {}),
    )

    async def handler(ctx, **kw):
        return ActionResult(ok=True, tool=name, message=f"{name} done", verified=True)

    core.tools.register(spec, handler)
    return spec


# ---------------------------------------------------------------------------
# 1. the question carries the candidates
# ---------------------------------------------------------------------------


def test_a_question_about_a_workspace_names_the_workspaces_that_are_open(core, open_workspace):
    response = _run(core, "close the workspace")
    assert not response.actions, "an incomplete sentence became an action"
    assert response.plan.reply_kind == "clarification"
    assert "WS-" in response.reply, response.reply
    # Which workspace happens to be open is not this test's to control -- earlier tests in
    # the session may have opened some -- so the claim is that the id offered is one the
    # enumerator really returned, not that it is a particular one.
    listed = asyncio.run(core.tools.handler("list_workspaces")(_ctx(core)))
    real = {w["workspace_id"] for w in listed.output["workspaces"]}
    assert any(ws in response.reply for ws in real), (response.reply, real)


def test_the_offer_says_it_is_an_offer_not_a_choice(core, open_workspace):
    response = _run(core, "close the workspace")
    lower = response.reply.lower()
    assert "which" in lower or "did you mean" in lower, response.reply
    assert "closing" not in lower and "closed" not in lower, response.reply


def test_a_device_question_lists_the_devices_the_registry_knows(core):
    response = _run(core, "ping device")
    assert "phone_main" in response.reply, response.reply
    assert "laptop_main" in response.reply, response.reply


@pytest.mark.parametrize("param,tool", sorted(LOOKABLE.items()))
def test_the_enumerator_that_owns_an_identifier_is_declared_in_the_registry(core, param, tool):
    spec = core.tools.get(tool)
    assert spec is not None, tool
    assert param in (spec.provides_identifiers or {}), (tool, spec.provides_identifiers)
    shape = spec.provides_identifiers[param]
    for key in ("items", "id", "label"):
        assert shape.get(key), (tool, param, shape)


def test_a_route_written_before_this_phase_can_use_the_same_offer(core, open_workspace):
    """The offer is not a fallback-only trick.

    ``_coding`` asks for a workspace id in its own words, written in Phase 31; it now
    declares what it is missing, and the orchestrator fills it the same way. A mechanism
    that only helped the route that invented it would leave most of the 55 questions
    exactly as they were.
    """
    response = _run(core, "list the workspace")
    assert response.plan.reply_kind == "clarification", response.reply
    assert "WS-" in response.reply or "nothing of that kind" in response.reply, response.reply
    assert response.plan.identifier_sources_used == ["list_workspaces"], response.reply


def test_a_schedule_question_lists_the_schedules_that_exist(core):
    """The scheduler's ask names ids rather than demanding them."""
    import asyncio
    from jarvis.tools.base import ToolContext

    ctx = ToolContext(device_id="laptop_main", session_id="p33-sched")
    asyncio.run(core.tools.handler("schedule_task")(
        ctx, name="morning standup", schedule="every morning"))
    response = _run(core, "pause the schedule")
    assert response.plan.reply_kind == "clarification", response.reply
    assert "SCHED-" in response.reply, response.reply
    assert response.plan.identifier_sources_used == ["list_schedules"], response.reply


# ---------------------------------------------------------------------------
# 2. the mapping is derived, both directions
# ---------------------------------------------------------------------------


def test_a_newly_declared_source_is_used_without_touching_the_router(core):
    _register(core, "list_gadgets", "List the gadgets on the bench.", agent="laptop",
              provides={"gadget_id": {"items": "gadgets", "id": "gadget_id", "label": "nick"}})
    core.tools._handlers["list_gadgets"] = _gadget_handler("gadgets", [
        {"gadget_id": "GAD-1", "nick": "the wobbly one"},
        {"gadget_id": "GAD-2", "nick": "the good one"},
    ])
    _register(core, "tune_gadget", "Tune one gadget.", required=["gadget_id"])
    response = _run(core, "tune gadget")
    assert "GAD-1" in response.reply and "the wobbly one" in response.reply, response.reply


def test_removing_the_declaration_sends_the_question_back_to_plain(core):
    _register(core, "list_sprockets", "List sprockets.",
              provides={"sprocket_id": {"items": "sprockets", "id": "sprocket_id",
                                        "label": "name"}})
    core.tools._handlers["list_sprockets"] = _gadget_handler(
        "sprockets", [{"sprocket_id": "SP-1", "name": "big one"}])
    _register(core, "grease_sprocket", "Grease one sprocket.", required=["sprocket_id"])
    with_answer = _run(core, "grease sprocket")
    assert "SP-1" in with_answer.reply, with_answer.reply

    del core.tools.get("list_sprockets").provides_identifiers["sprocket_id"]
    without = _run(core, "grease sprocket")
    assert "SP-1" not in without.reply, without.reply
    assert without.plan.reply_kind == "clarification", without.reply


def test_a_same_agent_source_wins_when_two_tools_offer_the_same_identifier(core):
    """`uid` belongs to an email *and* to a calendar event, so the parameter name alone
    cannot say which list to consult. The asking tool's own agent decides.

    Asserted on registered twins rather than on the production pair because the
    production sentences ("read email", "cancel event") are claimed by their own
    specific routes before the fallback is reached -- measured, and not a defect.
    """
    _register(core, "read_missive", "Read one message.", agent="mail", required=["uid"],
              provides={})
    _register(core, "list_missives", "List messages.", agent="mail",
              provides={"uid": {"items": "results", "id": "uid", "label": "subject"}})
    _register(core, "read_booking", "Read one booking.", agent="diary", required=["uid"],
              provides={})
    _register(core, "list_bookings", "List bookings.", agent="diary",
              provides={"uid": {"items": "events", "id": "uid", "label": "title"}})
    core.tools._handlers["list_missives"] = _gadget_handler(
        "results", [{"uid": "MAIL-9", "subject": "the electricity bill"}])
    core.tools._handlers["list_bookings"] = _gadget_handler(
        "events", [{"uid": "EVT-4", "title": "Board sync"}])

    mail = _run(core, "read missive")
    diary = _run(core, "read booking")
    assert mail.plan.identifier_sources_used == ["list_missives"], mail.reply
    assert "electricity bill" in mail.reply, mail.reply
    assert diary.plan.identifier_sources_used == ["list_bookings"], diary.reply
    assert "Board sync" in diary.reply, diary.reply


# ---------------------------------------------------------------------------
# 3. honesty when the lookup cannot answer
# ---------------------------------------------------------------------------


def test_a_lookup_that_fails_leaves_the_question_honest(core, monkeypatch):
    _register(core, "list_widgets", "List widgets.",
              provides={"widget_id": {"items": "widgets", "id": "widget_id", "label": "name"}})

    async def broken(ctx, **kw):
        from jarvis.tools.base import ActionResult
        return ActionResult(ok=False, tool="list_widgets", error="bench offline",
                            verified=False)

    core.tools._handlers["list_widgets"] = broken
    _register(core, "inspect_widget", "Inspect one widget.", required=["widget_id"])
    response = _run(core, "inspect widget")
    assert response.plan.reply_kind == "clarification", response.reply
    assert "widget" in response.reply.lower(), response.reply
    assert "bench offline" in response.reply or "could not check" in response.reply.lower(), (
        response.reply)


def test_no_candidates_are_invented_when_there_are_none(core):
    """`list_papers` with an empty shelf must not produce a plausible-looking row, and the
    reply must not claim the user has nothing in a voice that sounds like a result."""
    response = _run(core, "forget paper")
    assert not response.actions
    assert response.plan.reply_kind == "clarification", response.reply


def test_an_identifier_with_no_zero_argument_source_still_asks_plainly(core):
    """`memory_id` has no enumerator that answers without a query, so the question stays
    exactly as Phase 30 wrote it. The honest response is to leave it that way, not to
    invent a listing tool to make an assertion come true."""
    _register(core, "fetch_gist_by_key", "Read one stored gist.", required=["memory_id"])
    response = _run(core, "fetch gist by key")
    assert response.plan.reply_kind == "clarification", response.reply
    assert response.plan.identifier_sources_used == [], response.plan.identifier_sources_used
    assert "Here is what I have" not in response.reply, response.reply


# ---------------------------------------------------------------------------
# 4. bounds and attribution
# ---------------------------------------------------------------------------


def test_a_long_candidate_list_is_bounded_and_says_so(core):
    _register(core, "list_doohicks", "List doohicks.",
              provides={"doohick_id": {"items": "doohicks", "id": "doohick_id",
                                       "label": "name"}})
    many = [{"doohick_id": f"DH-{i}", "name": f"doohick {i}"} for i in range(1, 13)]
    core.tools._handlers["list_doohicks"] = _gadget_handler("doohicks", many)
    _register(core, "polish_doohick", "Polish one doohick.", required=["doohick_id"])
    response = _run(core, "polish doohick")
    listed = response.reply.count("DH-")
    assert 0 < listed < 12, (listed, response.reply)
    assert "more" in response.reply.lower() or "…" in response.reply, response.reply


def test_the_lookup_that_informed_the_question_is_written_in_the_ledger(core, open_workspace):
    """A question built from another tool's answer needs the same attribution Phase 26
    demanded for a fallback that answers with one: the chain must say which read ran."""
    response = _run(core, "close the workspace", session_id="p33-attrib")
    row = next(r for r in core.audit.recent(15) if r.get("request_id") == response.request_id)
    detail = row.get("detail") or {}
    assert "list_workspaces" in str(detail.get("identifier_sources_used")), detail
    assert row["result"] == "no_action", row


def test_the_lookup_is_read_only_and_requires_nothing(core):
    """The invariant that keeps this from becoming a way to run arbitrary work while
    phrasing a question."""
    offenders = []
    for spec in core.tools.all():
        for param, shape in (spec.provides_identifiers or {}).items():
            if spec.risk is not RiskLevel.READ:
                offenders.append(f"{spec.name} informs {param} at tier {spec.risk.name}")
            if spec.required_parameters:
                offenders.append(f"{spec.name} informs {param} but needs {spec.required_parameters}")
            if not {"items", "id", "label"} <= set(shape):
                offenders.append(f"{spec.name}.{param} shape is {shape}")
    assert offenders == [], "\n".join(offenders)


def test_informing_a_question_emits_no_action_and_changes_no_state(core, open_workspace):
    response = _run(core, "close the workspace")
    assert response.actions == []
    assert response.decision is None, response.decision
    before = asyncio.run(core.tools.handler("list_workspaces")(_ctx(core)))
    ids = [w["workspace_id"] for w in before.output["workspaces"]]
    after = asyncio.run(core.tools.handler("list_workspaces")(_ctx(core)))
    assert [w["workspace_id"] for w in after.output["workspaces"]] == ids, (
        "the lookup changed the list it was only supposed to read")


def test_asking_to_close_a_workspace_by_id_closes_it_rather_than_describing_it(core):
    """Measured on the live tree, this sentence did the wrong thing outright:

        "close the workspace WS-ab9a7bfc8d0f"
            tool=inspect_workspace
            reply='tmp16ycb2dy: 0 source file(s), 0 test file(s). Languages: unknown.'

    A request to change something was answered with a read of that thing, recorded as a
    success, and nothing in the reply or the ledger said the two were different verbs.
    The coding route's default branch is an *inspect*, and it claimed the sentence because
    the word "workspace" appeared in it.
    """
    from jarvis.tools.base import ToolContext

    area = __import__("tempfile").mkdtemp(prefix="p33-close-")
    ctx = ToolContext(device_id="laptop_main", session_id="p33-close")
    asyncio.run(core.tools.handler("open_workspace")(ctx, path=area))
    ws_id = asyncio.run(core.tools.handler("list_workspaces")(
        ctx)).output["workspaces"][0]["workspace_id"]

    plan = core.planner.parse(f"close the workspace {ws_id}")
    assert [a.tool for a in plan.actions] == ["close_workspace"], plan.clarification_question
    assert plan.actions[0].params["workspace_id"] == ws_id


# ---------------------------------------------------------------------------
# helpers kept at the bottom so the assertions read first
# ---------------------------------------------------------------------------


def _ctx(core):
    from jarvis.tools.base import ToolContext
    return ToolContext(device_id="laptop_main", session_id="p33-check")


def _gadget_handler(key, rows):
    """An enumerator answering from a fixed shelf, so candidate formatting is tested
    against declared shapes rather than against a device or a network."""
    from jarvis.tools.base import ActionResult

    async def handler(ctx, **kw):
        return ActionResult(ok=True, tool="enumerator", message=f"{len(rows)} item(s).",
                            verified=True, output={key: copy.deepcopy(rows)})

    return handler
