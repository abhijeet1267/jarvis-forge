"""Phase 30: a capability JARVIS has must be reachable from a sentence.

Measured on the live tree before anything was changed: **127** tools are registered and
**28** of them appear nowhere in ``planner.py``, so no typed sentence could ask for them.
The answer a user got for a capability that exists was:

    >>> "list the workspaces"
        intent=unknown  reply_kind=not_understood
        reply="I understood the words but not the goal yet. Try things like ..."

That sentence is true of the *router* and reads as "I can't". Two things were wrong: the
vocabulary was smaller than the system, and where a capability needed an argument the
sentence did not supply, the message said nothing about the capability at all.

The repair is derived from the registry rather than hand-written per tool, because a
hand-written route is what produced the last four phases' bugs:

* a tool whose own name words are all in the sentence is reached, if the sentence also
  supplies what the tool requires or the tool requires nothing;
* parameters are validated by ``ToolRegistry.validate_params`` before an action is
  emitted, so the route structurally cannot claim an action it cannot fill;
* when the argument is missing, the question is phrased from the tool's own
  ``description`` -- human language the registry already carries -- and never names an
  internal field;
* two tools matching the same words asks which, and nothing matching asks nothing.

Reaching a tool does not change its permission verdict: the same funnel decides that.
That is asserted here, and it is what caught **30-A** -- ``sweep_expired_memories``
deletes memories permanently, was tiered LOW with no confirmation, and was unreachable
only because no sentence could name it. Making it typeable would have made an
unconfirmed deletion live.
"""

from __future__ import annotations

import asyncio
import re

import pytest
from jarvis.core.models import CommandRequest, PolicyDecision, RiskLevel, ToolSpec

#: The tools that require nothing from the user, so a sentence that names them is
#: complete on its own. Measured: these five were registered and unreachable.
NO_PARAM_TOOLS = [
    ("list the workspaces", "list_workspaces"),
    ("what design integrations are available", "design_integrations"),
    ("list car metrics", "list_car_metrics"),
    ("sweep expired memories", "sweep_expired_memories"),
    ("approve the pairing", "approve_pairing"),
]

#: Every tool that appeared nowhere in ``planner.py`` when the phase opened. Each
#: sentence says the tool's own name words, so the capability is genuinely identified;
#: most of these need an argument, which is why the contract is "reached or asked
#: about", never "not understood".
UNREACHABLE_AT_START = [
    ("search events about standup", "search_events"),
    ("read the design asset ASSET-1a2b3c", "read_design_asset"),
    ("delete the design asset ASSET-1a2b3c", "delete_design_asset"),
    ("analyze the design integrations", "design_integrations"),
    ("list the car metrics", "list_car_metrics"),
    ("get the device capabilities of phone_main", "get_device_capabilities"),
    ("ping device phone_main", "ping_device"),
    ("inspect memory MEM-12", "inspect_memory"),
    ("sweep the expired memories", "sweep_expired_memories"),
    ("list the open workspaces", "list_workspaces"),
    ("close the workspace WS-3", "close_workspace"),
    ("get the schedule JOB-4", "get_schedule"),
    ("run the schedule JOB-4 now", "run_schedule_now"),
    ("read email 41", "read_email"),
    ("update event 12", "update_event"),
    ("cancel event 12", "cancel_event"),
    ("add a paper called Growing Agents", "add_paper"),
    ("forget paper PAPER-7", "forget_paper"),
    ("add a research note that the gate is the authority", "add_research_note"),
    ("extract the paper methods of PAPER-7", "extract_paper_methods"),
    ("extract the paper datasets of PAPER-7", "extract_paper_datasets"),
    ("extract the paper limitations of PAPER-7", "extract_paper_limitations"),
    ("link the asset to the project", "link_asset_to_project"),
    ("link the paper to the project", "link_paper_to_project"),
    ("add a project node", "add_project_node"),
    ("propose a code change", "propose_code_change"),
    ("apply the code change", "apply_code_change"),
    ("approve the pairing", "approve_pairing"),
]

