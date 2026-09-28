"""Phase 24: a refusal and a question are different answers (19-B).

Phase 19 found that "I will not do that" and "which one did you mean?" both came back as
``reply_kind="declined"``, and left it open because separating them "would mean guessing
from punctuation". Phases 22 and 23 made that cost real: the system now asks genuine
questions -- "Send what to Arun, and by which medium?", "Which light did you mean" -- and
every one of them was still labelled a refusal, while the guard's actual refusals carried
no label at all (``reply_kind=""``).

The guess Phase 19 was wary of is not the one that matters. The note being classified is
the planner's *own* sentence, written by the route that produced it, not the user's
punctuation. So the kind is read from that wording, and a route can override it explicitly
(``Intent(clarification=False)``) where its own sentence is a statement rather than a
question -- "I cannot read that device's battery: no phone is paired" asks for nothing.

The property this is really protecting is one-directional: a refusal must never be
presented as an invitation to continue. Getting that wrong is how a safety boundary quietly
becomes a prompt.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest
from jarvis.core.models import CommandRequest, Plan

REPO = Path(__file__).resolve().parent.parent


def _dashboard_state(core, text: str) -> tuple:
    """Run the *shipped* dashboard mapping over the real response payload."""

    response = asyncio.run(core.handle(CommandRequest(text=text, source="test")))
    payload = response.model_dump()
    source = (REPO / "dashboard" / "index.html").read_text()
    body = re.search(r"<script>(.*?)</script>", source, re.S).group(1)
    snippet = body[body.index("const STATE_TONE"):body.index("function renderState")]
    snippet = snippet.replace("const STATE_TONE", "var STATE_TONE", 1)
    script = "eval(" + json.dumps(snippet) + ");\n" \
             "console.log(pipelineState(JSON.parse(process.argv[2])));\n"
    handle = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False)
    handle.write(script)
    handle.close()
    node = subprocess.run(["node", handle.name, json.dumps(payload, default=str)],
                          capture_output=True, text=True, check=False)
    os.unlink(handle.name)
    assert node.returncode == 0, node.stderr
    return payload, node.stdout.strip()


def _kind(core, text: str) -> Plan:
    response = asyncio.run(core.handle(CommandRequest(text=text, source="test")))
    return response.plan


# ---------------------------------------------------------------------------
# 24-A -- questions stop being labelled refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sentence,expected_question", [
    ("Jarvis, send to Arun.", "Send what to Arun"),
    ("Jarvis, turn on the light.", "Which light"),
    ("Jarvis, compress this", "Which file"),
    ("Jarvis, remember x", "What should I remember"),
])
def test_a_missing_detail_is_a_clarification_not_a_refusal(core, sentence, expected_question):
    plan = _kind(core, sentence)

    assert plan.reply_kind == "clarification", (
        f"{sentence!r} answered a question but was labelled {plan.reply_kind!r}")
    assert plan.needs_clarification is True
    assert expected_question.lower() in (plan.clarification_question or "").lower(), (
        plan.clarification_question)


def test_a_command_waiting_on_a_missing_prerequisite_asks_for_it(core, monkeypatch):
    """"Send it" with nothing drafted is a question, not a refusal.

    State-dependent by nature -- with a draft waiting it is a real send -- so the
    precondition is set rather than hoped for, instead of relying on test ordering.
    """
    from jarvis.tools import email as email_module

    monkeypatch.setattr(email_module, "latest_draft", lambda *a, **k: None)
    plan = _kind(core, "Jarvis, send it.")

    assert plan.reply_kind == "clarification", plan.reply_kind
    assert "draft" in (plan.clarification_question or "").lower(), plan.clarification_question


def test_a_refusal_is_a_refusal_and_not_a_question(core):
    plan = _kind(core, "Jarvis, delete everything.")

    assert plan.reply_kind == "refusal", (
        f"the guard blocked it but the plan says {plan.reply_kind!r}; an unlabelled "
        "refusal and a clarification dressed as one are the same defect in two directions")
    assert plan.needs_clarification is False


def test_a_capability_that_is_unavailable_asks_for_nothing(core):
    """A statement of what is missing is not a question, even though it ends a turn."""
    plan = _kind(core, "Jarvis, what is my phone battery?")

    assert plan.reply_kind == "declined", plan.reply_kind
    assert plan.needs_clarification is False, (
        f"{plan.clarification_question!r} is not something the user can answer to proceed")


def test_not_understood_stays_its_own_kind(core):
    plan = _kind(core, "Jarvis, design this system.")

    assert plan.reply_kind == "not_understood"
    assert plan.needs_clarification is False


# ---------------------------------------------------------------------------
# 24-B -- the distinction reaches the user, the ledger, and the screen
# ---------------------------------------------------------------------------


def test_the_reply_kind_travels_to_the_http_payload(core):
    response = asyncio.run(core.handle(
        CommandRequest(text="Jarvis, send to Arun.", source="test")))
    plan = response.model_dump().get("plan") or {}

    assert plan.get("reply_kind") == "clarification", plan
    assert plan.get("needs_clarification") is True


def test_the_dashboard_shows_needs_detail_for_a_question(core):
    payload, state = _dashboard_state(core, "Jarvis, send to Arun.")

    assert state == "NEEDS DETAIL", (
        f"a question was rendered as {state!r}; the user cannot tell what to do next")


def test_the_dashboard_still_shows_blocked_for_a_refusal(core):
    payload, state = _dashboard_state(core, "Jarvis, delete everything.")

    assert state == "BLOCKED", state


def test_the_ledger_records_which_kind_of_non_action_it_was(core):
    before = core.db.query("SELECT COUNT(*) AS n FROM action_logs")[0]["n"]
    asyncio.run(core.handle(CommandRequest(text="Jarvis, send to Arun.", source="test")))

    row = core.db.query("SELECT * FROM action_logs ORDER BY rowid DESC LIMIT 1")[0]
    assert core.db.query("SELECT COUNT(*) AS n FROM action_logs")[0]["n"] > before
    detail = json.loads(row["detail"] or "{}")
    assert detail.get("reply_kind") == "clarification", (
        f"the chain records result={row['result']!r} but not what kind of decision it was: "
        f"{sorted(detail)}")


def test_a_refusal_row_is_labelled_in_the_chain_too(core):
    asyncio.run(core.handle(CommandRequest(text="Jarvis, delete everything.", source="test")))

    row = core.db.query("SELECT * FROM action_logs ORDER BY rowid DESC LIMIT 1")[0]
    detail = json.loads(row["detail"] or "{}")
    assert detail.get("reply_kind") == "refusal", detail
    assert row["result"] == "blocked"
