"""A read reports its state; a change keeps the "could not verify" caveat.

Measured defect: "is it running?" answered "Calculator is installed (version
12.0) and is running, but I could not verify it happened." Every field was
honest -- the tool reported verification=UNKNOWN because a read never claims to
alter the world -- but the composed sentence contradicted the fact it had just
stated.

The rule: the caveat belongs to a tool that changed something. A read-only tool's
message IS the observation, so the caveat is dropped there and kept everywhere
else.
"""

from __future__ import annotations

import pytest

from jarvis.core.models import CommandResponse, RiskLevel, Verification
from jarvis.core.orchestrator import JarvisCore
from jarvis.tools.base import ok


@pytest.fixture
def core():
    return JarvisCore()


def _response(*results):
    return CommandResponse(session_id="s", request_id="R", actions=list(results))


def test_a_read_is_not_given_the_caveat(core):
    """check_agent_status is READ-tier: its message is the observation."""
    spec = core.tools.get("check_agent_status")
    assert spec is not None and spec.risk == RiskLevel.READ
    result = ok("check_agent_status",
                "Calculator is installed (version 12.0) and is running.")
    assert result.verification is Verification.UNKNOWN
    reply = core._compose_reply("is it running", _response(result))
    assert "could not verify" not in reply, (
        f"a read was caveated as unverified: {reply!r}")
    assert "is running" in reply


def test_a_change_keeps_the_caveat(core):
    """close_app is not READ-tier: the caveat is the truth for it."""
    spec = core.tools.get("close_app")
    assert spec is not None and spec.risk != RiskLevel.READ
    result = ok("close_app", "Quit Calculator")
    assert result.verification is Verification.UNKNOWN
    reply = core._compose_reply("close it", _response(result))
    assert "could not verify it happened" in reply, (
        f"a world-changing action lost its caveat: {reply!r}")


def test_a_verified_result_is_stated_plainly(core):
    result = ok("check_agent_status", "Calculator is running", verified=True)
    assert result.verification is Verification.SUCCESS
    reply = core._compose_reply("is it running", _response(result))
    assert "could not verify" not in reply


def test_an_unknown_tool_keeps_the_caveat(core):
    """A tool with no spec cannot be shown to be a read, so it keeps the caveat."""
    result = ok("not_a_registered_tool", "something happened")
    reply = core._compose_reply("do the thing", _response(result))
    assert "could not verify it happened" in reply, (
        "an unknown tool was treated as a read without evidence")


def test_the_observation_check_reads_the_registry_not_a_list(core):
    """The signal is the spec's risk tier, not a hardcoded name list."""
    assert core._is_observation(ok("check_agent_status", "x")) is True
    assert core._is_observation(ok("close_app", "x")) is False
    assert core._is_observation(ok("no_such_tool", "x")) is False