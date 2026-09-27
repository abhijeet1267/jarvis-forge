"""Phase 18: a simulated actuation is not a verified world change.

Measured at the start of the phase, with the simulators on:

    home control (simulated)     ok=True ver=success  sim=True   LEDGER=success
    home routine (simulated)     ok=True ver=success  sim=True   LEDGER=success
    experiment (simulated)       ok=True ver=success  sim=True   LEDGER=success
    robot motion (simulated)     <same shape>

Each of those replies says, in its own text, "no real device was reached" / "no
real robot was reached", and `output.simulated` is true -- and the hash-chained
ledger wrote `success`. Since Phase 13 the dashboard has been masking this by
reading `output.simulated` and showing SIMULATED, which means the only honest copy
of the fact lived in the UI and the tamper-evident record was wrong. Phase 13, 14
and 15 each carried it forward; this is where it closes.

Deliberately NOT changed: the simulated *car read*. Reading a labelled simulator
is a real read of that simulator, the reply says "(simulated)" and the value is
never presented as a vehicle's. A read and an actuation are different claims, so
they get different rules -- pinned below so the difference stays a decision rather
than an accident.
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
from jarvis.core.models import CommandRequest, Verification
from jarvis.core.orchestrator import JarvisCore
from jarvis.home import HomeGateway, reset_home_gateway
from jarvis.robotics.gateway import RobotGateway, reset_robot_gateway
from jarvis.tools.base import ToolContext

PAGE = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"
node = shutil.which("node")

SIMULATED_ACTUATIONS = [
    ("control_home_device", {"device": "kitchen light", "action": "on"}, "home"),
    ("run_home_routine", {"routine": "study"}, "home"),
    ("run_experiment", {"experiment": "pick_place"}, "robot"),
]


def _ctx(core) -> ToolContext:
    return ToolContext(settings=core.settings, apps=core.apps, devices=core.devices,
                       memory=core.memory, graph=core.graph, core=core)


@pytest.fixture()
def simulators():
    """Both gateways in simulate mode, put back exactly as found."""
    reset_home_gateway(HomeGateway(simulate=True))
    reset_robot_gateway(RobotGateway(simulate=True))
    try:
        yield
    finally:
        reset_home_gateway(None)
        reset_robot_gateway(None)


@pytest.fixture(scope="module")
def state_of(tmp_path_factory):
    if not node:
        pytest.skip("node not available to run the page's own code")
    body = re.search(r"<script>(.*?)</script>", PAGE.read_text(), re.S).group(1)
    extracted = body[body.index("const STATE_TONE"):body.index("function renderState")]
    extracted = extracted.replace("const STATE_TONE", "var STATE_TONE", 1)
    root = tmp_path_factory.mktemp("p18")
    lib = root / "page.js"

    def _run(payload):
        lib.write_text("const s = " + json.dumps(extracted) + ";\neval(s);\n"
                       "console.log(pipelineState(JSON.parse(process.argv[2])));\n")
        proc = subprocess.run([node, str(lib), json.dumps(payload, default=str)],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr[:300]
        return proc.stdout.strip()

    return _run


# ---------------------------------------------------------------------------
# 1. The four simulated actuations stop claiming a verified world change.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool, kwargs, gateway", SIMULATED_ACTUATIONS)
async def test_simulated_actuation_is_not_verified(core, simulators, tool, kwargs, gateway):
    result = await core.tools.handler(tool)(_ctx(core), **kwargs)

    assert result.ok is True, "the handler did complete -- that part stays true"
    assert result.output.get("simulated") is True, tool
    assert result.verification is not Verification.SUCCESS, (
        f"{tool} wrote {result.verification.value} to the ledger for a change that "
        "its own reply says never reached a real device")


async def test_simulated_robot_motion_is_not_verified(core, simulators):
    from jarvis.robotics.gateway import get_robot_gateway

    name = get_robot_gateway().robots[0].name
    result = await core.tools.handler("command_robot_motion")(
        _ctx(core), robot=name, action="home")

    assert result.output.get("simulated") is True, result.error or result.message
    assert result.verification is not Verification.SUCCESS


# ---------------------------------------------------------------------------
# 2. The ledger gets its own honest token, not success and not a borrowed failure.
# ---------------------------------------------------------------------------


def test_audit_token_for_simulated_is_its_own_state(core, simulators):
    result = asyncio.run(core.tools.handler("control_home_device")(
        _ctx(core), device="kitchen light", action="on"))

    token = JarvisCore._audit_token(result)
    assert token == "simulated", token
    assert token not in ("success", "failure", "unverified")


async def test_pipeline_writes_simulated_to_the_ledger(core, simulators):
    before = core.db.query("SELECT COUNT(*) AS n FROM action_logs")[0]["n"]
    response = await core.handle(CommandRequest(
        text="Jarvis, turn on the study desk lamp", source="test"))
    if response.needs_confirmation:
        response = await core.resolve_confirmation(
            response.confirmation_id, True, session_id=response.session_id)

    rows = core.db.query(
        "SELECT result FROM action_logs ORDER BY rowid DESC LIMIT 1")
    assert core.db.query("SELECT COUNT(*) AS n FROM action_logs")[0]["n"] > before
    assert rows[0]["result"] == "simulated", rows[0]["result"]


# ---------------------------------------------------------------------------
# 3. The job step and the scheduler follow the same rule.
# ---------------------------------------------------------------------------


def test_simulated_step_is_unverified_not_succeeded(core, simulators):
    result = asyncio.run(core.tools.handler("run_experiment")(
        _ctx(core), experiment="map_lab"))

    status = JarvisCore._action_status(result)
    assert status.value != "succeeded", status.value
    assert status.value == "unverified"


async def test_scheduled_simulated_actuation_is_not_logged_as_executed(core, simulators):
    """Unattended runs get the same rule as interactive ones.

    The runner splits `executed` / `unverified` / `failed` on verification (Phase 12),
    so with a simulated actuation now reporting UNKNOWN it records `unverified`.
    Asserted against what the code actually did: this test was first written to
    expect the job to *park* at awaiting_approval, on an assumption about
    confirmation gating, and the measurement showed it executes -- so the real
    property, and the one that matters, is the stored outcome.
    """
    from jarvis.scheduler.runner import SchedulerRunner
    from jarvis.scheduler.schedule import Recurrence
    from jarvis.scheduler.store import ScheduleStore, ScheduledJob

    job = ScheduledJob(
        schedule_id="SCHED-P18SIM", name="p18 simulated", action="control_home_device",
        recurrence=Recurrence(kind="daily"),
        params={"device": "kitchen light", "action": "on"}, max_risk=4)
    outcome = await SchedulerRunner(ScheduleStore()).run_job(core, job)

    assert "simulated" in outcome["detail"], outcome["detail"]
    assert outcome["outcome"] == "unverified", outcome
    assert outcome["outcome"] != "executed", (
        "a scheduled job that touched no real device was recorded as executed")


# ---------------------------------------------------------------------------
# 4. The dashboard must keep saying SIMULATED now that it can no longer lean on
#    verification=success to get there.
# ---------------------------------------------------------------------------


async def test_dashboard_still_reports_simulated(core, simulators, state_of):
    result = await core.tools.handler("control_home_device")(
        _ctx(core), device="kitchen light", action="on")
    payload = {"actions": [json.loads(result.model_dump_json())]}

    assert payload["actions"][0]["verification"] != "success"
    assert state_of(payload) == "SIMULATED"


async def test_a_real_failure_still_outranks_a_simulated_one(core, simulators, state_of):
    """SIMULATED must not become a new way to hide a failure."""
    simulated = await core.tools.handler("control_home_device")(
        _ctx(core), device="kitchen light", action="on")
    failed = await core.tools.handler("delete_file")(
        _ctx(core), path=str(Path(os.getcwd()) / "p18-no-such-file.bin"))
    payload = {"actions": [json.loads(simulated.model_dump_json()),
                           json.loads(failed.model_dump_json())]}

    assert any(a["verification"] == "failed" for a in payload["actions"])
    assert state_of(payload) == "FAILED"


def test_audit_card_gives_simulated_its_own_tone():
    body = PAGE.read_text()
    assert re.search(r"simulated:\s*\"warn\"", body), (
        "the card has no tone for the new token; it would fall through to the "
        "default and a simulation would read like a failure")
    assert '"simulated"' in body or "simulated:" in body


# ---------------------------------------------------------------------------
# 5. The read-vs-actuation line, kept explicit.
# ---------------------------------------------------------------------------


async def test_a_simulated_car_read_is_still_a_successful_read(core):
    """Different claim, different rule -- and the reason is in the output.

    The vehicle simulator's values are genuinely returned, labelled, and the tool
    says "This is a simulated vehicle, not a real car." Relabelling that as
    unverified would claim the read did not happen, which is false. An actuation is
    the opposite: nothing in the world changed.
    """
    from jarvis.car.gateway import CarGateway, reset_car_gateway
    from jarvis.car.readers import SimulatedReader
    from jarvis.tools.car import get_car_telemetry

    reset_car_gateway(CarGateway(SimulatedReader()))
    try:
        result = await get_car_telemetry(_ctx(core), metric="fuel")
    finally:
        # Session-scoped core and a process-wide gateway: unrestored, this leaks a
        # simulated car into every later module.
        reset_car_gateway(None)

    assert result.verification is Verification.SUCCESS
    assert "simulated" in result.message.lower()
    assert JarvisCore._audit_token(result) == "success"
