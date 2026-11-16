"""The native desktop executor and the tools that drive it.

Two things are load-bearing here and both are asserted by measurement rather
than by a comment:

1. **A desktop tool never claims success it did not observe.** The executor
   distinguishes an event that was *accepted* from an outcome that was *read
   back*, and the tool layer must preserve that distinction — otherwise the
   product reports a click that moved nothing, which is the failure mode this
   whole subsystem exists to prevent.
2. **The registry contains no phantom capability.** A tool that is advertised
   but whose handler does nothing is worse than a missing tool: the planner will
   reach for it. Every advertised desktop tool must resolve to a real handler,
   declare a risk, and declare its parameters.

The tests use a stub executor so they run with or without macOS input
permission. The live behaviour is covered by the acceptance scripts in
``benchmarks/v14_desktop/``; these tests pin the *contract*.
"""

from __future__ import annotations

import asyncio

import pytest

from jarvis.config import get_settings
from jarvis.core.models import RiskLevel
from jarvis.tools import build_default_registry
from jarvis.tools import desktop_native
from jarvis.tools.base import ToolContext


@pytest.fixture()
def ctx():
    return ToolContext(settings=get_settings(), device_id="laptop")


@pytest.fixture()
def registry():
    return build_default_registry()


def _call(registry, ctx, name, **params):
    handler = registry.handler(name)
    assert handler is not None, f"{name} is advertised but has no handler"
    return asyncio.run(handler(ctx, **params))


# -- the registry has no phantom tools ---------------------------------------

def test_every_desktop_tool_resolves_to_a_real_handler(registry):
    """The recorder defect, generalised: a manifest name with no owner."""
    names = [n for n in registry.names() if n.startswith("desktop_")]
    assert len(names) >= 12, f"expected the full desktop set, found {len(names)}"
    for name in names:
        spec = registry.get(name)
        handler = registry.handler(name)
        assert spec is not None, name
        assert handler is not None, f"{name} is advertised with no implementation"
        assert callable(handler), name


def test_every_desktop_tool_declares_risk_and_parameters(registry):
    for name in [n for n in registry.names() if n.startswith("desktop_")]:
        spec = registry.get(name)
        assert spec.description.strip(), f"{name} has no description"
        assert isinstance(spec.risk, RiskLevel), name
        # A tool with required parameters must declare them, or a caller cannot
        # know what to send and a validation gap becomes a runtime failure.
        for param in getattr(spec, "required_parameters", ()) or ():
            assert param in spec.parameters, f"{name} requires undeclared {param}"


def test_a_visible_action_is_not_ranked_as_a_read(registry):
    """Moving the pointer changes what the user sees, so it is not READ.

    It is LOW rather than EXTERNAL, which is a deliberate correction: the
    project's invariant is that *every* EXTERNAL and DESTRUCTIVE tool is
    confirmation-gated, enforced by three separate phase tests. Ranking the move
    EXTERNAL while leaving it ungated broke that invariant, and the honest way
    out is to rank it where it belongs, not to weaken the gate. A pointer move
    is reversible and less consequential than `close_app` or `browser_open`,
    both of which are LOW.
    """
    move = registry.get("desktop_move_pointer")
    assert move.risk is RiskLevel.LOW
    assert move.risk > RiskLevel.READ
    assert move.requires_confirmation is False  # it is reversible and visible


def test_nothing_ranked_external_is_ungated(registry):
    """The project-wide invariant, asserted on the desktop tools as well."""
    for spec in registry.all():
        if spec.risk in (RiskLevel.EXTERNAL, RiskLevel.DESTRUCTIVE):
            assert spec.requires_confirmation, (
                f"{spec.name} changes something outside and does not ask first")


def test_anything_that_lands_on_the_users_session_asks_first(registry):
    """A click or a keystroke hits whatever is focused and cannot be undone."""
    for name in ("desktop_click", "desktop_double_click", "desktop_right_click",
                 "desktop_drag", "desktop_type_text", "desktop_press_key",
                 "desktop_shortcut", "desktop_press_element", "desktop_scroll"):
        spec = registry.get(name)
        assert spec.risk is RiskLevel.DESTRUCTIVE, name
        assert spec.requires_confirmation is True, f"{name} must ask first"
        assert spec.reversible is False or name == "desktop_scroll", name


def test_reading_the_desktop_needs_no_confirmation(registry):
    for name in ("desktop_permissions", "desktop_inspect_ui",
                 "desktop_observe", "desktop_verify_change"):
        spec = registry.get(name)
        assert spec.risk is RiskLevel.READ, name
        assert spec.requires_confirmation is False, name


# -- performed is not verified ------------------------------------------------

@pytest.fixture()
def stub(monkeypatch):
    """Replace the executor with a programmable stub, so no real input is sent."""
    calls = []

    def make(result):
        def _stub(*args, **kwargs):
            calls.append((args, kwargs))
            return dict(result)
        return _stub

    return calls, make, monkeypatch


