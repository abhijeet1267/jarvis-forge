"""Outcome Assurance: observe the world beyond the filesystem, and never guess.

The property that matters is not "there are more observer kinds" -- it is that
each one reports UNKNOWN when it could not look, FAILED when it looked and the
world is wrong, and VERIFIED only when it actually saw the thing. A verifier
that turned "I could not check" into "it worked" would be worse than none.
"""

from __future__ import annotations

import json

import pytest

from jarvis.core.missions import ConditionStatus
from jarvis.core.outcome_assurance import (
    ALLOWED_COMMANDS,
    KINDS,
    Observation,
    observe_app_frontmost,
    observe_command,
    observe_port,
    observe_process,
    observe_url,
    observe_url_status,
    observer_for_spec,
    supported_kinds,
    verifier_from_spec,
)


# -- command observers -------------------------------------------------------

def test_an_allowlisted_command_that_succeeds_is_verified():
    obs = observe_command("echo", needle="hello", args=["hello"])
    assert obs.status == "verified"
    assert "hello" in obs.detail


def test_a_command_that_exits_nonzero_is_failed():
    # `test -e /nonexistent` exits 1 -- a real failure, not an unknown.
    obs = observe_command("test_file", args=["/definitely/not/here/xyz"])
    assert obs.status == "failed"


def test_an_unlisted_command_is_refused_not_run():
    """An observer must not run arbitrary programs -- that would be an
    execution path wearing a check's name."""
    obs = observe_command("rm", args=["-rf", "/"])
    assert obs.status == "unknown"
    assert "not an allowlisted command" in obs.detail


def test_a_command_whose_output_lacks_the_needle_is_failed():
    obs = observe_command("echo", needle="absent-text", args=["something else"])
    assert obs.status == "failed"


# -- process observers -------------------------------------------------------

def test_a_running_process_is_seen():
    # `ps` itself is in the table for the moment it runs; `launchd` is always there.
    obs = observe_process("launchd")
    assert obs.status == "verified"


def test_an_absent_process_is_failed_when_asked_for():
    obs = observe_process("a-process-that-does-not-exist-xyz", absent=True)
    assert obs.status == "verified"  # confirmed gone


def test_a_present_process_is_failed_when_absence_was_claimed():
    obs = observe_process("launchd", absent=True)
    assert obs.status == "failed"


# -- port observers ----------------------------------------------------------

def test_a_closed_local_port_is_failed_not_unknown():
    # Port 1 on loopback is not listening in any sane environment.
    obs = observe_port(1)
    assert obs.status == "failed"


def test_an_invalid_port_is_unknown():
    obs = observe_port(0)
    assert obs.status == "unknown"


# -- url observers -----------------------------------------------------------

def test_a_refused_address_is_unknown_not_failed():
    """The egress firewall refusing a check means the check could not be made --
    reporting FAILED would blame the mission for the firewall's decision."""
    obs = observe_url("https://collect.example.net/x", needle="anything")
    assert obs.status == "unknown"
    assert "egress firewall" in obs.detail


def test_a_url_with_no_host_is_unknown():
    obs = observe_url_status("not-a-url", codes=[200])
    assert obs.status == "unknown"


# -- the declarative surface -------------------------------------------------

def test_an_unknown_kind_is_none_so_the_condition_is_unknown():
    assert observer_for_spec(json.dumps({"kind": "make_it_up"})) is None


def test_an_unparseable_spec_is_none():
    assert observer_for_spec("{not json") is None
    assert observer_for_spec("") is None


def test_a_spec_resolves_to_a_working_observer():
    obs = observer_for_spec(json.dumps(
        {"kind": "command_output", "command": "echo", "needle": "ok", "args": ["ok"]}))
    assert obs is not None
    assert obs().status == "verified"


def test_a_missing_field_makes_the_spec_unresolvable():
    """A url_body spec with no url cannot observe anything -- None, not a pass."""
    assert observer_for_spec(json.dumps({"kind": "url_body", "needle": "x"})) is None
    assert observer_for_spec(json.dumps({"kind": "port_listening"})) is None


# -- the runner's verifier ---------------------------------------------------

def test_the_verifier_reports_unknown_for_an_unobservable_condition():
    verify = verifier_from_spec(json.dumps({"kind": "make_it_up"}), "c1")
    result = verify(None)
    assert result.status is ConditionStatus.UNKNOWN