#: An internal field name must never be the user's answer (29-B's rule, applied to
#: sentences this phase makes reachable).
_SNAKE = re.compile(r"\b[a-z]+_[a-z]+(?:_[a-z]+)*\b")


def _run(core, text: str, session_id: str = "phase30"):
    return asyncio.run(core.handle(CommandRequest(
        text=text, source="phase30", session_id=session_id)))


def _parse(core, text: str):
    return core.planner.parse(text)


def _handlers(core, tools):
    """Counting handlers that succeed, restored by the autouse fixture."""
    calls = {t: 0 for t in tools}

    def build(tool):
        async def handler(ctx, **kw):
            calls[tool] += 1
            from jarvis.tools.base import ActionResult
            return ActionResult(ok=True, tool=tool, message=f"{tool} done", verified=True)
        return handler

    for t in tools:
        core.tools._handlers[t] = build(t)
    return calls


def _register(core, name, description, *, risk=RiskLevel.READ, required=(), tags=()):
    """Add a capability to the registry for the duration of one test.

    The phase's claim is that reachability comes from the registry, so the only honest
    way to test a tool that does not exist yet is to register it -- which is also how
    the ambiguity and parameter-filling contracts get pinned without arguing with the
    forty hand-written routes ahead of the fallback in the chain.
    """
    from jarvis.tools.base import ActionResult

    spec = ToolSpec(
        name=name, description=description, agent="laptop", risk=risk,
        required_parameters=list(required), parameters={r: "string" for r in required},
        tags=list(tags),
    )

    async def handler(ctx, **kw):
        return ActionResult(ok=True, tool=name, message=f"{name} done", verified=True)

    core.tools.register(spec, handler)
    return spec


# ---------------------------------------------------------------------------
# 1. a capability that needs nothing is reachable by saying its name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,tool", NO_PARAM_TOOLS)
def test_a_named_capability_that_needs_no_argument_is_planned(core, text, tool):
    plan = _parse(core, text)
    assert [a.tool for a in plan.actions] == [tool], (plan.intent, plan.reply_kind, plan.direct_reply)
    assert plan.actions[0].params == {}, "a tool that requires nothing must not be given invented values"


def test_the_reached_capability_actually_runs(core):
    calls = _handlers(core, ["list_workspaces"])
    response = _run(core, "list the workspaces")
    assert calls["list_workspaces"] == 1, response.reply
    assert response.actions and response.actions[0].ok is True


def test_reachability_does_not_change_a_permission_verdict(core):
    """The gate is the only place authority is decided; a new route must not move it."""
    for text, tool in NO_PARAM_TOOLS:
        spec = core.tools.get(tool)
        decision = _run(core, text).decision
        if spec.requires_confirmation:
            assert decision is PolicyDecision.CONFIRM, (tool, decision)
        else:
            assert decision is PolicyDecision.EXECUTE, (tool, decision)


def test_a_gated_capability_runs_nothing_before_anyone_approves(core):
    """``approve_pairing`` issues a device key. Naming it must not be the same as
    approving it -- the fallback reaches the tool, the gate still stops it."""
    calls = _handlers(core, ["approve_pairing"])
    response = _run(core, "approve the pairing")
    assert calls["approve_pairing"] == 0, response.reply
    assert response.needs_confirmation is True
    assert response.decision is PolicyDecision.CONFIRM


# ---------------------------------------------------------------------------
# 2. 30-A -- the phase's own safety measurement
# ---------------------------------------------------------------------------


def test_deleting_memories_is_tiered_as_a_deletion(core):
    """Measured before this phase: risk LOW, ``requires_confirmation`` false, while its
    sibling fifteen lines away in the same file -- ``clear_memory_scope``, which deletes
    a scope instead of a set of expired rows -- is DESTRUCTIVE and confirms. Both are
    ``reversible=False``. The difference was never the action; it was that no sentence
    could reach one of them."""
    spec = core.tools.get("sweep_expired_memories")
    assert spec.risk is RiskLevel.DESTRUCTIVE, spec.risk
    assert spec.requires_confirmation is True
    assert "destructive" in spec.tags


