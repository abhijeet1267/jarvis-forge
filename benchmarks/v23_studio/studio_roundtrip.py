#!/usr/bin/env python3
"""Record a 3-step desktop workflow in Automation Studio, move the target window,
and replay it -- verifying the target, not the mission status.

The v21 studio_replay established that a single recorded ``desktop_click``
replays through the mission runner and moves a fixture counter. What was never
established is the *multi-step* case: several recorded steps, saved as one
workflow, replayed after the world moved underneath the recording.

The question that matters is coordinate freshness. The recorder stores absolute
global points. Nothing in the recorder re-resolves them at replay time, so after
a window moves, a replay of the recorded values aims at where the button *was*.
This script measures that instead of assuming either answer:

  1. record 3 steps (move pointer away, move to the button, click it)
  2. replay the derived draft as recorded, and check the fixture's own counter
  3. move the fixture window, then ask the Studio's own review endpoint for a
     draft whose values are the *freshly resolved* button centre
  4. replay that, and confirm the mission's steps carry the fresh values --
     not the recorded ones -- and that the counter and pointer moved again

Every effect is read from the target: the fixture's counter in
``/tmp/vantage_probe_app/state.json`` and the real pointer position read back by
the native executor. A step reporting ``done`` is never the evidence.

When no fixture is running the script still runs: it records three pointer
moves to distinct on-screen points and verifies each one by its readback.

Port discipline: the installed app's backend owns 8765 and is refused. The
back end this drives is expected to be *already running* on a test port from
``scripts/dev_port_guard.sh``'s range; ``VANTAGE_TEST_PORT`` selects it.

    VANTAGE_TEST_PORT=8798 .venv/bin/python benchmarks/v23_studio/studio_roundtrip.py

Exit codes: 0 every check that could run passed; 1 a check failed; 2 blocked
(no backend, no executor permission, or the window could not be moved/observed).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[2]
PORT_GUARD = REPO / "scripts" / "dev_port_guard.sh"
FORBIDDEN_PORT = 8765
TOKEN_FILE = Path(os.environ.get("VANTAGE_TEST_TOKEN_FILE",
                                 "/tmp/vantage_probe2/api_token"))
STATE = Path("/tmp/vantage_probe_app/state.json")
FIXTURE = "vantage-probe"
BUTTON_LABEL = "Click me"

#: Global points, top-left origin -- the space every executor operation accepts.
POINT_TOLERANCE = 2.0

results: List[Tuple[str, str, str]] = []


def record(name: str, status: str, detail: str) -> None:
    results.append((name, status, detail))
    print(f"  {status:8s} {name}: {detail}", flush=True)


def run(cmd: List[str], timeout: float = 20.0) -> Tuple[int, str]:
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


# ---------------------------------------------------------------------------
# The native executor: the only process macOS authorises to act on the desktop
# ---------------------------------------------------------------------------
def executor_path() -> Optional[Path]:
    """The same order ``jarvis.desktop.executor`` uses: granted bundle first.

    The bundle is first because that is the code identity the person granted in
    System Settings; the bare binary is a different identity and is often not
    granted.
    """
    override = os.environ.get("VANTAGE_DESKTOP_EXECUTOR")
    candidates = [
        Path(override) if override else None,
        REPO / "native" / "desktop_executor" / "VantageDesktopDev.app" / "Contents"
        / "MacOS" / "vantage-desktop",
        REPO / "native" / "desktop_executor" / "vantage-desktop",
        REPO / "build" / "vantage-desktop",
        Path.home() / ".jarvis-forge" / "bin" / "vantage-desktop",
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


EXECUTOR: Optional[Path] = executor_path()


def run_exec(operation: str, *args: str, timeout: float = 30.0) -> Dict[str, Any]:
    """Invoke the executor and return its structured result verbatim."""
    if EXECUTOR is None:
        return {"status": "missing", "reason": "the native desktop executor is not built"}
    code, out = run([str(EXECUTOR), operation, *args], timeout=timeout)
    for line in reversed(out.strip().splitlines()):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {"status": "unknown", "reason": f"unparseable output (exit {code}): {out[:200]}"}


def read_cursor() -> Optional[Tuple[float, float]]:
    """The real pointer position, read by the process that would move it."""
    posture = run_exec("permissions")
    cursor = posture.get("cursor") or {}
    try:
        return float(cursor["x"]), float(cursor["y"])
    except (KeyError, TypeError, ValueError):
        return None


def fixture_windows() -> List[Dict[str, Any]]:
    out = run_exec("windows", f"application={FIXTURE}")
    return list(out.get("windows") or [])


def fixture_running() -> bool:
    if fixture_windows():
        return True
    code, _ = run(["pgrep", "-f", "tools/input_probe/vantage-probe"])
    return code == 0


def resolve_button() -> Optional[Tuple[float, float]]:
    """The live centre of the fixture's button, read through Accessibility.

    This is the freshness source that matters: AX reads where the control *is
    now*. The fixture's own ``state.json`` targets are cached and -- measured --
    do not follow a window move.
    """
    out = run_exec("read_element", f"application={FIXTURE}",
                   f"keyword={BUTTON_LABEL}", "role=AXButton")
    element = out.get("element") or {}
    if out.get("status") != "verified" or not element:
        return None
    try:
        return float(element["center_x"]), float(element["center_y"])
    except (KeyError, TypeError, ValueError):
        return None


def frontmost_application() -> str:
    """What the window server says is in front -- the executor's own readback."""
    return str(run_exec("windows").get("frontmost_application") or "").lower()