def test_an_accepted_event_is_reported_unverified_not_verified(stub, registry, ctx):
    """A posted click that was not observed is `unverified`, never `verified`."""
    calls, make, mp = stub
    mp.setattr(desktop_native.executor, "click",
               make({"status": "performed_unverified",
                     "reason": "click posted at (10, 20)"}))
    result = _call(registry, ctx, "desktop_click", x=10, y=20)
    assert result.ok is True
    assert result.verification.value == "unknown", \
        "an unobserved action must not be reported as success"


def test_an_observed_change_is_the_only_thing_reported_verified(stub, registry, ctx):
    calls, make, mp = stub
    mp.setattr(desktop_native.executor, "verify_change",
               make({"status": "verified", "reason": "count reports 3"}))
    result = _call(registry, ctx, "desktop_verify_change",
                   keyword="click count", key="value", value="3")
    assert result.ok is True
    assert result.verification.value == "success"


def test_a_missing_permission_is_a_refusal_not_a_silent_noop(stub, registry, ctx):
    calls, make, mp = stub
    mp.setattr(desktop_native.executor, "click",
               make({"status": "permission_required",
                     "reason": "clicking needs permission to post input events",
                     "needs": "post_events"}))
    result = _call(registry, ctx, "desktop_click", x=10, y=20)
    assert result.ok is False
    # The reason has to reach the person, not sit in an empty message field.
    text = str(result.message or result.error)
    assert "permission" in text.lower()
    assert "did not act" in text.lower()


def test_the_refusal_names_the_executable_that_needs_the_grant(stub, registry, ctx):
    """A bare "denied" leaves the user with no next step; the subject is the
    whole point of a per-process permission."""
    calls, make, mp = stub
    mp.setattr(desktop_native.executor, "type_text",
               make({"status": "permission_required",
                     "reason": "typing needs permission to post input events and "
                               "macOS has not granted it to this executable (vantage-desktop)"}))
    result = _call(registry, ctx, "desktop_type_text", text="x")
    text = str(result.message or result.error)
    assert "vantage-desktop" in text


def test_a_target_that_does_not_exist_is_reported_as_such(stub, registry, ctx):
    calls, make, mp = stub
    mp.setattr(desktop_native.executor, "press_element",
               make({"status": "target_not_found",
                     "reason": "no element matches 'Save'"}))
    result = _call(registry, ctx, "desktop_press_element", keyword="Save")
    assert result.ok is False
    text = str(result.message or result.error)
    assert "could not find" in text.lower()


def test_a_timeout_is_not_reported_as_success(stub, registry, ctx):
    calls, make, mp = stub
    mp.setattr(desktop_native.executor, "move",
               make({"status": "timed_out", "reason": "the executor did not answer"}))
    result = _call(registry, ctx, "desktop_move_pointer", x=1, y=2)
    assert result.ok is False


# -- input validation happens before anything is sent -------------------------

def test_out_of_range_coordinates_are_refused_before_the_executor_runs(stub, registry, ctx):
    calls, make, mp = stub
    mp.setattr(desktop_native.executor, "move", make({"status": "verified", "reason": "ok"}))
    result = _call(registry, ctx, "desktop_move_pointer", x=999999, y=2)
    assert result.ok is False
    assert calls == [], "an invalid coordinate must not reach the executor"


def test_a_drag_without_all_four_points_is_refused(stub, registry, ctx):
    calls, make, mp = stub
    mp.setattr(desktop_native.executor, "drag", make({"status": "verified", "reason": "ok"}))
    result = _call(registry, ctx, "desktop_drag", from_x=1, from_y=2, to_x=3)
    assert result.ok is False
    assert calls == []
    text = str(result.message or result.error)
    assert "to_y" in text


def test_a_shortcut_without_a_modifier_is_refused(stub, registry, ctx):
    """A 'shortcut' with no modifier is a bare keystroke; letting it through
    would type into a field while claiming a safe, known action."""
    calls, make, mp = stub
    mp.setattr(desktop_native.executor, "press_key", make({"status": "verified", "reason": "ok"}))
    result = _call(registry, ctx, "desktop_shortcut", key="s")
    assert result.ok is False
    assert calls == []


def test_typing_more_than_the_bound_is_refused(stub, registry, ctx):
    calls, make, mp = stub
    mp.setattr(desktop_native.executor, "type_text", make({"status": "verified", "reason": "ok"}))
    result = _call(registry, ctx, "desktop_type_text",
                   text="x" * (desktop_native.MAX_TEXT_CHARS + 1))
    assert result.ok is False
    assert calls == []


def test_a_zero_line_scroll_is_refused(stub, registry, ctx):
    calls, make, mp = stub
    mp.setattr(desktop_native.executor, "scroll", make({"status": "verified", "reason": "ok"}))
    result = _call(registry, ctx, "desktop_scroll", x=1, y=2, lines=0)
    assert result.ok is False
    assert calls == []


def test_scroll_lines_are_bounded_even_when_the_caller_is_greedy(stub, registry, ctx):
    """A scroll of 10_000 lines would be a way to hammer an application."""
    calls, make, mp = stub
    mp.setattr(desktop_native.executor, "scroll", make({"status": "performed_unverified",
                                                        "reason": "scrolled"}))
    _call(registry, ctx, "desktop_scroll", x=1, y=2, lines=10_000)
    assert calls, "the bounded scroll should still have been sent"
    _, kwargs = calls[0]
    assert abs(kwargs.get("lines", 0)) <= 50


