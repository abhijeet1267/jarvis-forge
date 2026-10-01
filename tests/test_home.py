"""Home / IoT agent tests (v1.5).

The whole point of this agent is *honest reachability*. With no broker
configured the gateway is offline, and the control tools must say plainly that
no device was switched -- never fabricate a success. These tests pin that down,
plus the simulate and online paths, the routines, and the planner routing.
"""

from __future__ import annotations

import pytest

from jarvis.core.orchestrator import JarvisCore
from jarvis.core.models import Verification
from jarvis.home import (
    HomeGateway,
    TransportResult,
    default_inventory,
    get_home_gateway,
    reset_home_gateway,
)
from jarvis.tools.base import ToolContext
from jarvis.tools.home import (
    control_home_device,
    get_home_reading,
    get_home_status,
    list_home_devices,
    run_home_routine,
)

ctx = ToolContext()


@pytest.fixture(autouse=True)
def _fresh_gateway(monkeypatch: pytest.MonkeyPatch):
    """Keep tests independent: no simulate flag, one clean gateway each test."""
    monkeypatch.delenv("JARVIS_HOME_SIMULATE", raising=False)
    monkeypatch.delenv("JARVIS_HOME_MQTT_BROKER", raising=False)
    reset_home_gateway(None)
    yield
    reset_home_gateway(None)


class _FakeTransport:
    def __init__(self, delivered: bool = True, verified: bool = True, detail: str = "ok"):
        self.delivered = delivered
        self.verified = verified
        self.detail = detail
        self.sent = []

    def send(self, topic, payload):
        self.sent.append((topic, dict(payload)))
        return TransportResult(self.delivered, self.verified, self.detail)


# ---------------------------------------------------------------------------
# Gateway (domain) behaviour
# ---------------------------------------------------------------------------

def test_gateway_offline_by_default() -> None:
    gw = get_home_gateway()
    assert gw.online is False
    assert gw.mode == "offline"
    assert gw.simulate is False
    assert gw.devices, "default inventory should not be empty"


def test_resolve_devices() -> None:
    gw = get_home_gateway()
    assert gw.resolve("desk lamp").device_id == "study.desk_lamp"
    assert gw.resolve("the desk light").device_id == "study.desk_lamp"
    assert gw.resolve("living room ceiling light").device_id == "living.ceiling"
    assert gw.resolve("hallway door").device_id == "hallway.door"
    assert gw.resolve("ac").device_id == "study.ac"
    assert gw.resolve("kitchen light").device_id == "kitchen.light"
    assert gw.resolve("a nonexistent gadget") is None


def test_control_offline_does_not_change_state() -> None:
    gw = get_home_gateway()
    device = gw.resolve("kitchen.light")
    assert device is not None and device.on is False
    outcome = gw.control(device, on=True)
    assert outcome.offline is True
    assert outcome.delivered is False
    assert device.on is False, "offline control must not mutate state"


def test_control_simulate_updates_state() -> None:
    gw = HomeGateway(simulate=True)
    device = gw.resolve("kitchen.light")
    assert device.on is False
    outcome = gw.control(device, on=True)
    assert outcome.offline is False
    assert outcome.delivered is True
    assert outcome.simulated is True
    assert device.on is True
    # dimming records a level
    light = gw.resolve("living.ceiling")
    gw.control(light, on=True, level=0.5)
    assert light.on is True and light.level == 0.5


def test_control_online_verified_and_unverified() -> None:
    transport = _FakeTransport(delivered=True, verified=True)
    gw = HomeGateway(online=True, transport=transport)
    outcome = gw.control(gw.resolve("kitchen.light"), on=True)
    assert outcome.delivered is True and outcome.verified is True

    flaky = _FakeTransport(delivered=True, verified=False, detail="published; echo not confirmed")
    gw2 = HomeGateway(online=True, transport=flaky)
    outcome2 = gw2.control(gw2.resolve("kitchen.light"), on=True)
    assert outcome2.delivered is True and outcome2.verified is False

    refused = _FakeTransport(delivered=False, verified=False, detail="broker rejected")
    gw3 = HomeGateway(online=True, transport=refused)
    outcome3 = gw3.control(gw3.resolve("kitchen.light"), on=True)
    assert outcome3.delivered is False and outcome3.verified is False


def test_run_routine_offline_reports_not_applied() -> None:
    gw = get_home_gateway()
    result = gw.run_routine("sleep")
    assert result["ok"] is True
    assert result["offline"] is True
    assert result["applied"] == 0
    assert result["total"] > 0