def focus_fixture() -> bool:
    """Put the fixture in front, and report whether the window server agrees.

    Activating is not proof: macOS 14+ refuses ``NSRunningApplication.activate``
    from a command-line tool, so the executor's own focus op reports
    ``frontmost_after`` unchanged. System Events' ``set frontmost`` is the route
    that works, and the check still asks the window server rather than trusting
    the AppleScript exit code. A click posted while another app is frontmost
    lands in that app.
    """
    if FIXTURE in frontmost_application():
        return True
    for _ in range(6):
        run_exec("focus", f"application={FIXTURE}")
        if FIXTURE in frontmost_application():
            return True
        run(["osascript", "-e",
             f'tell application "System Events" to set frontmost of '
             f'(first process whose name contains "{FIXTURE}") to true'],
            timeout=15.0)
        time.sleep(0.4)
        if FIXTURE in frontmost_application():
            return True
    return False


def move_fixture_window(x: int, y: int) -> Tuple[bool, str]:
    script = (f'tell application "System Events" to tell process "{FIXTURE}" '
              f'to set position of window 1 to {{{int(x)}, {int(y)}}}')
    code, out = run(["osascript", "-e", script], timeout=15.0)
    return code == 0, out.strip()


def wait_for_button_move(previous: Tuple[float, float],
                         timeout: float = 5.0) -> Optional[Tuple[float, float]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        point = resolve_button()
        if point is not None and (abs(point[0] - previous[0]) > POINT_TOLERANCE
                                  or abs(point[1] - previous[1]) > POINT_TOLERANCE):
            return point
        time.sleep(0.2)
    return None


def fixture_state() -> Dict[str, Any]:
    """The fixture's own report, retried because it rewrites the file in place."""
    for _ in range(40):
        try:
            return json.loads(STATE.read_text())
        except (OSError, json.JSONDecodeError):
            time.sleep(0.05)
    return {}


# ---------------------------------------------------------------------------
# The backend: an already-running test instance, never the app's 8765
# ---------------------------------------------------------------------------
def choose_port() -> Tuple[Optional[int], str]:
    raw = os.environ.get("VANTAGE_TEST_PORT", "").strip()
    if raw:
        try:
            port = int(raw)
        except ValueError:
            return None, f"VANTAGE_TEST_PORT={raw!r} is not a port number"
        if port == FORBIDDEN_PORT:
            return None, f"{FORBIDDEN_PORT} is the installed app's backend; refusing to use it"
        return port, f"VANTAGE_TEST_PORT={port}"
    if not PORT_GUARD.is_file():
        return None, f"the port guard is missing at {PORT_GUARD}"
    code, out = run([str(PORT_GUARD), "claim"])
    text = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if code != 0 or not text.isdigit():
        return None, f"`dev_port_guard.sh claim` exited {code}: {out.strip()!r}"
    port = int(text)
    if port == FORBIDDEN_PORT:
        return None, f"the port guard offered the forbidden port {FORBIDDEN_PORT}"
    return port, f"scripts/dev_port_guard.sh claim -> {port}"


def http_call(base: str, headers: Dict[str, str], method: str, path: str,
              body: Any = None) -> Tuple[int, Dict[str, Any]]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read() or b"{}"
            return response.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except (ValueError, OSError):
            return exc.code, {}
    except (urllib.error.URLError, OSError) as exc:
        return 0, {"detail": f"{type(exc).__name__}: {exc}"}


MOVE_READBACK = re.compile(r"real pointer is at \((-?\d+(?:\.\d+)?), (-?\d+(?:\.\d+)?)\)")
CLICK_POSTED = re.compile(r"click posted at \((-?\d+(?:\.\d+)?), (-?\d+(?:\.\d+)?)\)")


def near(expected: Tuple[float, float], actual: Optional[Tuple[float, float]]) -> bool:
    if actual is None:
        return False
    return (abs(actual[0] - expected[0]) <= POINT_TOLERANCE
            and abs(actual[1] - expected[1]) <= POINT_TOLERANCE)


def launch_and_approve(base, headers, request: str, steps: List[Dict[str, Any]],
                       conditions: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], str]:
    """Create a mission from the draft's steps and run it through the gate.

    A DESTRUCTIVE step parks at ``awaiting_approval``; the confirmation id the
    runner recorded on the step is the only one an approval can match, so it is
    read back from the step detail and offered unchanged.
    """
    if not focus_fixture() and fixture_running():
        return None, "the fixture would not come to the front; not dispatching input"
    status, mission = http_call(base, headers, "POST", "/missions",
                                {"request": request, "conditions": conditions,
                                 "steps": steps})
    if status != 200:
        return None, f"POST /missions -> {status}: {mission.get('detail')}"
    mission_id = mission["mission_id"]
    status, out = http_call(base, headers, "POST", f"/missions/{mission_id}/run", {})
    if status != 200:
        return None, f"POST /missions/{mission_id}/run -> {status}: {out.get('detail')}"
    approvals = 0
    while out.get("state") == "awaiting_approval" and approvals < 6:
        confirmation = None
        for step in out.get("steps") or []:
            match = re.search(r"confirmation\s+(\S+)", str(step.get("detail") or ""))
            if match:
                confirmation = match.group(1)
        if not confirmation:
            return out, "parked for approval but no confirmation id was recorded"
        status, out = http_call(base, headers, "POST", f"/missions/{mission_id}/run",
                                {"approval": {"confirmation_id": confirmation}})
        if status != 200:
            return None, f"approval run -> {status}: {out.get('detail')}"
        approvals += 1
    return out, ""


