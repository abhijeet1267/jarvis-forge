"""Phase 29: a route must fill the action it claims, or ask for what is missing.

Measured on the live tree, the flowchart and architecture-diagram routes had been
unreachable since they were written. They emit their tool with a title and nothing
else, while both tools *require* a second parameter, so the permission gate
answered first:

    >>> "draw a flowchart of onboarding: request, review, approve"
        intent=create_flowchart  decision=BLOCK  tools=[]
        reply="I cannot do that: missing required parameter 'steps'."
    >>> "make a diagram of the build pipeline with checkout, compile and test"
        intent=create_architecture_diagram  decision=BLOCK  tools=[]
        reply="I cannot do that: missing required parameter 'components'."
    >>> "notify my watch"
        intent=watch_notify  decision=BLOCK  tools=[]
        reply="I cannot do that: missing required parameter 'title'."

The content was in the sentence and thrown away. And the answer the user should
have got was already written -- in the tool, in human words ("Which steps? Give me
them in order, separated by commas.") -- but unreachable, because the route never
let the handler run. A schema field name won the argument.

Two rules, both measured here:

* a route that can fill its parameters from the sentence does, and the tool runs;
* a route that cannot must ask -- as a clarification, in the language of the
  request -- and must not emit an action it knows the gate will refuse.
"""

from __future__ import annotations

import asyncio

import pytest
from jarvis.core.models import CommandRequest, PolicyDecision

CORPUS = [
    "draw a flowchart of the build",
    "make a diagram of the system",
    "create an architecture diagram",
    "notify my watch",
    "send a notification to my watch",
    "draw a flowchart",
    "make me a diagram of the pipeline",
]


def _run(core, text: str):
    return asyncio.run(core.handle(CommandRequest(text=text, source="phase29")))


# ---------------------------------------------------------------------------
# 29-A -- a parameter name is not an answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", CORPUS)
def test_no_planner_sentence_is_answered_with_a_schema_field_name(core, text):
    """The gate's vocabulary must never be the user's answer.

    It is still the right thing for a direct API call that omits a parameter --
    what is wrong is a *route* putting a user in that position.
    """
    response = _run(core, text)
    assert "missing required parameter" not in response.reply, response.reply
    assert not response.actions, f"{text!r} executed something: {[a.tool for a in response.actions]}"


@pytest.mark.parametrize("text", [
    "draw a flowchart of the build",
    "notify my watch",
    "make a diagram of the system",
])
def test_an_unfillable_request_asks_in_human_words(core, text):
    """Asks -- which means the taxonomy from Phase 24 applies: it is a question,
    not a refusal, and it advertises no phantom approval."""
    response = _run(core, text)
    assert response.plan.reply_kind == "clarification", response.reply
    assert response.needs_confirmation is False, response.reply
    assert response.plan.needs_clarification is True, response.plan
    assert "?" in response.reply, response.reply
    assert response.decision is None, response.decision


def test_the_question_names_what_it_needs_in_the_users_own_terms(core):
    """'steps' and 'components' are fine words when they are inside a question;
    they were only ever wrong as a validator's complaint."""
    flow = _run(core, "draw a flowchart of the build")
    assert "step" in flow.reply.lower(), flow.reply
    assert "order" in flow.reply.lower() or "," in flow.reply, flow.reply

    diagram = _run(core, "make a diagram of the payment service")
    assert "component" in diagram.reply.lower(), diagram.reply

    watch = _run(core, "notify my watch")
    assert "notif" in watch.reply.lower() or "say" in watch.reply.lower(), watch.reply


def test_a_ledger_row_exists_for_the_ask_and_says_no_action_ran(core):
    """The clarification is an answered request, so it belongs in the chain."""
    response = _run(core, "draw a flowchart of the ingest queue")
    row = next(r for r in core.audit.recent(15) if r.get("request_id") == response.request_id)
    # Nothing ran, so the token is `no_action`; what distinguishes a question
    # from a refusal in the chain is the kind the route wrote (Phase 24).
    assert row["result"] == "no_action", row
    assert (row.get("detail") or {}).get("reply_kind") == "clarification", row


# ---------------------------------------------------------------------------
# 29-B -- what the sentence already said is used, not dropped
# ---------------------------------------------------------------------------


def test_a_flowchart_written_in_one_sentence_is_actually_drawn(core):
    response = _run(core, "draw a flowchart of onboarding: request, review, approve")
    # The handler reports the artifact it made, not the tool's name.
    assert [a.tool for a in response.actions] == ["diagram_created"], response.reply
    assert response.decision is PolicyDecision.EXECUTE, response.decision
    result = response.actions[0]
    assert result.ok and result.verification.value == "success", (result.error, result.summary)
    assert result.output.get("steps") == 3, result.output
    assert result.output["asset"]["filename"].startswith("flowchart-of-onboarding"), result.output["asset"]


