"""Phase 19: say which part of a request was not done.

Two findings, one mechanism.

14-D -- measured at the start of this phase:

    "what is the time and then create a file called ../p19_escape.txt containing: nope"
        intent=time  actions=['get_time']
        reply: It is 13:01 on Sunday, 27 September 2026.

    "what is the time and then frobnicate the warp core"
        intent=time  actions=['get_time']
        reply: It is 13:01 on Sunday, 27 September 2026.

Both answered the half they could handle and said nothing about the half they did
not. The refusal reason the planner had already composed -- "lands outside my file
workspace, so I will not create it by name" -- was computed, put in the Intent's
note, and then thrown away by the fallback.

17-A -- `no_action` currently covers four different facts: nothing was understood,
the capability does not exist, a route refused, and a request for more detail. The
planner knows which of those it produced; the response did not say.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from jarvis.core.models import CommandRequest
from jarvis.core.planner import Intent
from jarvis.core.planner import RulePlanner

PAGE = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"
node = shutil.which("node")

ESCAPE_SENTENCE = (
    "what is the time and then create a file called ../p19_escape.txt containing: nope")
JARGON_SENTENCE = "what is the time and then frobnicate the warp core"


# ---------------------------------------------------------------------------
# 14-D -- the dropped half must be named.
# ---------------------------------------------------------------------------


async def test_refused_clause_is_named_in_the_reply(core):
    response = await core.handle(CommandRequest(text=ESCAPE_SENTENCE, source="test"))

    assert response.intent == "time", "the first half no longer runs -- different test needed"
    assert "workspace" in response.reply.lower(), response.reply
    assert "p19_escape.txt" in response.reply, (
        "the refused clause vanished from the answer entirely")


async def test_unroutable_clause_is_named_in_the_reply(core):
    response = await core.handle(CommandRequest(text=JARGON_SENTENCE, source="test"))

    assert "frobnicate the warp core" in response.reply, response.reply
    assert response.intent == "time"


async def test_dropped_clause_is_recorded_in_the_audit_detail(core):
    before = core.db.query("SELECT COUNT(*) AS n FROM action_logs")[0]["n"]
    await core.handle(CommandRequest(text=ESCAPE_SENTENCE, source="test"))
    row = core.db.query(
        "SELECT detail FROM action_logs ORDER BY rowid DESC LIMIT 1")[0]["detail"]

    assert core.db.query("SELECT COUNT(*) AS n FROM action_logs")[0]["n"] > before
    assert "p19_escape.txt" in row, row


async def test_nothing_is_dropped_when_every_clause_routes(core):
    plan = core.planner.parse(
        "what is the time and then create a file called p19-ok.txt containing: fine")

    assert [a.tool for a in plan.actions] == ["get_time", "write_file"]
    assert plan.unhandled == []


async def test_a_single_clause_sentence_has_no_unhandled_list(core):
    plan = core.planner.parse("what is the time")

    assert len(plan.actions) == 1
    assert plan.unhandled == []


async def test_the_refusal_still_creates_no_file(core, tmp_area, monkeypatch):
    from jarvis.config import get_settings

    monkeypatch.setattr(get_settings(), "file_workspace", str(tmp_area))
    await core.handle(CommandRequest(text=ESCAPE_SENTENCE, source="test"))

    assert not (tmp_area.parent / "p19_escape.txt").exists()
    assert list(tmp_area.iterdir()) == [], "the refused clause wrote something anyway"


# ---------------------------------------------------------------------------
# 17-A -- the planner's four kinds of non-answer, kept distinct.
# ---------------------------------------------------------------------------


def test_reply_kind_for_unrecognised_input(core):
    assert core.planner.parse("flibbertigibbet the wozzle").reply_kind == "not_understood"


def test_reply_kind_for_a_refused_route(core):
    assert core.planner.parse("Jarvis, transfer this file to my phone.").reply_kind == "declined"


def test_reply_kind_when_a_named_capability_is_missing(core):
    """`_to_plan` directly: no sentence can name a tool the registry lacks."""
    plan = core.planner._to_plan("do the thing", Intent("thing", tool="no_such_tool_xyz"))

    assert plan.direct_reply and "no_such_tool_xyz" in plan.direct_reply
    assert plan.reply_kind == "capability_missing"


def test_declined_route_still_reaches_the_dashboard_as_a_non_execution(core, state_of):
    import asyncio

    response = asyncio.run(core.handle(
        CommandRequest(text="Jarvis, transfer this file to my phone.", source="test")))
    payload = json.loads(response.model_dump_json())

    assert payload["plan"]["reply_kind"] == "declined"
    assert state_of(payload) == "NOTHING EXECUTED"


# ---------------------------------------------------------------------------
# The page must render the new kinds distinctly, and never as a success.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def state_of(tmp_path_factory):
    if not node:
        pytest.skip("node not available to run the page's own code")
    body = re.search(r"<script>(.*?)</script>", PAGE.read_text(), re.S).group(1)
    extracted = body[body.index("const STATE_TONE"):body.index("function renderState")]
    extracted = extracted.replace("const STATE_TONE", "var STATE_TONE", 1)
    lib = tmp_path_factory.mktemp("p19") / "page.js"

    def _run(payload):
        lib.write_text("const s = " + json.dumps(extracted) + ";\neval(s);\n"
                       "console.log(pipelineState(JSON.parse(process.argv[2])));\n")
        proc = subprocess.run([node, str(lib), json.dumps(payload, default=str)],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr[:300]
        return proc.stdout.strip()

    return _run


def _payload(kind, extra_reply=""):
    return {"actions": [], "plan": {"actions": [], "reply_kind": kind,
                                    "direct_reply": extra_reply or "x",
                                    "needs_clarification": False, "unhandled": []}}


def test_each_non_answer_gets_its_own_state(state_of):
    assert state_of(_payload("not_understood")) == "NOT RECOGNISED"
    assert state_of(_payload("capability_missing")) == "NOT AVAILABLE"
    assert state_of(_payload("declined")) == "NOTHING EXECUTED"
    clarifying = _payload("declined")
    clarifying["plan"]["needs_clarification"] = True
    assert state_of(clarifying) == "NEEDS DETAIL"


def test_no_non_answer_can_render_as_verified(state_of):
    for kind in ("not_understood", "capability_missing", "declined", "", "invented_kind"):
        assert state_of(_payload(kind)) != "VERIFIED", kind


def test_page_ships_the_new_vocabulary():
    source = PAGE.read_text()
    for token in ("NOT RECOGNISED", "NOT AVAILABLE", "reply_kind", "capability_missing"):
        assert token in source, f"{token} never reaches the shipped page"


def test_an_absent_reply_kind_stays_conservative(state_of):
    """A payload that carries no answer at all is still UNKNOWN, not a claim.

    Written first as NOTHING EXECUTED, which would have had the page assert a fact
    the backend never stated: UNKNOWN is the answer when the evidence does not
    distinguish, and an invented `reply_kind` must not change that.
    """
    assert state_of({"actions": [], "plan": {"actions": []}}) == "UNKNOWN"
    assert state_of({"actions": [], "plan": {"actions": [], "reply_kind": "invented"}}) == "UNKNOWN"


# ---------------------------------------------------------------------------
# Found while proving this phase, over live HTTP: with the dropped clause now
# named in the reply, the state chip still said VERIFIED, because it only ever
# looked at the actions that did run. A request answered in part is not a request
# verified in whole.
# ---------------------------------------------------------------------------


def test_a_partly_handled_request_is_not_plain_verified(state_of):
    payload = {"actions": [{"tool": "get_time", "verification": "success",
                            "ok": True, "output": {}}],
               "plan": {"actions": [{"tool": "get_time"}], "unhandled": ["the second half"]}}

    assert state_of(payload) == "VERIFIED IN PART"


async def test_the_live_escape_sentence_does_not_claim_full_verification(core, state_of):
    response = await core.handle(CommandRequest(text=ESCAPE_SENTENCE, source="test"))
    payload = json.loads(response.model_dump_json())

    assert payload["actions"], "the first half no longer runs"
    assert payload["plan"]["unhandled"], "the refused clause is no longer reported"
    assert state_of(payload) != "VERIFIED", "a dropped clause went back to being hidden"
    assert state_of(payload) == "VERIFIED IN PART"


def test_a_real_failure_outranks_a_dropped_clause(state_of):
    payload = {"actions": [{"tool": "delete_file", "verification": "failed",
                            "ok": False, "output": {}}],
               "plan": {"actions": [], "unhandled": ["something else"]}}

    assert state_of(payload) == "FAILED"
