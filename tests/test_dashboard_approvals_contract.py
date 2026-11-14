"""The dashboard can show and answer a pending approval.

The macOS app gained that in R6.5; the dashboard — the surface most people
use for the model — could not. It had no `/confirmations` fetch, no card, and
no approve or deny. The server side was already proven (`/confirmations` and
`/confirm/{id}` both work, and the macOS app uses them), so this was a
client-shaped gap: the page could not show a person an action the backend
was holding for them.

These tests pin the client contract, and the honesty rules that matter more
than the plumbing:

* the card shows **the server's own `preview`** of what will happen, not
  something the page rebuilt from the parameters;
* the answer carries **the approver's session**, because the backend records
  *who* answered;
* a **failed** action is reported as failed — approving does not turn a
  failure into a success;
* the text is inserted as **text**, so a page-supplied string cannot inject
  markup into the page.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DASHBOARD = REPO / "dashboard" / "ai_chat.html"


@pytest.fixture(scope="module")
def source() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def script(source: str) -> str:
    """The dashboard's JavaScript, extracted."""
    match = re.search(r"<script>(.*?)</script>", source, re.S)
    assert match, "the dashboard has no inline script"
    return match.group(1)


# ---------------------------------------------------------------------------
# The client reaches the contract
# ---------------------------------------------------------------------------
def test_the_dashboard_reads_the_pending_approvals(script: str) -> None:
    """It asks the backend, rather than inventing a list."""
    assert '"/confirmations"' in script, (
        "the dashboard never reads the backend's pending confirmations")


def test_the_dashboard_answers_them(script: str) -> None:
    """And it can answer one."""
    assert "/confirm/" in script, "the dashboard cannot answer an approval"
    assert "approve" in script, "the answer does not say whether it approved"


def test_the_answer_carries_the_approvers_session(script: str) -> None:
    """The backend records *who* answered, so the page must say who it is.

    A confirmation answered on the dashboard and one answered on the phone
    are not the same fact, and the audit needs to tell them apart.
    """
    assert "session_id" in script, "the answer does not carry a session"
    match = re.search(r"session_id:\s*\"([^\"]+)\"", script)
    assert match, "the session is not set to a value"
    assert match.group(1).strip(), "the session is empty"


# ---------------------------------------------------------------------------
# The card tells the truth
# ---------------------------------------------------------------------------
def test_the_card_shows_the_servers_own_description(script: str) -> None:
    """The preview is what will happen.

    The page must not rebuild that from the parameters: a card that says
    something slightly different from the thing being approved is worse than
    no card, because it is a confident lie in the place a person looks
    before pressing a button.
    """
    assert "confirmation.preview" in script, (
        "the card does not show the server's preview of the effect")
    # It must not reconstruct one from the parameters.
    assert not re.search(r"confirmation\.params\s*\[", script), (
        "the card rebuilds the description from the parameters instead of "
        "showing the server's own preview")


def test_the_card_names_the_tool(script: str) -> None:
    """Which action is being approved."""
    assert "confirmation.tool" in script, "the card does not name the tool"


def test_the_approval_text_is_inserted_as_text(script: str) -> None:
    """A string from the server is data, never markup.

    The preview is built by the backend from a page's own parameters, so it
    can contain anything a page chose to put there. Inserting it as HTML
    would let a page inject markup into the dashboard.
    """
    card = re.search(r"function buildApprovalCard\(.*?\n      \}", script, re.S)
    assert card, "the approval card builder is gone"
    body = card.group(0)
    assert "textContent" in body, "the approval text is not inserted as text"
    assert ".innerHTML =" not in body, (
        "the approval card assigns innerHTML -- a page could inject markup")


# ---------------------------------------------------------------------------
# The result is reported honestly
# ---------------------------------------------------------------------------
def test_a_failed_action_is_reported_as_failed(script: str) -> None:
    """Approving does not turn a failure into a success.

    The gate releases the action; the action can still fail. The page has to
    say which happened, or a person reads "Approved" and believes the thing
    was done.
    """
    assert "action.ok" in script, "the page does not check whether the action worked"
    assert "failed" in script, "a failed action is not reported as failed"
    # The verification state is reported too, so "done" and "confirmed" stay
    # distinguishable. The check is on the *reporting* of it, not on the word
    # appearing somewhere in the file.
    suffix = re.search(r"function verificationSuffix\(.*?\n      \}", script, re.S)
    assert suffix, "the result does not say whether it was verified"
    body = suffix.group(0)
    assert "action.verification" in body, (
        "the verification state is not read")
    for state in ("success", "failed", "unknown"):
        assert state in body, (
            f"the {state!r} verification state is not reported -- 'done' and "
            "'confirmed' would be indistinguishable")


def test_the_approvals_are_refreshed_after_a_send(script: str) -> None:
    """A message can leave an action waiting for a person."""
    send = re.search(r"async function handleSend\(\).*?\n      \}", script, re.S)
    assert send, "the send handler is gone"
    assert "refreshApprovals" in send.group(0), (
        "the approvals are not re-read after a message is sent")


def test_the_approvals_are_refreshed_after_answering(script: str) -> None:
    """And after an answer, so the card disappears when it is answered."""
    resolve = re.search(r"async function resolveApproval\(.*?\n      \}", script, re.S)
    assert resolve, "the resolve handler is gone"
    assert "refreshApprovals" in resolve.group(0), (
        "the approvals are not re-read after one is answered")


def test_a_failure_to_list_the_approvals_is_not_shown_as_an_error(script: str) -> None:
    """The gate is still holding the action whether or not the page can list it.

    Turning a listing failure into a big red banner would report a problem
    that does not exist, and hide the fact that something is still waiting.
    """
    refresh = re.search(r"async function refreshApprovals\(.*?\n      \}", script, re.S)
    assert refresh, "the refresh handler is gone"
    body = refresh.group(0)
    assert "catch" in body, "a failed listing is not handled"
    assert "renderApprovals([])" in body, (
        "a failed listing leaves the previous approvals on screen")


# ---------------------------------------------------------------------------
# The page is well-formed
# ---------------------------------------------------------------------------
def test_the_dashboard_script_parses(source: str, script: str) -> None:
    """A syntax error in the page's script would break the whole chat view."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available to check the script")
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write(script)
        path = handle.name
    result = subprocess.run([node, "--check", path], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