def test_run_routine_simulate_applies_everything() -> None:
    gw = HomeGateway(simulate=True)
    result = gw.run_routine("sleep")
    assert result["offline"] is False
    assert result["simulated"] is True
    assert result["applied"] == result["total"]
    # lights off, camera + alarm on after the sleep routine
    assert gw.resolve("kitchen.light").on is False
    assert gw.resolve("garden.camera").on is True
    assert gw.resolve("bedroom.alarm").on is True


def test_run_routine_leaving_keeps_fridge_on() -> None:
    gw = HomeGateway(simulate=True)
    gw.run_routine("leaving")
    # everything off ...
    assert gw.resolve("kitchen.light").on is False
    assert gw.resolve("living.media").on is False
    # ... except the fridge, which must stay powered
    assert gw.resolve("kitchen.fridge").on is True


def test_run_routine_unknown() -> None:
    gw = get_home_gateway()
    result = gw.run_routine("karaoke")
    assert result["ok"] is False
    assert "study" in result["available"]


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

async def test_get_home_status_offline() -> None:
    result = await get_home_status(ctx)
    assert result.ok is True
    assert result.output["online"] is False
    assert result.output["mode"] == "offline"
    assert "offline" in result.message.lower()
    assert result.output["device_count"] == len(default_inventory())


async def test_list_home_devices_filters() -> None:
    all_devices = await list_home_devices(ctx)
    assert all_devices.ok is True
    assert all_devices.output["devices"], "should list the default inventory"

    study = await list_home_devices(ctx, room="study")
    assert all(d["room"] == "study" for d in study.output["devices"])

    lights = await list_home_devices(ctx, kind="light")
    assert all(d["kind"] == "light" for d in lights.output["devices"])


async def test_get_home_reading_all_and_one() -> None:
    all_readings = await get_home_reading(ctx)
    assert all_readings.ok is True
    assert any(r["unit"] == "\u00b0C" for r in all_readings.output["readings"])
    assert all_readings.output["source"] == "last known state"

    study_temp = await get_home_reading(ctx, room="study")
    assert all(r["room"] == "study" for r in study_temp.output["readings"])

    one = await get_home_reading(ctx, device="study temperature sensor")
    assert one.ok is True
    assert one.output["reading"]["reading"] == 23.0

    door = await get_home_reading(ctx, device="front door sensor")
    assert door.ok is True and "closed" in door.message


async def test_get_home_reading_rejects_non_sensor() -> None:
    result = await get_home_reading(ctx, device="kitchen light")
    assert result.ok is False
    assert "not a sensor" in result.error


async def test_get_home_reading_unknown() -> None:
    result = await get_home_reading(ctx, device="weather vane")
    assert result.ok is False
    assert result.output.get("candidates")


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------

async def test_control_offline_is_honest() -> None:
    result = await control_home_device(ctx, device="kitchen light", action="on")
    assert result.ok is True  # the *call* succeeded; the *action* did not happen
    assert result.output["delivered"] is False
    assert result.output["offline"] is True
    assert "no device was switched" in result.message.lower()


async def test_control_simulate_reports_simulated() -> None:
    reset_home_gateway(HomeGateway(simulate=True))
    result = await control_home_device(ctx, device="kitchen light", action="on")
    assert result.output["delivered"] is True
    # Read the ActionResult field, not result.output["verified"]: the flag moved
    # out of the output dict when `ok()` stopped hardcoding it (Phase 9 P1-9 --
    # this assertion had been aborting on KeyError and therefore testing nothing).
    assert result.verified is False
    assert result.output["simulated"] is True
    assert "simulated" in result.message.lower()
    # Phase 10 §12 left this OPEN; Phase 18 closed it. A simulated actuation used
    # to record Verification.SUCCESS -- "simulated" in the words, an established
    # effect in the record -- and now records UNKNOWN with `simulated` as its audit
    # token, so the ledger says what the reply says.
    assert result.verification is Verification.UNKNOWN
    assert JarvisCore._audit_token(result) == "simulated"

    dimmed = await control_home_device(ctx, device="living ceiling", action="set", value=40)
    assert dimmed.ok is True
    assert dimmed.output["state"]["level"] == 0.4