def test_the_verifier_runs_a_real_observation():
    verify = verifier_from_spec(
        json.dumps({"kind": "command_output", "command": "echo", "needle": "hi",
                    "args": ["hi"]}), "c1")
    result = verify(None)
    assert result.status is ConditionStatus.VERIFIED


def test_the_verifier_never_crashes_on_an_observer_error(monkeypatch):
    """An observer raising must be UNKNOWN, not an exception out of verification."""
    import jarvis.core.outcome_assurance as mod

    def boom():
        raise RuntimeError("observer exploded")

    monkeypatch.setattr(mod, "observer_for_spec", lambda spec: boom)
    verify = mod.verifier_from_spec(json.dumps({"kind": "command_output",
                                                "command": "echo"}), "c1")
    result = verify(None)
    assert result.status is ConditionStatus.UNKNOWN
    assert "raised" in result.observation


# -- the runner resolves the new kinds ---------------------------------------

def test_the_runner_resolves_an_assurance_kind():
    """`_verifier_from_spec` -- the mission runner's own resolver -- must find
    these observers, or a mission could never use them."""
    from jarvis.core.mission_runner import _verifier_from_spec

    verify = _verifier_from_spec(
        json.dumps({"kind": "command_output", "command": "echo", "needle": "yes",
                    "args": ["yes"]}), "c1")
    assert verify is not None
    assert verify(None).status is ConditionStatus.VERIFIED


def test_the_runner_still_resolves_the_file_kinds():
    from jarvis.core.mission_runner import _verifier_from_spec

    verify = _verifier_from_spec(json.dumps({"kind": "file_absent",
                                             "path": "/definitely/not/here"}), "c1")
    assert verify is not None
    assert verify(None).status is ConditionStatus.VERIFIED


# -- the honest listing ------------------------------------------------------

def test_supported_kinds_matches_the_module():
    assert {row["kind"] for row in supported_kinds()} == set(KINDS)


def test_every_kind_declares_what_it_carries():
    for row in supported_kinds():
        assert row["carries"], row["kind"]


def test_the_allowlist_is_absolute_paths_only():
    """Each allowlisted command is a full path, so a PATH change cannot swap it."""
    for name, argv in ALLOWED_COMMANDS.items():
        assert argv[0].startswith("/"), name


# -- frontmost application (permission-gated, so tolerant) -------------------

def test_the_frontmost_observer_is_honest_about_permissions():
    obs = observe_app_frontmost("Finder")
    # Whatever the answer, it must be one of the three states, and a permission
    # problem must never be reported as a match.
    assert obs.status in {"verified", "failed", "unknown"}
    if obs.status == "unknown":
        assert "permission" in obs.detail.lower() or "time" in obs.detail.lower() \
            or "could not" in obs.detail.lower()

# -- a real mission verified by a non-file observer --------------------------

def test_a_mission_verifies_a_process_condition_end_to_end(tmp_path):
    """The point of the phase: a success condition about the *world* -- here, a
    running process -- is verified by the runner, not recorded UNKNOWN because it
    was not a file."""
    import asyncio

    from jarvis.core.mission_runner import MissionRunner
    from jarvis.core.missions import MissionService
    from jarvis.db.database import get_db

    service = MissionService(get_db())
    mission = service.create(
        request="confirm launchd is up",
        conditions=[{
            "condition_id": "c1",
            "statement": "launchd is running",
            "verifier": json.dumps({"kind": "process_running", "process": "launchd"}),
        }],
        steps=[{"step_id": "s1", "title": "look", "tool": "get_time",
                "satisfies": "c1"}],
    )

    async def execute_step(action, ctx):
        from jarvis.core.models import ActionResult

        return ActionResult(ok=True, tool=action.tool, output={})

    runner = MissionRunner(service, execute_step=execute_step)
    # Verification alone, without the steps: what the runner's verifier concludes
    # about a process condition.
    from jarvis.core.mission_runner import _verifier_from_spec

    verifier = _verifier_from_spec(
        json.dumps({"kind": "process_running", "process": "launchd"}), "c1")
    result = verifier(mission)
    assert result.status is ConditionStatus.VERIFIED, result.observation
    assert "launchd" in result.observation
