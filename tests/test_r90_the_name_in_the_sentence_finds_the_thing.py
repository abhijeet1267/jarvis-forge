"""The sentence's own words must be enough to find the thing it names.

**A calendar request became a file move, and then became a dead end.**

Measured on the live tree, in three steps:

1. *"move my Design Review event to tomorrow at 3pm"* was planned as
   ``move_file(source="my Design Review event", destination="tomorrow at 3pm")``.
   A file rename proposed for a calendar request, with the person's approval
   spent on it. The model planner was shown all 137 proposable tools -- about
   17,000 characters of one-per-line definitions -- and asked to pick.
2. With only the calendar tools visible, the same sentence came back as
   ``search_events``: the right first step. So the fix was to narrow the
   catalogue, and the narrowing is :mod:`jarvis.core.domain`.
3. *"cancel my Design Review event"* then asked which event was meant, and
   answered its own question with *"I checked, and there is nothing of that
   kind right now"* -- because the identifier source, ``list_events``, was
   run with **no arguments**, and no arguments means *today*, and the event
   was six days out.

The rules asserted here:

* a calendar request is never planned as a file operation;
* the catalogue the model sees is narrowed to the domain the sentence names,
  and the full catalogue is used when the sentence names no domain or more
  than one (never a wrong narrowing);
* the planner is told the current date and time, and is told the machine's
  own time format, so "tomorrow at 3pm" becomes a concrete timestamp instead
  of a phrase the tool cannot read;
* an identifier source is **searched by the name the person gave**, so the
  lookup finds an event that is not scheduled for today;
* when exactly one thing matches that name, the plan is completed rather
  than asked about -- and the completion still goes through the permission
  gate;
* when several things match, the question still stands: a name that matches
  nine events identifies none of them, and picking one would be a coin toss
  presented as an answer.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from jarvis.core.domain import detect_domains, narrow_domain
from jarvis.core.models import CommandRequest, RiskLevel
from jarvis.core.planner import RulePlanner


def _run(core, text: str, session_id: str = "r90"):
    return asyncio.run(core.handle(CommandRequest(
        text=text, source="r90", session_id=session_id)))


# ---------------------------------------------------------------------------
# 1. the rule planner must not plan a calendar request as a file operation
# ---------------------------------------------------------------------------

def test_a_calendar_request_is_not_planned_as_a_file_move(core):
    """``move my Design Review event to tomorrow at 3pm`` was ``move_file``.

    The file rule ran before the calendar rule, so a sentence that mentioned
    a file verb and a calendar noun was read as a file operation. The file
    rule now steps aside when the sentence names a calendar thing.
    """
    plan = core.planner.parse("move my Design Review event to tomorrow at 3pm", {})
    tools = [action.tool for action in plan.actions]
    assert "move_file" not in tools, plan.actions
    assert "copy_file" not in tools, plan.actions


def test_a_plain_file_request_still_uses_the_file_rule(core):
    """The deferral must not break the rule it defers for."""
    plan = core.planner.parse("move notes.txt to /Users/me/Desktop/", {})
    tools = [action.tool for action in plan.actions]
    assert "move_file" in tools, plan.actions


def test_a_file_request_whose_title_mentions_a_calendar_word_still_moves(core):
    """The deferral is on the subject's *kind*, not on any calendar-ish word.

    A file called *meeting-notes.txt* is a file. The check looks for a word
    that names a calendar thing on its own -- "event", "meeting" -- and a
    compound name is not that.
    """
    plan = core.planner.parse("move event-log.txt to /Users/me/Desktop/", {})
    tools = [action.tool for action in plan.actions]
    assert "move_file" in tools, plan.actions


# The move-branch deferral above was only half the story: the *other*
# file-mutating routes (compress, delete, copy) had no calendar deferral, so a
# calendar request phrased with one of those verbs still became a proposal to
# mutate a file named after the event. Each must defer to the calendar rule,
# the same way move does.

def test_a_calendar_request_is_not_planned_as_a_file_compression(core):
    """``archive my standup meeting`` was ``compress_path("my standup meeting")``."""
    plan = core.planner.parse("archive my standup meeting", {})
    tools = [action.tool for action in plan.actions]
    assert "compress_path" not in tools, plan.actions


def test_compress_an_event_defers_to_the_calendar_rule(core):
    plan = core.planner.parse("compress that meeting", {})
    tools = [action.tool for action in plan.actions]
    assert "compress_path" not in tools, plan.actions
    # It lands in the calendar domain (a question/clarification), not a file op.
    assert plan.intent == "calendar", plan


def test_delete_an_event_is_not_a_file_delete(core):
    """``delete my Design Review event`` was ``delete_file("my Design Review event")``.

    A file literally named after the event must not be the deletion target.
    """
    plan = core.planner.parse("delete my Design Review event", {})
    tools = [action.tool for action in plan.actions]
    assert "delete_file" not in tools, plan.actions
    assert plan.intent == "calendar", plan


def test_copy_an_event_is_not_a_file_copy(core):
    plan = core.planner.parse("copy that event to notes", {})
    tools = [action.tool for action in plan.actions]
    assert "copy_file" not in tools, plan.actions


def test_a_file_named_after_an_event_still_compresses_and_deletes(core):
    """The deferral is on the subject's kind; a real file still works.

    A folder genuinely called "events/" and a file "meeting-notes.txt" are
    files, and must still compress/delete/copy even though their names carry a
    calendar word.
    """
    assert "compress_path" in _tools(core.planner.parse("archive that folder", {}))
    assert "compress_path" in _tools(core.planner.parse("compress the events folder", {}))
    assert "delete_file" in _tools(core.planner.parse("delete meeting-notes.txt", {}))
    assert "copy_file" in _tools(core.planner.parse("copy event-log.txt to backup", {}))
    assert "move_file" in _tools(core.planner.parse("move event-log.txt to backup", {}))


# The same wrong-target-mutation class extends to the other managed domains:
# design assets, papers, schedules, and workspaces each have a real backend
# delete/manage tool, but the file-mutating routes used to steal the phrasing
# first and mutate a file named after the entry. Each must defer (no mutating
# file tool), the same way the calendar entry does.

_FILE_MUTATORS = ("compress_path", "move_file", "copy_file", "delete_file",
                  "write_file", "append_file")


def _no_file_mutation(plan):
    assert not any(t in _FILE_MUTATORS for t in _tools(plan)), plan.actions


def test_delete_a_design_asset_is_not_a_file_delete(core):
    """``delete the design asset`` was ``delete_file("the design asset")``."""
    _no_file_mutation(core.planner.parse("delete the design asset", {}))


def test_archive_a_design_asset_is_not_a_file_compression(core):
    _no_file_mutation(core.planner.parse("archive that design asset", {}))


def test_delete_and_compress_a_paper_are_not_file_ops(core):
    """``compress that paper`` / ``delete the paper about X`` must not touch a file."""
    _no_file_mutation(core.planner.parse("compress that paper", {}))
    _no_file_mutation(core.planner.parse("delete the paper about growing agents", {}))


def test_move_a_paper_is_not_a_file_move(core):
    _no_file_mutation(core.planner.parse("move that paper to archive", {}))


def test_delete_a_schedule_is_not_a_file_delete(core):
    _no_file_mutation(core.planner.parse("delete the morning briefing schedule", {}))


def test_delete_a_workspace_is_not_a_file_delete(core):
    _no_file_mutation(core.planner.parse("delete that workspace", {}))


def test_a_file_named_after_a_domain_entry_still_works(core):
    """A file whose name carries a domain word is still a file (extension wins).

    "paper-notes.txt", "schedule.csv", "workspace.yaml", "asset.png" are files;
    the deferral must not stop them from being deleted/moved/compressed.
    """
    assert "delete_file" in _tools(core.planner.parse("delete paper-notes.txt", {}))
    assert "delete_file" in _tools(core.planner.parse("delete schedule.csv", {}))
    assert "move_file" in _tools(core.planner.parse("move workspace.yaml to backup", {}))
    assert "compress_path" in _tools(core.planner.parse("compress the asset.png", {}))


def _tools(plan) -> list:
    return [a.tool for a in plan.actions]


def test_the_calendar_rule_declines_a_sentence_it_cannot_act_on(core):
    """The rules know the difference between "I cannot do that" and "ask the model".

    A calendar sentence the rules cannot act on used to answer with a flat
    "declined", which the orchestrator answers with that sentence and
    nothing else. The model never saw it. The route now says
    ``not_understood``, which is the kind the orchestrator routes to the
    model.
    """
    plan = core.planner.parse("change the Design Review event to 3pm tomorrow", {})
    assert plan.reply_kind == "not_understood", plan
    assert not plan.needs_clarification, plan


# ---------------------------------------------------------------------------
# 2. the catalogue the model sees is narrowed to the domain
# ---------------------------------------------------------------------------

def test_a_calendar_sentence_names_the_calendar_domain():
    assert narrow_domain("move my Design Review event to tomorrow at 3pm") == "calendar"
    assert narrow_domain("what is on my calendar tomorrow") == "calendar"


def test_a_sentence_that_names_no_domain_is_not_narrowed():
    assert narrow_domain("do the thing we discussed") is None


def test_a_sentence_that_names_two_domains_is_not_narrowed():
    """Showing the full catalogue is safer than hiding the right tool."""
    assert narrow_domain("email me the calendar for tomorrow") is None


def test_an_event_title_is_not_a_domain():
    """*Design Review* is an event's name, not a request about design.

    The design domain's words had to be multi-word phrases, because "Design
    Review" is a calendar event that happens to be called *Design*.
    """
    assert detect_domains("Design Review") == []
    assert narrow_domain("Design Review") is None


def test_a_calendar_word_beats_a_scheduler_word():
    """*schedule a meeting* is a calendar request, not a reminder."""
    assert narrow_domain("schedule a meeting tomorrow at 3pm") == "calendar"


def test_a_scheduler_word_on_its_own_is_the_scheduler():
    assert narrow_domain("remind me every morning to stretch") == "scheduler"


def test_an_email_word_beats_a_scheduler_word():
    """*remind me about the email* is an email request, not a reminder.

    The sentence names two domains, so the full catalogue is used -- but the
    email domain is the one the priority order puts first, so a later
    narrowing pass that has to choose lands on email.
    """
    assert detect_domains("remind me about the email from Priya") == ["email", "scheduler"]
    assert narrow_domain("email me the report") == "email"


# ---------------------------------------------------------------------------
# 3. the planner is told the time, and told the format
# ---------------------------------------------------------------------------

def test_the_planner_prompt_carries_the_current_date_and_time():
    from jarvis.core.model_planner import ModelPlanner

    note = ModelPlanner._now_note()
    assert "Current date and time" in note, note
    stamp = note.split("Current date and time:")[1].split("(")[0].strip()
    # The format the planner is told to use is the format it gets.
    datetime.strptime(stamp, "%Y-%m-%d %H:%M")


def test_the_planner_prompt_states_the_time_format_the_tools_read():
    from jarvis.core import model_planner

    system = model_planner._SYSTEM
    assert "YYYY-MM-DD HH:MM" in system, system
    assert "24-hour" in system, system


def test_the_narrowed_catalogue_holds_only_the_named_domain(core):
    """The catalogue is filtered by tool tag, not by a hand-written list."""
    from jarvis.core.model_planner import ModelPlanner

    planner = ModelPlanner(tools=core.tools)
    calendar_only = planner._catalogue("calendar")
    assert calendar_only, "the calendar domain produced no tools at all"
    assert "create_event" in calendar_only, calendar_only
    assert "move_file" not in calendar_only, calendar_only


def test_the_destructive_tier_is_not_offered_to_the_model(core):
    """The narrowing must not widen what the model may propose.

    ``cancel_event`` is DESTRUCTIVE, and the tiers the model may propose
    from are READ, LOW and EXTERNAL. A narrowed catalogue that included it
    would let the model ask for a cancellation on its own; the rule planner
    owns that tier.
    """
    from jarvis.core.model_planner import PROPOSABLE_RISKS, ModelPlanner

    planner = ModelPlanner(tools=core.tools)
    catalogue = planner._catalogue("calendar")
    assert "cancel_event" not in catalogue, catalogue
    assert RiskLevel.DESTRUCTIVE not in PROPOSABLE_RISKS


# ---------------------------------------------------------------------------
# 4. the identifier lookup searches by the name the person gave
# ---------------------------------------------------------------------------

def test_the_calendar_source_declares_which_parameter_takes_the_name(core):
    spec = core.tools.get("list_events")
    assert spec is not None
    shape = spec.provides_identifiers["uid"]
    assert shape.get("lookup") == "query", shape
    assert "query" in spec.parameters, spec.parameters


def test_the_orchestrator_passes_the_name_to_the_source(core):
    """The lookup is called with the name, not with nothing.

    With nothing, ``list_events`` means *today*, and a request about next
    week's event was answered "there is nothing of that kind right now".
    """
    from jarvis.core.orchestrator import JarvisCore

    spec = core.tools.get("list_events")
    params = JarvisCore._lookup_params(spec, "Design Review", "query")
    assert params == {"query": "Design Review"}, params


def test_a_source_without_a_declared_lookup_is_run_with_no_arguments(core):
    """A source that does not declare one is not searched.

    Every identifier source now declares a lookup, so this uses a
    registered one that does not -- the rule is about the *declaration*,
    not about which tools happen to have one today.
    """
    from jarvis.core.orchestrator import JarvisCore
    from jarvis.core.models import ToolSpec, RiskLevel

    spec = ToolSpec(name="list_widgets", description="List widgets.",
                    agent="laptop", risk=RiskLevel.READ, parameters={},
                    provides_identifiers={"widget_id": {"items": "widgets",
                                                       "id": "widget_id",
                                                       "label": "name"}})
    assert JarvisCore._lookup_params(spec, "anything", "query") == {}


def test_a_lookup_parameter_the_source_does_not_have_is_not_passed(core):
    from jarvis.core.orchestrator import JarvisCore

    spec = core.tools.get("list_events")
    assert JarvisCore._lookup_params(spec, "Design Review", "nonsense") == {}


def test_the_calendar_tool_searches_the_whole_calendar_when_given_a_name(core):
    """A name turns the enumerator into a search.

    The window default is *today*; a search covers the calendar, so an event
    six days out is found.
    """
    from jarvis.tools.base import ToolContext

    ctx = ToolContext(device_id="laptop_main", session_id="r90-search")
    result = asyncio.run(core.tools.handler("list_events")(
        ctx, query="a name that matches nothing at all"))
    assert result.ok, result.error
    assert result.output["events"] == [], result.output


# ---------------------------------------------------------------------------
# 5. one match completes the plan; several still ask
# ---------------------------------------------------------------------------

def test_a_name_that_matches_exactly_one_thing_completes_the_plan(core):
    """The person's own words identify it, so the plan is not asked about.

    The completion is a *proposal*: it still goes to the permission gate, so
    a destructive action still waits for approval.
    """
    from jarvis.tools.base import ActionResult, ToolContext

    spec = core.tools.get("list_events")

    async def handler(ctx, query="", **kw):
        return ActionResult(ok=True, tool="list_events", verified=True,
                            output={"events": [{"uid": "EVT-1", "title": "Design Review"}]})

    # The live handler is put back, not deleted: popping it left the registry
    # short of a real capability for every later test in the session, and a
    # later file that drives `list_events` failed with `'NoneType' object is
    # not callable` -- a failure of this test's cleanup, not of the code.
    original = core.tools.handler("list_events")
    core.tools.register(spec, handler)
    try:
        response = _run(core, "cancel my Design Review event")
    finally:
        core.tools.register(spec, original)

    assert response.needs_confirmation, response.reply
    assert "EVT-1" in response.reply, response.reply
    assert response.plan.reply_kind != "clarification", response.plan


def test_a_name_that_matches_several_things_still_asks(core):
    """Nine events called *Browser Check* identify none of them."""
    from jarvis.tools.base import ActionResult

    spec = core.tools.get("list_events")

    async def handler(ctx, query="", **kw):
        return ActionResult(ok=True, tool="list_events", verified=True,
                            output={"events": [{"uid": f"EVT-{i}", "title": "Browser Check"}
                                               for i in range(9)]})

    original = core.tools.handler("list_events")
    core.tools.register(spec, handler)
    try:
        response = _run(core, "cancel my Browser Check event")
    finally:
        core.tools.register(spec, original)

    assert response.plan.reply_kind == "clarification", response.reply
    assert not response.needs_confirmation, response.reply
    assert "EVT-0" in response.reply, response.reply


def test_a_name_that_matches_nothing_still_asks_and_says_so(core):
    from jarvis.tools.base import ActionResult

    spec = core.tools.get("list_events")

    async def handler(ctx, query="", **kw):
        return ActionResult(ok=True, tool="list_events", verified=True,
                            output={"events": []})

    original = core.tools.handler("list_events")
    core.tools.register(spec, handler)
    try:
        response = _run(core, "cancel my Nonexistent Thing event")
    finally:
        core.tools.register(spec, original)

    assert response.plan.reply_kind == "clarification", response.reply
    assert "nothing of that kind" in response.reply, response.reply


def test_the_question_names_the_thing_the_person_named(core):
    """*your sentence does not say which one* is false about a named sentence."""
    response = _run(core, "cancel my Design Review event")
    assert "Design Review" in response.reply, response.reply
    assert "does not say which one" not in response.reply, response.reply


def test_the_lookup_does_not_choose_when_the_sentence_named_nothing(core):
    """No name, no completion -- the question is the honest answer."""
    response = _run(core, "cancel my event")
    assert response.plan.reply_kind == "clarification", response.reply
    assert response.plan.resolved_identifier == {}, response.plan


# ---------------------------------------------------------------------------
# 6. every identifier source can be searched by name
# ---------------------------------------------------------------------------

def test_every_identifier_source_declares_a_usable_lookup(core):
    """A source that cannot be searched cannot answer "which one did you mean".

    The mechanism only works when the source says which of its parameters
    takes the name. A source with no declaration is run with no arguments --
    and for a windowed enumerator that means its default window, which is
    how an event six days out was reported as not existing.
    """
    missing = []
    for spec in core.tools.all():
        for param, shape in (spec.provides_identifiers or {}).items():
            if not shape:
                continue
            lookup = shape.get("lookup")
            if not lookup:
                missing.append((spec.name, param, "no lookup declared"))
            elif lookup not in (spec.parameters or {}):
                missing.append((spec.name, param,
                                f"lookup {lookup!r} is not a parameter"))
    assert not missing, (
        "these identifier sources cannot be searched by name: "
        f"{missing}")


def test_a_searched_source_answers_a_name_that_matches_nothing(core):
    """The search is over the whole collection, not the default window."""
    from jarvis.tools.base import ToolContext

    ctx = ToolContext(device_id="laptop_main", session_id="r90-empty")
    result = asyncio.run(core.tools.handler("list_schedules")(
        ctx, query="a schedule that does not exist"))
    assert result.ok, result.error
    assert result.output["schedules"] == [], result.output


def test_a_searched_source_answers_with_the_matching_rows(core):
    """A searched source narrows to the match, not to everything."""
    from jarvis.tools.base import ToolContext

    ctx = ToolContext(device_id="laptop_main", session_id="r90-hit")
    asyncio.run(core.tools.handler("schedule_task")(
        ctx, name="morning stretch", schedule="every morning"))
    listed = asyncio.run(core.tools.handler("list_schedules")(
        ctx, query="stretch"))
    assert listed.ok, listed.error
    names = [s.get("name") for s in listed.output["schedules"]]
    assert "morning stretch" in names, listed.output