async def test_control_online_unverified_not_claimed_done() -> None:
    reset_home_gateway(HomeGateway(online=True, transport=_FakeTransport(verified=False)))
    result = await control_home_device(ctx, device="kitchen light", action="on")
    assert result.output["delivered"] is True
    # `verified` is an ActionResult field now, not an output key (Phase 9 P1-9).
    assert result.verified is False
    assert result.verification is not Verification.SUCCESS
    assert "not yet confirmed" in result.message.lower()


async def test_control_requires_a_device() -> None:
    result = await control_home_device(ctx, action="on")
    assert result.ok is False


async def test_control_unknown_device() -> None:
    result = await control_home_device(ctx, device="chandelier in the attic", action="on")
    assert result.ok is False
    assert result.output.get("candidates")


async def test_control_sensor_is_not_controllable() -> None:
    result = await control_home_device(ctx, device="front door sensor", action="on")
    assert result.ok is False
    assert "sensor" in result.error.lower()


async def test_run_routine_offline_message() -> None:
    result = await run_home_routine(ctx, routine="movie")
    assert result.ok is True
    assert result.output["offline"] is True
    assert result.output["applied"] == 0
    assert "offline" in result.message.lower()


async def test_run_routine_simulate_message() -> None:
    reset_home_gateway(HomeGateway(simulate=True))
    result = await run_home_routine(ctx, routine="study")
    assert result.output["offline"] is False
    assert result.output["simulated"] is True
    assert result.output["applied"] == result.output["total"]


async def test_run_routine_requires_name() -> None:
    result = await run_home_routine(ctx)
    assert result.ok is False
    assert "sleep" in result.output["available"]


# ---------------------------------------------------------------------------
# Planner routing
# ---------------------------------------------------------------------------

def test_planner_routes_control(core: JarvisCore) -> None:
    plan = core.planner.parse("turn on the desk lamp")
    assert plan.actions and plan.actions[0].tool == "control_home_device"
    assert plan.actions[0].params["action"] == "on"
    assert plan.actions[0].params["value"] is None

    dimmed = core.planner.parse("dim the desk lamp to 50%")
    assert dimmed.actions[0].tool == "control_home_device"
    assert dimmed.actions[0].params["action"] == "set"
    assert dimmed.actions[0].params["value"] == 50


def test_planner_routes_routines(core: JarvisCore) -> None:
    for text, routine in [
        ("study mode", "study"),
        ("run sleep mode", "sleep"),
        ("leaving home", "leaving"),
        ("emergency mode", "emergency"),
    ]:
        plan = core.planner.parse(text)
        assert plan.actions, f"no action planned for {text!r}"
        assert plan.actions[0].tool == "run_home_routine"
        assert plan.actions[0].params == {"routine": routine}


def test_planner_routes_status_and_reading(core: JarvisCore) -> None:
    status = core.planner.parse("is anyone home")
    assert status.actions[0].tool == "get_home_status"

    temp = core.planner.parse("what is the temperature in the study")
    assert temp.actions[0].tool == "get_home_reading"
    assert temp.actions[0].params["room"] == "study"


def test_planner_does_not_steal_unrelated_turn_on(core: JarvisCore) -> None:
    # "turn on the music" is media, not a home device
    plan = core.planner.parse("turn on the music")
    for action in plan.actions:
        assert action.tool != "control_home_device"


# ---------------------------------------------------------------------------
# End-to-end through the full loop
# ---------------------------------------------------------------------------

async def test_e2e_status_when_offline(core: JarvisCore, make_request) -> None:
    response = await core.handle(make_request("is anyone home"))
    assert response.intent == "home_status"
    assert response.actions[0].ok
    assert "offline" in response.reply.lower()


async def test_e2e_control_when_offline_does_not_claim_success(core: JarvisCore, make_request) -> None:
    """An offline gateway must not be reported as a switch that happened.

    Phase 31 put a confirmation in front of every home actuation (22-G), so the
    sentence now stops at the gate: the first assertions are that nothing ran until
    someone approved it, and the claim this test was written for -- no success claim
    when nothing was observed -- is made after the approval, on the way it always
    mattered.
    """
    response = await core.handle(make_request("turn on the kitchen light"))
    assert response.intent == "home_control"
    assert not response.actions, "a physical actuation ran before anyone approved it"
    assert response.needs_confirmation, response.reply

    approved = await core.resolve_confirmation(response.confirmation_id, True)
    assert approved.actions[0].ok
    assert "no device was switched" in approved.reply.lower(), approved.reply
