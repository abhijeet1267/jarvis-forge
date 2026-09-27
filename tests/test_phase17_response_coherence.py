"""Phase 17: the response must agree with the record written for it.

Phase 16 put the truth in the ledger -- a refusal is now ``result="no_action"`` --
and left the in-band answer contradicting it. Measured over the five no-execution
acceptance commands: every one came back with ``decision=EXECUTE`` and zero
actions. So the row said nothing ran and the reply said something was executed, and
the second half of the sentence "every command has a truthful, auditable terminal
outcome" was still false.

The property is stated once, for both directions, rather than as five special cases.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jarvis.api.app import app
from jarvis.core.models import CommandRequest, PolicyDecision

TEST_TOKEN = os.environ["JARVIS_API_TOKEN"]
PAGE = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"


@pytest.fixture(scope="module")
def client() -> TestClient:
    """Authenticated, loopback-host client -- the dashboard's own position."""
    return TestClient(app, base_url="http://127.0.0.1:8765",
                      headers={"X-JARVIS-Token": TEST_TOKEN})


REFUSALS = [
    "Jarvis, transfer this file to my phone.",
    "Jarvis, what is my phone battery?",
    "Jarvis, design this system.",
    "Jarvis, flibbertigibbet the wozzle.",
    "Jarvis, send it.",
    "Jarvis, help",
]

EXECUTED = [
    "Jarvis, what is the time?",
    "Jarvis, show me my laptop battery.",
    "Jarvis, list my apps",
]


def _rows(core):
    return core.db.query("SELECT * FROM action_logs ORDER BY rowid")


# ---------------------------------------------------------------------------
# Nothing ran -> the response may not say it did.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sentence", REFUSALS)
async def test_refusal_does_not_report_execution(core, sentence):
    before = len(_rows(core))
    response = await core.handle(CommandRequest(text=sentence, source="test"))
    row = _rows(core)[before]

    assert not response.actions, sentence
    assert row["result"] == "no_action", row["result"]
    assert response.decision is not PolicyDecision.EXECUTE, (
        f"{sentence!r} executed nothing and the ledger agrees, but the response "
        f"still reported {response.decision}")


@pytest.mark.parametrize("sentence", REFUSALS)
async def test_refusal_over_the_api_never_reads_as_executed(client, sentence):
    """The same property on the wire, not just in the object.

    ``decision`` is what the dashboard branches on; an in-process assertion would
    not catch a value that only goes wrong on serialisation.
    """
    response = client.post("/command", json={"text": sentence, "source": "dashboard"})
    body = response.json()

    assert body["actions"] == [], sentence
    assert body["decision"] != "EXECUTE", body["decision"]


# ---------------------------------------------------------------------------
# The other direction: a claimed execution must have actions to show for it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sentence", EXECUTED)
async def test_executed_command_still_reports_execution(core, sentence):
    response = await core.handle(CommandRequest(text=sentence, source="test"))

    assert response.actions, sentence
    assert response.decision is PolicyDecision.EXECUTE, (
        "clearing the decision on refusals must not have cleared it where work "
        f"really was done: {sentence!r}")


async def test_decision_and_action_count_never_disagree(core):
    """One invariant, stated as the property it is.

    EXECUTE without an action is the lie; an action without EXECUTE is impossible
    by construction, so both halves are checked from the same data.
    """
    for sentence in REFUSALS + EXECUTED + ["Jarvis, check my car fuel."]:
        response = await core.handle(CommandRequest(text=sentence, source="test"))
        if response.decision is PolicyDecision.EXECUTE:
            assert response.actions, f"{sentence!r} claimed EXECUTE with no action"
        else:
            if response.actions:
                assert response.decision is PolicyDecision.CONFIRM, (
                    f"{sentence!r} ran actions under decision={response.decision}")


# ---------------------------------------------------------------------------
# The audit card must not colour a decision like a failure.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tone_of(tmp_path_factory):
    """Run the page's own ``auditTone`` over the shipped file's text."""
    if not shutil.which("node"):
        pytest.skip("node not available to run the page's own code")
    body = re.search(r"<script>(.*?)</script>", PAGE.read_text(), re.S).group(1)
    if "function auditTone" not in body:
        # Reported by test_page_ships_audit_tone_mapping, which fails rather than
        # errors -- a missing function is a real answer, not a broken harness.
        pytest.skip("auditTone is not in the shipped page")
    start = body.index("function auditTone")
    end = body.index("\n}", start) + 2
    lib = tmp_path_factory.mktemp("p17") / "tone.js"

    def _run(token: str) -> str:
        lib.write_text("eval(" + json.dumps(body[start:end]) + ");\n"
                       # argv[0] is node, argv[1] is this script: the token is
                       # argv[2]. Reading argv[1] handed every test the file path
                       # and made the fallback tone look like a wrong mapping.
                       "console.log(auditTone(process.argv[2]));\n")
        proc = subprocess.run([shutil.which("node"), str(lib), token],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr[:300]
        return proc.stdout.strip()

    return _run


@pytest.mark.parametrize("token, tone", [
    ("success", "ok"),
    ("failure", "bad"),
    ("unverified", "bad"),
    ("blocked", "bad"),
    ("denied", "bad"),
    ("no_action", "muted"),
    ("clarification", "warn"),
    ("awaiting_confirmation", "warn"),
    ("expired", "warn"),
    ("recovered", "warn"),
])
def test_each_ledger_token_has_its_own_tone(tone_of, token, tone):
    assert tone_of(token) == tone


def test_a_decision_is_not_coloured_like_a_failure(tone_of):
    """The whole point: ``no_action`` and ``failure`` used to share amber."""
    assert tone_of("no_action") != tone_of("failure")
    assert tone_of("no_action") != tone_of("blocked")
    assert tone_of("clarification") != tone_of("success")


def test_unknown_token_is_not_silently_green(tone_of):
    assert tone_of("something_new") in ("muted", "warn")
    assert tone_of("something_new") != "ok"


def test_page_ships_audit_tone_mapping():
    """The audit card must classify by token, not by "is it success or not"."""
    body = re.search(r"<script>(.*?)</script>", PAGE.read_text(), re.S).group(1)
    assert "function auditTone" in body, (
        "no token-to-tone mapping exists in the shipped page")
    assert "auditTone(r.result)" in body, (
        "the mapping exists but the audit card does not use it")