def test_the_steps_are_the_steps_and_not_the_whole_sentence(core):
    response = _run(core, "create a flowchart for releases with draft, build, publish")
    assert response.actions, response.reply
    assert response.actions[0].ok, response.actions[0].error
    asset = response.actions[0].output["asset"]
    assert "draft" not in asset["title"].lower(), asset["title"]
    assert "releas" in asset["title"].lower(), asset["title"]


def test_a_diagram_with_components_in_the_sentence_is_drawn(core):
    response = _run(core, "make an architecture diagram of the build with api, worker, db")
    assert [a.tool for a in response.actions] == ["diagram_created"], response.reply
    result = response.actions[0]
    assert result.ok, (result.error, result.summary)
    assert result.output["asset"]["filename"].endswith(".svg"), result.output["asset"]


def test_a_diagram_whose_sentence_names_connections_draws_them_too(core):
    """"api > worker, worker > db" names boxes *and* arrows.

    Before this, the list went in as components and the builder reported
    "2 component(s), 0 link(s)" -- every connection the user described vanished
    without a word about it.
    """
    response = _run(core, "make an architecture diagram of the build "
                          "with api > worker, worker > database")
    assert response.actions, response.reply
    result = response.actions[0]
    assert result.ok, result.error
    assert result.output.get("links") == 2, result.output
    assert result.output.get("components") == 3, result.output


def test_a_list_it_cannot_read_cleanly_is_asked_about_not_trimmed(core):
    """The extractor is all-or-nothing on purpose.

    "request, review, approve with review as a decision" has a trailing clause
    glued to the last step. Taking the two items that looked tidy would have
    drawn a two-step flowchart from a three-step request and reported it as
    done -- the partial answer with nothing saying so, which this program has
    been closing since Phase 19.
    """
    response = _run(core, "draw a flowchart of onboarding: request, review, "
                          "approve with review as a decision")
    assert not response.actions, [a.tool for a in response.actions]
    assert response.plan.reply_kind == "clarification", response.reply
    assert "step" in response.reply.lower(), response.reply


def test_a_diagram_request_names_its_parts_or_asks_and_never_leaks_a_field(core):
    """28-B, the repro Phase 28 parked: no schema name, no silent half-answer."""
    response = _run(core, "make a diagram of the build pipeline")
    assert "missing required parameter" not in response.reply, response.reply
    assert not response.actions, response.reply
    assert "component" in response.reply.lower(), response.reply


def test_a_watch_notification_that_names_its_text_still_sends(core):
    """The paired-watch path is simulated elsewhere; what matters here is that a
    sentence with text reaches the tool at all, which it always did."""
    response = _run(core, "notify my watch that the backup finished")
    assert [a.tool for a in response.actions] == ["send_watch_notification"], response.reply
    # Unpaired hardware answers honestly rather than pretending to notify.
    assert "not paired" in response.reply or "sent" in response.reply.lower(), response.reply


# ---------------------------------------------------------------------------
# 29-C -- the structural rule, asserted against the planner itself
# ---------------------------------------------------------------------------


def test_no_route_in_the_planner_emits_an_action_it_cannot_fill(core):
    """Every ``Intent(tool=..., params={...})`` literal in the planner must supply
    the tool's required parameters.

    The sweep that found this phase's defects, turned into a guard so the next
    route cannot make the same mistake. ``**``-built dicts are checked by the
    behaviour tests above rather than guessed at here.
    """
    import ast
    from pathlib import Path

    source = Path("jarvis/core/planner.py").read_text()
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Intent"):
            continue
        tool = params = None
        for kw in node.keywords:
            if kw.arg == "tool" and isinstance(kw.value, ast.Constant):
                tool = kw.value.value
            if kw.arg == "params" and isinstance(kw.value, ast.Dict):
                params = {k.value for k in kw.value.keys if isinstance(k, ast.Constant)}
                if any(k is None for k in kw.value.keys):
                    params = None      # {**other, ...} -- not statically checkable
        if tool is None or params is None:
            continue
        spec = core.tools.get(tool)
        if spec is None:
            offenders.append(f"planner.py:{node.lineno} {tool} is not registered")
            continue
        gap = set(spec.required_parameters) - params
        if gap:
            offenders.append(f"planner.py:{node.lineno} {tool} misses {sorted(gap)}")
    assert offenders == [], offenders


def test_the_two_design_tools_were_never_reachable_from_a_sentence_before_this(core):
    """Guard against the fix being undone by deleting the capability instead.

    Both tools are registered, tier low, and take content the user has to name;
    if a future change routes them to something else, this is the test that says
    why they must stay.
    """
    for name, required in (("create_flowchart", "steps"),
                           ("create_architecture_diagram", "components")):
        spec = core.tools.get(name)
        assert spec is not None and required in spec.required_parameters, spec
        assert spec.requires_confirmation is False