def test_a_sentence_that_deletes_memories_asks_first(core):
    calls = _handlers(core, ["sweep_expired_memories"])
    response = _run(core, "sweep expired memories")
    assert calls["sweep_expired_memories"] == 0, response.reply
    assert response.decision is PolicyDecision.CONFIRM, response.reply


def test_the_two_ways_to_delete_memories_now_agree(core):
    sweep = core.tools.get("sweep_expired_memories")
    clear = core.tools.get("clear_memory_scope")
    assert sweep.risk is clear.risk, (sweep.risk, clear.risk)
    assert sweep.requires_confirmation is clear.requires_confirmation


# ---------------------------------------------------------------------------
# 3. a capability that needs an argument asks for it, in human words
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,tool", UNREACHABLE_AT_START)
def test_a_named_capability_is_never_answered_as_unrecognised(core, text, tool):
    """The defect was not that JARVIS declined; it was that the decline did not mention
    the capability it has. Either the action is planned or a question is asked."""
    plan = _parse(core, text)
    assert plan.reply_kind != "not_understood", (text, plan.direct_reply)
    assert plan.actions or plan.reply_kind == "clarification", (text, plan.direct_reply)


def test_an_under_specified_request_asks_and_plans_nothing(core):
    _register(core, "sample_widget", "Take one reading from a widget.", required=["widget_id"])
    plan = _parse(core, "sample the widget")
    assert plan.actions == [], [a.tool for a in plan.actions]
    assert plan.reply_kind == "clarification"
    assert "?" in plan.clarification_question


def test_the_question_names_the_capability_in_its_own_words(core):
    _register(core, "sample_widget", "Take one reading from a widget.", required=["widget_id"])
    plan = _parse(core, "sample the widget")
    assert "reading" in plan.clarification_question.lower(), plan.clarification_question
    assert "widget" in plan.clarification_question.lower()


def test_no_question_from_the_fallback_names_an_internal_field(core):
    """29-B, applied structurally: the description is the vocabulary, not the parameter."""
    _register(core, "sample_widget", "Take one reading from a widget.", required=["widget_id"])
    plan = _parse(core, "sample the widget")
    assert not _SNAKE.search(plan.clarification_question), plan.clarification_question
    assert "missing required parameter" not in plan.clarification_question


def test_a_missing_argument_is_not_reported_as_a_success(core):
    _register(core, "sample_widget", "Take one reading from a widget.", required=["widget_id"])
    response = _run(core, "sample the widget")
    assert response.plan.reply_kind == "clarification", response.reply
    assert not response.actions


def test_the_question_is_in_the_ledger_as_a_question(core):
    """Nothing ran, so the token is ``no_action`` -- the convention Phase 29 pinned --
    and ``detail.reply_kind`` is what keeps a question distinguishable from a refusal
    in the chain."""
    _register(core, "sample_widget", "Take one reading from a widget.", required=["widget_id"])
    response = _run(core, "sample the widget")
    row = next(r for r in core.audit.recent(15) if r.get("request_id") == response.request_id)
    assert row["result"] == "no_action", row
    assert (row.get("detail") or {}).get("reply_kind") == "clarification", row


# ---------------------------------------------------------------------------
# 4. what the sentence does supply is used
# ---------------------------------------------------------------------------


def test_an_id_in_the_sentence_reaches_the_tool_that_needs_it(core):
    _register(core, "sample_widget", "Take one reading from a widget.", required=["widget_id"])
    plan = _parse(core, "sample the widget WIDGET-9")
    assert [a.tool for a in plan.actions] == ["sample_widget"], plan.direct_reply
    assert plan.actions[0].params == {"widget_id": "WIDGET-9"}


def test_a_bare_number_in_the_sentence_reaches_the_tool_that_needs_it(core):
    _register(core, "sample_widget", "Take one reading from a widget.", required=["widget_id"])
    plan = _parse(core, "sample widget 3")
    assert [a.params for a in plan.actions] == [{"widget_id": "3"}], plan.direct_reply