# -- the executor bridge itself ----------------------------------------------

def test_the_bridge_reports_a_missing_executor_as_a_permission_problem(monkeypatch):
    """No binary is a missing capability, not a crash and not a silent success."""
    monkeypatch.delenv("VANTAGE_DESKTOP_EXECUTOR", raising=False)
    monkeypatch.setattr("jarvis.desktop.executor.executor_path", lambda: None)
    from jarvis.desktop import executor

    result = executor.click(1, 2)
    assert result["status"] == "permission_required"
    assert "not built" in result["reason"]


def test_the_bridge_passes_through_an_unparseable_answer_as_unknown(monkeypatch, tmp_path):
    """A helper that prints garbage must not read as a success."""
    script = tmp_path / "bad"
    script.write_text("#!/bin/sh\necho not-json\n")
    script.chmod(0o755)
    monkeypatch.setenv("VANTAGE_DESKTOP_EXECUTOR", str(script))
    from jarvis.desktop import executor

    result = executor.permissions()
    assert result["status"] == "unknown"


def test_not_acted_covers_every_status_that_means_nothing_happened():
    from jarvis.desktop import executor

    for status in ("permission_required", "blocked", "target_not_found",
                   "target_ambiguous", "cancelled", "timed_out"):
        assert status in executor.NOT_ACTED, status
    # The two that DO mean something happened must not be in it.
    assert "verified" not in executor.NOT_ACTED
    assert "performed_unverified" not in executor.NOT_ACTED

# -- coordinate validation must not be built on an empty display list ---------

def test_a_valid_on_screen_point_is_not_refused(registry, ctx):
    """The real bug this pins: `CGGetActiveDisplayList` reports 0 displays on
    this machine while `CGGetOnlineDisplayList` reports the real one, so a bound
    check built on the wrong API refused every coordinate with "not on any
    display" — a validation bug wearing the costume of a safety feature.

    This asserts the live executor finds at least one display and accepts a
    point inside it. It is skipped, never faked, when no executor is built.
    """
    from jarvis.desktop import executor

    if executor.executor_path() is None:
        pytest.skip("the native executor is not built")

    posture = executor.permissions()
    displays = posture.get("displays") or []
    assert displays, (
        "the executor reports no displays; coordinate validation would refuse "
        "every point, including valid ones")

    first = displays[0]
    inside_x = first["x"] + first["width"] / 2
    inside_y = first["y"] + first["height"] / 2
    result = _call(registry, ctx, "desktop_move_pointer", x=inside_x, y=inside_y)
    detail = (getattr(result, "output", None) or {}).get("desktop_result", {})
    assert detail.get("status") != "blocked", (
        f"a point at the centre of a reported display was refused: {detail.get('reason')}")


def test_a_point_off_every_display_is_refused(registry, ctx):
    """The bound check still has to reject a coordinate that is nowhere."""
    from jarvis.desktop import executor

    if executor.executor_path() is None:
        pytest.skip("the native executor is not built")
    posture = executor.permissions()
    if not posture.get("displays"):
        pytest.skip("no display reported")

    # Inside the tool's own MAX_COORDINATE, but outside every display: this is
    # the case the display check exists for. A value above MAX_COORDINATE is
    # caught earlier, by the range guard, which is a different refusal.
    result = _call(registry, ctx, "desktop_move_pointer", x=31_000, y=31_000)
    detail = (getattr(result, "output", None) or {}).get("desktop_result", {})
    assert detail.get("status") == "blocked", detail


# -- a takeover must stop this surface too -----------------------------------

def test_a_takeover_refuses_a_desktop_action_from_the_http_surface():
    """The desktop routes were a second door: they called the executor directly,
    so a human takeover stopped the mission runner while a click from this
    surface still went through. Measured in the V14 acceptance run before this
    guard existed — a pointer move succeeded *during* a takeover."""
    from fastapi.testclient import TestClient
    from jarvis.api.app import app
    from tests.conftest import TEST_TOKEN

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}

    # The tower registers workers from the live agent set; a bare test process
    # has none yet, and `admit()` returns None (not a refusal) for a tool no
    # worker owns. Reaching the control-tower endpoint first is what a real
    # client does, and it is what makes this test exercise the gate rather than
    # the empty-registry short circuit.
    client.get("/control-tower", headers=headers)

    client.post("/reality/takeover", headers=headers, json={})
    try:
        refused = client.post("/desktop/move", headers=headers, json={"x": 10, "y": 10})
        assert refused.status_code == 409, (
            "a desktop action was dispatched while a human takeover was in force")
        assert "paused" in refused.json().get("detail", "").lower()
    finally:
        client.post("/reality/release", headers=headers)

    # And after release it is admitted again (it may still fail on permission,
    # which is a different refusal and must not be a 409).
    resumed = client.post("/desktop/move", headers=headers, json={"x": 10, "y": 10})
    assert resumed.status_code in (200, 500, 503), resumed.status_code