def mission_steps(draft_steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"step_id": f"S{index + 1}",
             "title": str(step.get("title") or step.get("tool") or ""),
             "tool": str(step.get("tool") or ""),
             "params": dict(step.get("params") or {}),
             "satisfies": str(step.get("satisfies") or "")}
            for index, step in enumerate(draft_steps)]


def observed_points(mission: Dict[str, Any]) -> Dict[int, Tuple[float, float]]:
    """Where each step's executor read the pointer, keyed by step position."""
    observed: Dict[int, Tuple[float, float]] = {}
    for index, step in enumerate(mission.get("steps") or []):
        detail = str(step.get("detail") or "")
        match = MOVE_READBACK.search(detail) or CLICK_POSTED.search(detail)
        if match:
            observed[index] = (float(match.group(1)), float(match.group(2)))
    return observed


def as_point(params: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    try:
        return float(params["x"]), float(params["y"])
    except (KeyError, TypeError, ValueError):
        return None


def same_point(left: Optional[Tuple[float, float]],
               right: Optional[Tuple[float, float]]) -> bool:
    return left is not None and right is not None and near(left, right)


# ---------------------------------------------------------------------------
# The benchmark
# ---------------------------------------------------------------------------
def main() -> int:
    print("=" * 74)
    print("Automation Studio: a 3-step workflow, replayed after the target moved")
    print("=" * 74)

    port, port_note = choose_port()
    if port is None:
        record("port", "BLOCKED", port_note)
        return 2
    base = f"http://127.0.0.1:{port}"
    record("port", "PASS", f"{port_note}; 8765 refused")

    status, identity = http_call(base, {}, "GET", "/health/identity")
    service = (identity.get("identity") or {}).get("service") if status == 200 else None
    if service != "vantage-backend":
        record("backend", "BLOCKED",
               f"nothing answering as vantage-backend on {base} (status {status}). "
               f"Start the test backend on this port, or set VANTAGE_TEST_PORT to "
               f"the one already running.")
        return 2
    record("backend", "PASS", f"{service} pid "
                              f"{(identity.get('identity') or {}).get('pid')} "
                              f"revision {(identity.get('identity') or {}).get('revision')}")

    if not TOKEN_FILE.is_file():
        record("token", "BLOCKED", f"no API token at {TOKEN_FILE}")
        return 2
    headers = {"X-JARVIS-Token": TOKEN_FILE.read_text().strip(),
               "Host": "127.0.0.1", "Content-Type": "application/json"}
    status, health = http_call(base, headers, "GET", "/health")
    if status != 200:
        record("token", "BLOCKED", f"the token at {TOKEN_FILE} was refused ({status})")
        return 2
    record("token", "PASS", f"{health.get('tools')} tools, {len(health.get('agents') or [])} agents")

    posture = run_exec("permissions")
    can_act = (posture.get("status") == "verified"
               and posture.get("can_move_cursor") and posture.get("post_events"))
    if not can_act:
        record("desktop permission", "BLOCKED",
               f"the executor cannot post input: {posture.get('reason')}")
        return 2
    displays = posture.get("displays") or []
    screen = displays[0] if displays else {}
    record("desktop permission", "PASS",
           f"post_events and cursor movement allowed for {posture.get('process_name')}")

    has_fixture = fixture_running()
    windows_before = fixture_windows()
    if has_fixture and not windows_before:
        record("fixture", "BLOCKED",
               "a vantage-probe process exists but publishes no on-screen window")
        return 2
    if not has_fixture and STATE.exists():
        record("fixture", "SKIPPED",
               "state.json exists but no vantage-probe process/window is running; "
               "using the pointer-readback path")

    # -- choose the workflow -------------------------------------------------
    recorded_point: Optional[Tuple[float, float]] = None
    neutral: Tuple[float, float]
    if has_fixture:
        if not focus_fixture():
            record("fixture focus", "BLOCKED", "the fixture would not come to the front")
            return 2
        recorded_point = resolve_button()
        if recorded_point is None:
            record("fixture", "BLOCKED",
                   "the fixture's button could not be read through Accessibility")
            return 2
        state = fixture_state()
        published = (state.get("targets") or {}).get("click") or {}
        record("fixture", "PASS",
               f"window {windows_before[0].get('width')}x{windows_before[0].get('height')} "
               f"at ({windows_before[0].get('x')}, {windows_before[0].get('y')}); "
               f"button centre (AX) ({recorded_point[0]:g}, {recorded_point[1]:g}); "
               f"state.json says ({published.get('x')}, {published.get('y')})")
        neutral = (min(float(screen.get("width") or 1352) - 20, recorded_point[0] + 240),
                   min(float(screen.get("height") or 878) - 20, recorded_point[1] + 240))
    else:
        width = float(screen.get("width") or 1352)
        height = float(screen.get("height") or 878)
        target_a = (width * 0.25, height * 0.25)
        target_b = (width * 0.5, height * 0.5)
        target_c = (width * 0.75, height * 0.75)
        neutral = (width * 0.5, height * 0.75)

    # -- 1. record three steps ----------------------------------------------
    status, current = http_call(base, headers, "GET", "/intelligence/recordings/current")
    open_recording = (current.get("recording") or {}).get("open")
    if open_recording:
        stop_status, _ = http_call(base, headers, "POST", "/intelligence/recordings/stop", {})
        record("recording cleanup", "PASS" if stop_status == 200 else "FAIL",
               f"stopped an open recording left by an earlier run ({stop_status})")

    name = f"v23 3-step {time.strftime('%H:%M:%S')}"
    status, started = http_call(base, headers, "POST", "/intelligence/recordings",
                                {"name": name})
    if status == 409:
        http_call(base, headers, "POST", "/intelligence/recordings/stop", {})
        status, started = http_call(base, headers, "POST", "/intelligence/recordings",
                                    {"name": name})
    if status != 200:
        record("record start", "FAIL", f"{status}: {started.get('detail')}")
        return 1

    if has_fixture:
        steps_to_record = [
            ("desktop_move_pointer", {"x": neutral[0], "y": neutral[1]},
             "move the pointer away from the button"),
            ("desktop_move_pointer", {"x": recorded_point[0], "y": recorded_point[1]},
             "move the pointer to the button"),
            ("desktop_click", {"x": recorded_point[0], "y": recorded_point[1]},
             "click the button"),
        ]
    else:
        steps_to_record = [
            ("desktop_move_pointer", {"x": target_a[0], "y": target_a[1]}, "move to A"),
            ("desktop_move_pointer", {"x": target_b[0], "y": target_b[1]}, "move to B"),
            ("desktop_move_pointer", {"x": target_c[0], "y": target_c[1]}, "move to C"),
        ]
    captured: List[Dict[str, Any]] = []
    for kind, params, label in steps_to_record:
        status, step = http_call(base, headers, "POST",
                                 "/intelligence/recordings/steps",
                                 {"kind": kind, "params": params, "label": label})
        if status != 200:
            record("record step", "FAIL", f"{kind}: {status} {step.get('detail')}")
            return 1
        captured.append(step)
    status, stopped = http_call(base, headers, "POST", "/intelligence/recordings/stop", {})
    if status != 200:
        record("record stop", "FAIL", f"{status}: {stopped.get('detail')}")
        return 1
    recorded_ok = (len(captured) == 3
                   and all(not step.get("skipped") for step in captured)
                   and [step.get("kind") for step in captured]
                   == [kind for kind, _, _ in steps_to_record])
    record("record", "PASS" if recorded_ok else "FAIL",
           f"{len(captured)} step(s): "
           + ", ".join(f"{step.get('kind')} {step.get('params')}" for step in captured))

    # -- 2. derive the draft (the recorded values, untouched) ----------------
    status, draft = http_call(base, headers, "POST", "/intelligence/recordings/draft", {})
    draft_steps = list(draft.get("steps") or [])
    if status != 200 or len(draft_steps) != 3 or draft.get("needs_input"):
        record("draft", "FAIL",
               f"{status}: {len(draft_steps)} step(s), needs_input={draft.get('needs_input')}, "
               f"{draft.get('detail')}")
        return 1
    record("draft", "PASS",
           f"{len(draft_steps)} step(s), needs_input 0, verdict={draft.get('verdict')}, "
           f"conditions={len(draft.get('conditions') or [])}")

    expected_after_baseline = fixture_state().get("clicks") if has_fixture else None
    if has_fixture:
        if not focus_fixture():
            record("baseline replay", "BLOCKED", "the fixture lost the foreground")
            return 2
    mission, error = launch_and_approve(base, headers,
                                        "replay the recorded 3-step workflow",
                                        mission_steps(draft_steps),
                                        list(draft.get("conditions") or []))
    if mission is None:
        record("baseline replay", "FAIL", error)
        return 1

    run_states = [step.get("status") for step in mission.get("steps") or []]
    # A mission with no acceptance conditions can never reach `completed`:
    # `Mission.verdict()` answers `unknown` when nothing was checkable, and the
    # runner records that as `partially_completed`. The recorder proposes no
    # condition for a desktop click or pointer move (there is no observer for
    # "the button really fired" that it trusts), so that verdict is the honest
    # one and is reported, not treated as a replay failure. What must hold is
    # that every step RAN: a refused or failed step is the failure this check
    # is for.
    baseline_state_ok = (mission.get("state") in ("completed", "partially_completed")
                         and run_states == ["done", "done", "done"])
    observed = observed_points(mission)
    moved_points_ok = (same_point(as_point(draft_steps[0]["params"]), observed.get(0))
                       and same_point(as_point(draft_steps[1]["params"]), observed.get(1)))
    record("baseline replay", "PASS" if baseline_state_ok else "FAIL",
           f"state={mission.get('state')} verdict={mission.get('verdict')} "
           f"(desktop steps carry no checkable condition, so `unknown` is expected) "
           f"steps={run_states} record_step_effect="
           f"{'readback matches each recorded point' if moved_points_ok else 'NO readback match'}")
    if not baseline_state_ok:
        for index, step in enumerate(mission.get("steps") or []):
            print(f"      step {index + 1} {step.get('tool')}: {step.get('status')} "
                  f"-- {step.get('detail')}")
        print(f"      mission reason: {mission.get('reason')}")
        return 1
    if not moved_points_ok:
        record("baseline pointer", "FAIL",
               f"the executor's readbacks {observed} do not match the recorded points "
               f"{[as_point(s['params']) for s in draft_steps[:2]]}")
        return 1

    if has_fixture:
        before_clicks = expected_after_baseline or 0
        after_clicks = fixture_state().get("clicks")
        cursor_after = read_cursor()
        click_effect = after_clicks == before_clicks + 1
        pointer_effect = near(recorded_point, cursor_after)
        record("baseline effect", "PASS" if click_effect and pointer_effect else "FAIL",
               f"fixture counter {before_clicks} -> {after_clicks}; "
               f"pointer readback {cursor_after} vs recorded "
               f"({recorded_point[0]:g}, {recorded_point[1]:g})")
        if not (click_effect and pointer_effect):
            return 1
    else:
        final_cursor = read_cursor()
        readback_ok = near(as_point(draft_steps[2]["params"]), final_cursor)
        record("baseline effect", "PASS" if readback_ok else "FAIL",
               f"pointer readback {final_cursor} vs the third step's point "
               f"{as_point(draft_steps[2]['params'])}")
        if not readback_ok:
            return 1
        record("window move", "SKIPPED",
               "no fixture is running, so there is no window to move; "
               "window-move resilience is NOT verified by this run")
        return 0

    # -- 3. move the target window under the recording -----------------------
    window = fixture_windows()[0]
    original = (int(window.get("x") or 0), int(window.get("y") or 0))
    width = int(window.get("width") or 460)
    height = int(window.get("height") or 672)
    screen_width = float(screen.get("width") or 1352)
    screen_height = float(screen.get("height") or 878)
    new_x = int(min(original[0] + 300, screen_width - width - 20))
    new_y = int(min(original[1] + 100, screen_height - height - 20))
    if (new_x, new_y) == original:
        new_x = int(max(20, original[0] - 300))

    try:
        moved_ok, move_detail = move_fixture_window(new_x, new_y)
        windows_after = fixture_windows() if moved_ok else []
        server_moved = bool(windows_after) and (
            abs(int(windows_after[0].get("x") or 0) - new_x) <= 2
            and abs(int(windows_after[0].get("y") or 0) - new_y) <= 2)
        fresh_point = wait_for_button_move(recorded_point) if server_moved else None
        record("window move", "PASS" if server_moved and fresh_point else "BLOCKED",
               f"window ({original[0]}, {original[1]}) -> "
               f"({windows_after[0].get('x') if windows_after else '?'}, "
               f"{windows_after[0].get('y') if windows_after else '?'}) "
               f"(requested {new_x}, {new_y}); button centre (AX) "
               f"({recorded_point[0]:g}, {recorded_point[1]:g}) -> "
               f"{f'({fresh_point[0]:g}, {fresh_point[1]:g})' if fresh_point else 'unchanged/unreadable'}"
               + (f"; osascript: {move_detail}" if not moved_ok else ""))
        if not (server_moved and fresh_point):
            return 2

        # The fixture caches its targets; if they did not follow the move, the
        # cached value is not an observation and must not be used as one.
        published = (fixture_state().get("targets") or {}).get("click") or {}
        cached = (published.get("x"), published.get("y"))
        cache_is_point = all(isinstance(value, (int, float)) for value in cached)
        cache_agrees = cache_is_point and near(fresh_point, (cached[0], cached[1]))
        record("target observation", "PASS",
               f"state.json target {cached} vs live AX centre "
               f"({fresh_point[0]:g}, {fresh_point[1]:g})"
               + (" -- they agree" if cache_agrees else
                  " -- the fixture's cached target did NOT follow the window move, "
                  "so live AX is the only honest source"))

        # Does the draft the recorder produces re-resolve anything? No: this is
        # the recorded values, re-derived after the window moved.
        status, stale_draft = http_call(base, headers, "POST",
                                        "/intelligence/recordings/draft", {})
        stale_steps = list(stale_draft.get("steps") or [])
        stale_kept = (len(stale_steps) == 3
                      and same_point(as_point(stale_steps[1].get("params") or {}),
                                     recorded_point)
                      and same_point(as_point(stale_steps[2].get("params") or {}),
                                     recorded_point))
        record("draft freshness",
               "PASS" if stale_kept else "FAIL",
               "the re-derived draft still carries the RECORDED points "
               f"{as_point(stale_steps[2].get('params') or {}) if stale_steps else '?'}"
               if stale_kept else
               "the draft no longer carries the recorded points; a re-resolution "
               "mechanism may exist and this check needs rewriting")
        if not stale_kept:
            return 1

        # Is the recorded point still over the target? Read-only evidence that
        # the recorded values would now miss, so a success below is attributable
        # to the fresh values and not to luck.
        x0, y0 = new_x, new_y
        in_window = (x0 <= recorded_point[0] <= x0 + width
                     and y0 <= recorded_point[1] <= y0 + height)
        record("stale coordinates", "PASS" if not in_window else "SKIPPED",
               f"the recorded click point ({recorded_point[0]:g}, {recorded_point[1]:g}) "
               f"is {'inside' if in_window else 'outside'} the moved window "
               f"({x0}, {y0}) {width}x{height}"
               + ("" if in_window else " -- a replay of the recorded values would miss"))

        # -- 4. replay with the freshly resolved coordinates -----------------
        review = [{"index": 0, "params": {"x": neutral[0], "y": neutral[1]}},
                  {"index": 1, "params": {"x": fresh_point[0], "y": fresh_point[1]}},
                  {"index": 2, "params": {"x": fresh_point[0], "y": fresh_point[1]}}]
        status, reviewed = http_call(base, headers, "POST",
                                     "/intelligence/recordings/draft",
                                     {"steps": review, "name": name})
        reviewed_steps = list(reviewed.get("steps") or [])
        fresh_ok = (status == 200 and len(reviewed_steps) == 3
                    and same_point(as_point(reviewed_steps[1].get("params") or {}),
                                   fresh_point)
                    and same_point(as_point(reviewed_steps[2].get("params") or {}),
                                   fresh_point))
        record("reviewed draft", "PASS" if fresh_ok else "FAIL",
               f"step 2 {as_point(reviewed_steps[1].get('params') or {}) if len(reviewed_steps) > 1 else '?'}, "
               f"step 3 {as_point(reviewed_steps[2].get('params') or {}) if len(reviewed_steps) > 2 else '?'}"
               if status == 200 else f"{status}: {reviewed.get('detail')}")
        if not fresh_ok:
            return 1

        clicks_before = fixture_state().get("clicks") or 0
        if not focus_fixture():
            record("moved replay", "BLOCKED", "the fixture lost the foreground")
            return 2
        mission2, error = launch_and_approve(base, headers,
                                             "replay the 3-step workflow at the fresh target",
                                             mission_steps(reviewed_steps),
                                             list(reviewed.get("conditions") or []))
        if mission2 is None:
            record("moved replay", "FAIL", error)
            return 1
        states2 = [step.get("status") for step in mission2.get("steps") or []]
        replayed_points = [as_point((step.get("params") or {})) for step in
                           (mission2.get("steps") or [])]
        mission_used_fresh = (len(replayed_points) == 3
                              and same_point(replayed_points[1], fresh_point)
                              and same_point(replayed_points[2], fresh_point)
                              and not same_point(replayed_points[2], recorded_point))
        state2_ok = (mission2.get("state") in ("completed", "partially_completed")
                     and states2 == ["done"] * 3)
        record("moved replay", "PASS" if state2_ok and mission_used_fresh else "FAIL",
               f"state={mission2.get('state')} steps={states2}; "
               f"mission step points {replayed_points[1:]} vs fresh "
               f"({fresh_point[0]:g}, {fresh_point[1]:g}) and recorded "
               f"({recorded_point[0]:g}, {recorded_point[1]:g})")
        if not (state2_ok and mission_used_fresh):
            for index, step in enumerate(mission2.get("steps") or []):
                print(f"      step {index + 1} {step.get('tool')}: {step.get('status')} "
                      f"-- {step.get('detail')}")
            return 1

        observed2 = observed_points(mission2)
        readbacks_ok = (near(fresh_point, observed2.get(1))
                        and near(fresh_point, observed2.get(2)))
        clicks_after = fixture_state().get("clicks") or 0
        cursor_final = read_cursor()
        click_effect = clicks_after == clicks_before + 1
        pointer_effect = near(fresh_point, cursor_final)
        record("moved effect", "PASS" if readbacks_ok and click_effect and pointer_effect
               else "FAIL",
               f"executor readbacks {observed2.get(1)}, {observed2.get(2)}; "
               f"fixture counter {clicks_before} -> {clicks_after}; "
               f"pointer readback {cursor_final} vs fresh "
               f"({fresh_point[0]:g}, {fresh_point[1]:g})")
        if not (readbacks_ok and click_effect and pointer_effect):
            return 1
    finally:
        restored, detail = move_fixture_window(*original)
        windows_restored = fixture_windows() if restored else []
        back = bool(windows_restored) and (
            abs(int(windows_restored[0].get("x") or 0) - original[0]) <= 2
            and abs(int(windows_restored[0].get("y") or 0) - original[1]) <= 2)
        record("window restore", "PASS" if back else "FAIL",
               f"window back to ({original[0]}, {original[1]})"
               if back else f"the window could not be restored: {detail}")

    return 0


def finish(code: int) -> int:
    """Print the tally and return the exit code, never upgrading a failure.

    Called for every exit path, including the early blocked returns, so a run
    that stopped halfway still says what it managed to check.
    """
    print("-" * 74)
    passed = sum(1 for _, status, _ in results if status == "PASS")
    failed = [name for name, status, _ in results if status == "FAIL"]
    skipped = [name for name, status, _ in results if status == "SKIPPED"]
    print(f"{passed} passed, {len(failed)} failed, {len(skipped)} skipped")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return code


if __name__ == "__main__":
    sys.exit(finish(main()))