def test_a_quoted_value_in_the_sentence_reaches_the_tool_that_needs_it(core):
    _register(core, "sample_widget", "Take one reading from a widget.", required=["widget_id"])
    plan = _parse(core, 'sample widget "last tuesday"')
    assert [a.params for a in plan.actions] == [{"widget_id": "last tuesday"}], plan.direct_reply


def test_a_second_required_parameter_is_asked_for_not_invented(core):
    _register(core, "pair_two_things", "Join two named things.", required=["first", "second"])
    plan = _parse(core, "pair two things FIRST-1")
    assert plan.actions == [], [a.tool for a in plan.actions]
    assert plan.reply_kind == "clarification"


def test_the_fallback_never_emits_an_action_the_registry_would_reject(core):
    """``validate_params`` is the authority the route asks before it emits, so 29's bug
    cannot be reintroduced here by a future tool with a future parameter."""
    for text, _tool in UNREACHABLE_AT_START:
        plan = _parse(core, text)
        for action in plan.actions:
            problems = core.tools.validate_params(action.tool, action.params)
            assert problems == [], (text, action.tool, action.params, problems)


# ---------------------------------------------------------------------------
# 5. what the fallback refuses to do
# ---------------------------------------------------------------------------


def test_two_capabilities_with_the_same_words_ask_which(core):
    _register(core, "widget_report", "Write a widget report.", risk=RiskLevel.READ)
    _register(core, "widget_archive", "Archive the widget run.", risk=RiskLevel.READ)
    plan = _parse(core, "the widget report archive")
    assert plan.actions == [], [a.tool for a in plan.actions]
    assert plan.reply_kind == "clarification", plan.direct_reply
    assert "report" in plan.clarification_question.lower()
    assert "archive" in plan.clarification_question.lower()


def test_one_name_word_is_not_enough_to_reach_a_capability(core):
    """Conservatism is the point: a keyword must not plan a tool nobody described."""
    _register(core, "widget_gizmo", "Gizmo a widget.")
    plan = _parse(core, "the gizmo")
    assert plan.actions == [], [a.tool for a in plan.actions]


def test_a_sentence_that_names_nothing_is_still_answered_honestly(core):
    plan = _parse(core, "pleach the mimbolas")
    assert plan.reply_kind == "not_understood"
    assert "understood the words" in plan.direct_reply


def test_an_existing_route_still_answers_before_the_fallback(core):
    """The fallback is last in the chain; it must not steal a sentence a specific rule
    already handles, which is where the case-by-case vocabulary does better."""
    assert [a.tool for a in _parse(core, "what is my battery").actions] == ["battery_status"]
    assert [a.tool for a in _parse(core, "read the readme").actions] == ["read_file"]


def test_singular_and_plural_name_words_reach_the_same_capability(core):
    plans = [_parse(core, "cancel the event 12"), _parse(core, "cancel the events 12")]
    for plan in plans:
        assert [a.tool for a in plan.actions] == ["cancel_event"], plan.direct_reply
        assert plan.actions[0].params == {"uid": "12"}


def test_the_fallback_does_not_reach_a_tool_that_is_not_registered(core):
    plan = _parse(core, "frobnicate the warp core")
    assert plan.actions == []
    assert plan.reply_kind == "not_understood"


# ---------------------------------------------------------------------------
# 6. the phase's own claim, measured against the registry
# ---------------------------------------------------------------------------


def test_every_registered_tool_is_reachable_when_its_own_name_is_said(core):
    """The measured version of the phase's claim, and deliberately not a source grep.

    A registry-derived route means a tool can be reachable while its name appears
    nowhere in ``planner.py`` -- that is the point -- so the honest test spells out
    each tool's own name words and asks whether the router does anything with them.
    The answer may be an action or a question about the missing argument; it may not
    be "I understood the words but not the goal".
    """
    dead = []
    for spec in core.tools.all():
        sentence = " ".join(spec.name.split("_"))
        plan = _parse(core, sentence)
        if plan.reply_kind == "not_understood":
            dead.append(f"{spec.name}: {sentence!r} -> {plan.direct_reply[:60]!r}")
    assert dead == [], "\n".join(dead)


