"""Phase 13: the dashboard must render backend truth and never become an authority.

Every fixture here comes from a real backend call, and every rendered state comes
from the function actually shipped in ``dashboard/index.html`` -- not from a copy of
its logic. If the page and this file ever disagree, the test fails.

The property under test is narrow and was the point of Phases 9-12: VERIFIED may
only come from ``actions[].verification === "success"``. HTTP 200, ``action.ok``
and a reply that contains the word "success" are each separately proven not to
produce it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from jarvis.config import get_settings
from jarvis.core.models import CommandRequest, PlannedAction, PolicyDecision
from jarvis.home import HomeGateway, reset_home_gateway
from jarvis.tools.base import ToolContext, unverified

PAGE = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(not node, reason="node not available to run the page's own code")


@pytest.fixture(scope="module")
def state_of(tmp_path_factory):
    """Evaluate the dashboard's real pipelineState() against a payload."""
    source = PAGE.read_text()
    script = re.search(r"<script>(.*?)</script>", source, re.S)
    assert script, "no inline script found in the dashboard"
    body = script.group(1)
    start = body.index("const STATE_TONE")
    end = body.index("function renderState")
    extracted = body[start:end].replace("const STATE_TONE", "var STATE_TONE", 1)
    assert "function pipelineState" in extracted, "pipelineState() not found in the page"

    root = tmp_path_factory.mktemp("p13")
    lib = root / "page.js"
    lib.write_text(
        "const s = " + json.dumps(extracted) + ";\n"
        "eval(s);\n"
        "console.log(pipelineState(JSON.parse(process.argv[2])));\n"
    )

    def _run(payload) -> str:
        proc = subprocess.run(
            [node, str(lib), json.dumps(payload, default=str)],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr[:300]
        return proc.stdout.strip()

    return _run


def _payload(response) -> dict:
    """Serialize a real CommandResponse exactly as the API sends it."""
    return json.loads(response.model_dump_json())


def _ctx(core) -> ToolContext:
    return ToolContext(settings=core.settings, apps=core.apps, devices=core.devices,
                       memory=core.memory, graph=core.graph, core=core)


@pytest.fixture()
def workspace(tmp_area, monkeypatch) -> Path:
    """Point the file workspace at a scratch dir, so relative names land there.

    The route refuses absolute paths outside the workspace by design (Phase 12),
    so a test that hands it ``/tmp/...`` gets a refusal, not an action. Naming
    files relative to the workspace is what the user-facing path actually does.
    """
    monkeypatch.setattr(get_settings(), "file_workspace", str(tmp_area))
    return tmp_area


async def _confirmed(core, sentence: str):
    """Drive the real path: /command-shaped handle(), then approve it."""
    first = await core.handle(CommandRequest(text=sentence, source="test"))
    if not first.needs_confirmation:
        return first, first
    second = await core.resolve_confirmation(
        first.confirmation_id, True, session_id=first.session_id)
    return first, second


# ------------------------------------------------------------------ VERIFIED


async def test_real_created_file_renders_verified(core, workspace, state_of):
    target = workspace / "p13.txt"
    sentence = "Create a file called p13.txt containing: JARVIS PHASE 13 VERIFIED"

    first = await core.handle(CommandRequest(text=sentence, source="test"))
    assert first.needs_confirmation
    # Checked before anything is approved: an awaiting-confirmation state must
    # not have touched the disk yet.
    assert not target.exists(), "the file appeared before the user approved"

    after = await core.resolve_confirmation(
        first.confirmation_id, True, session_id=first.session_id)
    payload = _payload(after)
    assert payload["actions"][0]["verification"] == "success"
    assert state_of(payload) == "VERIFIED"
    assert target.read_text() == "JARVIS PHASE 13 VERIFIED\n"


# ------------------------------------------------------- AWAITING CONFIRMATION


async def test_unapproved_action_renders_awaiting_confirmation(core, workspace, state_of):
    target = workspace / "p13-wait.txt"
    sentence = "Create a file called p13-wait.txt containing: hold on"

    first = await core.handle(CommandRequest(text=sentence, source="test"))
    payload = _payload(first)
    assert payload["needs_confirmation"] is True
    assert payload["confirmation_id"]
    assert state_of(payload) == "AWAITING CONFIRMATION"
    assert not target.exists(), "an unapproved action wrote to disk"


async def test_denied_confirmation_never_renders_verified(core, workspace, state_of):
    target = workspace / "p13-deny.txt"
    sentence = "Create a file called p13-deny.txt containing: no thank you"

    first = await core.handle(CommandRequest(text=sentence, source="test"))
    denied = await core.resolve_confirmation(
        first.confirmation_id, False, session_id=first.session_id)

    payload = _payload(denied)
    assert state_of(payload) == "BLOCKED"
    assert state_of(payload) != "VERIFIED"
    assert not target.exists()


# ------------------------------------------------------------ VERIFICATION IS
# THE ONLY ROUTE TO VERIFIED. Each of the three lookalikes is tested apart.


async def test_ok_true_never_renders_verified(core, state_of):
    """`ok` means the handler answered. That is not the same fact."""
    result = unverified("some_tool", "answered, effect not established")
    assert result.ok is True and result.verified is False

    payload = {"actions": [json.loads(result.model_dump_json())]}
    assert state_of(payload) == "UNKNOWN"
    assert state_of(payload) != "VERIFIED"


async def test_reply_wording_never_renders_verified(core, state_of):
    result = unverified("some_tool", "success! everything is fine")
    payload = {
        "reply": "Created successfully, verified",
        "actions": [json.loads(result.model_dump_json())],
    }
    assert state_of(payload) == "UNKNOWN"


def test_http200_alone_never_renders_verified(state_of):
    """A body with no actions at all is not evidence of anything finishing."""
    assert state_of({"decision": "EXECUTE", "actions": []}) == "UNKNOWN"


def test_client_supplied_verified_field_is_not_trusted(state_of):
    """The renderer must not read a state the caller asserts about itself."""
    forged = {"verified": True, "status": "VERIFIED", "verification": "success", "actions": []}
    assert state_of(forged) == "UNKNOWN"


# --------------------------------------------------- FAILED / UNAVAILABLE /
# --------------------------------------------------- SIMULATED / UNKNOWN
# from real backend outcomes, not invented fixtures


async def test_real_failed_action_renders_failed(core, tmp_area, state_of):
    missing = tmp_area / "p13-nope.txt"
    verdict = core.policy.evaluate(PlannedAction(
        step_id=1, tool="delete_file", params={"path": str(missing)},
        device="laptop_main"))
    resolved = await core.resolve_confirmation(
        verdict.confirmation_id, True, session_id="p13")

    payload = _payload(resolved)
    assert payload["actions"][0]["verification"] == "failed"
    assert state_of(payload) == "FAILED"


async def test_unreachable_calendar_renders_unavailable_not_failed(core, monkeypatch, state_of):
    """Phase 11's offline-as-empty fix, seen through the UI.

    "Could not reach the server" and "the server answered and refused" must not
    collapse into the same chip.
    """
    # All three, because the provider only speaks CalDAV when it is configured
    # to; a URL alone leaves it on the local store, which really is empty.
    settings = get_settings()
    monkeypatch.setattr(settings, "calendar_url", "http://127.0.0.1:9/dav")
    monkeypatch.setattr(settings, "calendar_user", "nobody")
    monkeypatch.setattr(settings, "calendar_password", "nopass")
    result = await core.tools.handler("list_events")(_ctx(core))

    assert result.verification.value == "failed"
    assert result.output["provider_status"] == "unavailable"
    assert state_of({"actions": [json.loads(result.model_dump_json())]}) == "UNAVAILABLE"


async def test_simulated_actuation_is_not_shown_as_verified(core, monkeypatch, state_of):
    """The UI must not launder a known backend overstatement.

    A simulated home command currently records verification=SUCCESS -- that gap
    is carried forward from Phase 12 and is deliberately NOT fixed here. What is
    proven is that the renderer keys on ``output.simulated`` and so shows the
    truth the record does not.
    """
    from jarvis.home import reset_home_gateway

    gateway = HomeGateway(simulate=True)
    reset_home_gateway(gateway)
    try:
        result = await core.tools.handler("control_home_device")(
            _ctx(core), device="kitchen light", action="on")
    finally:
        reset_home_gateway(None)

    assert result.output["simulated"] is True
    assert result.verification.value == "success", (
        "the backend overstatement this test guards against has been fixed; "
        "this test can now be simplified rather than deleted")

    payload = {"actions": [json.loads(result.model_dump_json())]}
    assert state_of(payload) == "SIMULATED"
    assert state_of(payload) != "VERIFIED"


async def test_partial_forget_renders_unknown_not_verified(core, state_of):
    """A memory deletion that left a copy behind must not read as done."""
    # Through the pipeline, not the tool: the spoken path is what writes the
    # second copy (the session echo), and a single-row fact really does forget
    # cleanly -- asserting UNKNOWN on that would be testing the wrong thing.
    await core.handle(CommandRequest(
        text="remember that phase13 canary fact QK72ZZ", source="test"))
    rows = core.db.query(
        "SELECT id, scope FROM memories WHERE content LIKE '%QK72ZZ%' ORDER BY rowid")
    assert len(rows) >= 2, (
        f"expected the fact row plus an echo, got {len(rows)} -- the duplication "
        "this test guards against may no longer happen, so simplify it")
    named = [r for r in rows if r["scope"] == "long_term"] or rows
    try:
        forgotten = await core.tools.handler("forget_memory")(
            _ctx(core), memory_id=named[0]["id"])
        payload = {"actions": [json.loads(forgotten.model_dump_json())]}
        assert state_of(payload) in ("UNKNOWN", "FAILED"), forgotten.summary
        assert state_of(payload) != "VERIFIED"
    finally:
        for row in core.db.query(
                "SELECT id FROM memories WHERE content LIKE '%QK72ZZ%'"):
            core.db.execute("DELETE FROM memories WHERE id = ?", (row["id"],))


# --------------------------------------------------------------- safety path


async def test_unsafe_paths_never_render_verified(core, state_of):
    for sentence in (
        "Create a file called ../phase13_escape.txt containing: SHOULD NOT EXIST",
        f"Create a file called {Path.home() / '.ssh' / 'phase13'} containing: nope",
        "Create a file called /etc/hosts containing: nope",
    ):
        response = await core.handle(CommandRequest(text=sentence, source="test"))
        state = state_of(_payload(response))
        assert state != "VERIFIED", sentence
        assert not Path.home().joinpath("phase13_escape.txt").exists()


def test_page_ships_the_vocabulary():
    """The words must actually be in the shipped file, not just reachable."""
    source = PAGE.read_text()
    for state in ("VERIFIED", "AWAITING CONFIRMATION", "EXECUTING", "PLANNED",
                  "FAILED", "UNKNOWN", "UNAVAILABLE", "SIMULATED"):
        assert state in source, f"{state} never reaches the page"
    assert 'id="state"' in source and "renderState(" in source
    assert "pipelineState(data)" in source, "the response is not mapped to state"