# ---------------------------------------------------------------------------
# 7. the routes that already claimed the sentence
# ---------------------------------------------------------------------------
#
# Adding a fallback was the easy half. Measured on the live tree, four specific rules
# answered first, and three of them were saying something untrue:
#
#   "cancel event 12"   -> declined: 'I can read your schedule, find free time, and
#                          create or cancel events.'   (cancel_event was unreachable)
#   "list the workspace"-> BLOCK: "missing required parameter 'workspace_id'"
#   "get the device capabilities of phone_main"
#                       -> the 127-name help listing, because "capabilities" is a
#                          trigger word for the help rule
#   "list car metrics"  -> get_car_status (an honest alternative reading, so this one
#                          is left as a specificity gap and recorded, not fixed)


def test_a_sentence_that_cancels_an_event_cancels_it(core):
    """The calendar route promised cancellation in its own decline text while
    ``cancel_event`` was a tool no sentence could reach -- an advertisement the
    router could not honour."""
    plan = _parse(core, "cancel event 12")
    assert [a.tool for a in plan.actions] == ["cancel_event"], plan.direct_reply
    assert plan.reply_kind != "declined"


def test_a_sentence_that_updates_an_event_reaches_the_update(core):
    plan = _parse(core, "update event 12")
    assert [a.tool for a in plan.actions] == ["update_event"], plan.direct_reply


def test_a_cancel_still_needs_the_users_approval(core):
    calls = _handlers(core, ["cancel_event"])
    response = _run(core, "cancel event 12")
    assert calls["cancel_event"] == 0, response.reply
    assert response.decision is PolicyDecision.CONFIRM, response.reply


def test_help_is_still_help(core):
    """Asking what JARVIS can do is not the same as naming one capability."""
    plan = _parse(core, "what can you do")
    assert plan.reply_kind == "declined" or "open and close applications" in plan.direct_reply


def test_a_device_capability_sentence_is_not_answered_with_the_tool_list(core):
    plan = _parse(core, "get the device capabilities of phone_main")
    assert [a.tool for a in plan.actions] == ["get_device_capabilities"], plan.direct_reply
    assert plan.actions[0].params == {"target_device": "phone_main"}


def test_a_workspace_request_asks_which_workspace_rather_than_naming_the_field(core):
    """29-B through the hole 29-C admitted: the route built ``params`` in a variable,
    so the static sweep could not see it emitting an empty required parameter."""
    response = _run(core, "list the workspace")
    assert "missing required parameter" not in response.reply, response.reply
    assert not response.actions, [a.tool for a in response.actions]
    assert "workspace" in response.reply.lower()


@pytest.mark.parametrize("text", [
    "run the tests", "commit this with message hello", "report on the workspace",
    "inspect the workspace", "summarise the repository",
])
def test_no_coding_request_is_answered_with_a_parameter_name(core, text):
    response = _run(core, text)
    assert "missing required parameter" not in response.reply, response.reply


def test_no_tool_in_the_registry_is_reachable_only_to_leak_its_field_names(core):
    """The structural version of 29-C.

    For every registered tool, a sentence built from that tool's own name words plus
    three value shapes -- a quoted phrase, an id, a number -- is parsed, and no action
    it emits may be one the registry itself would refuse. This is the sweep that would
    have caught ``inspect_workspace`` before a human had to type the sentence.
    """
    offenders = []
    for spec in core.tools.all():
        sentence = " ".join(spec.name.split("_")) + ' WIDGET-1 42 "the rest"'
        plan = _parse(core, sentence)
        for action in plan.actions:
            problems = core.tools.validate_params(action.tool, action.params)
            if problems:
                offenders.append(f"{spec.name}: {sentence!r} -> {action.tool} "
                                 f"{action.params} {problems}")
        if "missing required parameter" in (plan.direct_reply or ""):
            offenders.append(f"{spec.name}: {sentence!r} -> leaked {plan.direct_reply!r}")
    assert offenders == [], "\n".join(offenders)
