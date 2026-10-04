"""Watch companion tests (v1.8).

The watch is the first agent whose whole job is to *ask a human*, so these tests
guard two failure modes that would each defeat the point:

1. **Overstating delivery.** An item is queued, then handed to a companion, then
   displayed, then answered -- and a reply from JARVIS may only claim the stage
   it actually reached. Every test below asserts the negative too: nothing says
   "sent" while it is still sitting in the outbox.
2. **Self-approval.** No tool may resolve a confirmation. The only path that can
   approve something is the companion's inbound POST carrying the wearer's
   button press -- which these tests exercise end to end, file copied on disk
   because a wrist said so, and not copied because a wrist said no.

The bridge transport is exercised both with a canned opener and against a real
localhost HTTP server, so the wire behaviour is not mocked into passing.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from jarvis.api.app import app
from jarvis.core.models import PlannedAction, PolicyDecision
from jarvis.core.orchestrator import JarvisCore
from jarvis.core.planner import RulePlanner
from jarvis.registry.devices import get_device_registry
from jarvis.tools import build_default_registry
from jarvis.tools.base import ToolContext
from jarvis.tools.watch import (
    ask_watch_confirmation,
    get_watch_outbox,
    get_watch_status,
    run_watch_routine,
    send_watch_notification,
    vibrate_watch,
)
from jarvis.watch.gateway import (
    DEVICE_ID,
    BridgeTransport,
    QueueTransport,
    SimulatedTransport,
    WatchGateway,
    get_watch_gateway,
    reset_watch_gateway,
)

TEST_TOKEN = os.environ["JARVIS_API_TOKEN"]
ctx = ToolContext()


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch):
    """No leaked env flags, no leaked outbox, no leaked pairing."""
    for var in ("JARVIS_WATCH_SIMULATE", "JARVIS_WATCH_BRIDGE", "JARVIS_WATCH_BRIDGE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    get_device_registry().set_auth_state(DEVICE_ID, "unpaired")
    reset_watch_gateway()
    yield
    reset_watch_gateway()
    get_device_registry().set_auth_state(DEVICE_ID, "unpaired")


@pytest.fixture()
def paired() -> WatchGateway:
    """A paired watch, with the default (queue-only) transport."""
    get_device_registry().set_auth_state(DEVICE_ID, "paired", fingerprint="AAAA BBBB CCCC DDDD")
    gw = WatchGateway()
    reset_watch_gateway(gw)
    return gw


@pytest.fixture()
def simulated() -> WatchGateway:
    """A paired watch with an auto-acknowledging companion."""
    get_device_registry().set_auth_state(DEVICE_ID, "paired")
    gw = WatchGateway(simulate=True)
    reset_watch_gateway(gw)
    return gw


def _expire_pending_confirmations() -> None:
    """Clear confirmations left by other modules' tests.

    The database is shared for the session, and "ask my watch to confirm" with no
    argument deliberately offers whatever is pending -- so the empty-case test has
    to make the absence real rather than assume it.
    """
    from jarvis.db.database import get_db

    get_db().execute(
        "UPDATE confirmations SET state = ? WHERE state = ?", ("expired", "pending")
    )


class _FakeOpener:
    """Stands in for urllib.request.urlopen with a canned answer."""

    def __init__(self, body: bytes = b'{"ok": true}', status: int = 200, error: Exception | None = None):
        self.body = body
        self.status = status
        self.error = error
        self.sent: list[dict] = []

    def __call__(self, request, timeout: float = 0):  # noqa: ARG002
        import urllib.error

        if self.error is not None:
            raise self.error
        opener = self

        class _Response:
            status = opener.status

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return opener.body

        return _Response()


# ---------------------------------------------------------------------------
# A real HTTP bridge, so the push path is not only mocked
# ---------------------------------------------------------------------------

class _BridgeHandler(BaseHTTPRequestHandler):
    received: list[dict] = []
    confirm = True

    def do_POST(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).received.append(payload)
        body = b'{"ok": true}' if type(self).confirm else b'{"queued": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence the test server
        return


@pytest.fixture()
def bridge_server():
    _BridgeHandler.received = []
    _BridgeHandler.confirm = True
    server = HTTPServer(("127.0.0.1", 0), _BridgeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Pairing gates everything
# ---------------------------------------------------------------------------

async def test_unpaired_watch_is_refused_without_queueing() -> None:
    gw = get_watch_gateway()
    result = await send_watch_notification(ctx, title="build finished")
    assert result.output["paired"] is False
    assert result.output["delivered"] is False
    assert "not paired" in result.message.lower()
    assert gw.items == [], "an unaddressable watch must not accumulate a queue"


async def test_status_reports_the_real_pairing_state() -> None:
    result = await get_watch_status(ctx)
    assert result.output["auth_state"] == "unpaired"
    assert "not paired" in result.message.lower()

    get_device_registry().set_auth_state(DEVICE_ID, "pending")
    pending = await get_watch_status(ctx)
    assert pending.output["auth_state"] == "pending"
    assert "not paired" in pending.message.lower()


def test_capabilities_come_from_the_device_registry(paired: WatchGateway) -> None:
    device = paired.device()
    assert device["paired"] is True
    assert "notifications" in device["capabilities"]
    assert "confirmation" in device["capabilities"]


# ---------------------------------------------------------------------------
# Delivery staging: the central guarantee
# ---------------------------------------------------------------------------

async def test_queue_mode_never_claims_delivery(paired: WatchGateway) -> None:
    result = await send_watch_notification(ctx, title="build finished", body="42 tests")
    assert result.output["queued"] is True
    assert result.output["delivered"] is False
    assert result.output["state"] == "queued"
    assert "queued" in result.message
    assert "nothing has reached your watch" in result.message.lower()
    assert paired.items[0].outcome_word == "queued on the laptop, not delivered"


def test_stages_advance_only_when_said_to(paired: WatchGateway) -> None:
    item = paired.queue("notification", "build finished")
    assert item.state == "queued"

    taken = paired.take()
    assert [i.item_id for i in taken] == [item.item_id]
    assert item.state == "delivered"
    assert item.outcome_word == "handed to the watch, not yet shown"

    paired.ack(item.item_id, displayed=True)
    assert item.state == "displayed"
    assert item.outcome_word == "shown on the watch"


def test_take_only_returns_queued_items(paired: WatchGateway) -> None:
    paired.queue("notification", "first")
    assert len(paired.take()) == 1
    assert paired.take() == [], "a delivered item must not be handed over twice"


async def test_vibration_wording_stays_conditional(paired: WatchGateway) -> None:
    queued = await vibrate_watch(ctx, pattern="short")
    assert "nothing buzzed" in queued.message.lower()
    assert queued.output["delivered"] is False

    simulated = WatchGateway(simulate=True)
    reset_watch_gateway(simulated)
    delivered = await vibrate_watch(ctx, pattern="long")
    assert delivered.output["delivered"] is True
    assert "the watch was told to buzz" in delivered.message


async def test_unknown_vibration_pattern_is_refused(paired: WatchGateway) -> None:
    result = await vibrate_watch(ctx, pattern="cannon")
    assert result.ok is False
    assert "short" in result.output["patterns"]


async def test_notification_needs_text(paired: WatchGateway) -> None:
    result = await send_watch_notification(ctx, title="   ")
    assert result.ok is False


# ---------------------------------------------------------------------------
# Simulated companion: delivers, never answers
# ---------------------------------------------------------------------------

async def test_simulate_delivers_but_cannot_answer(simulated: WatchGateway) -> None:
    result = await ask_watch_confirmation(ctx, question="start the render?")
    assert result.output["delivered"] is True
    assert result.output["state"] == "displayed"
    assert simulated.status()["mode"] == "simulate"

    # The simulated wearer never presses a button: an auto-answer here would be
    # the exact "runner approves on the user's behalf" failure the project bans.
    assert simulated.unanswered(), "a delivered prompt must still await the wearer"
    assert simulated.counts()["replied"] == 0


def test_simulate_status_says_so(simulated: WatchGateway) -> None:
    status = simulated.status()
    assert status["mode"] == "simulate"
    assert "simulated companion" in simulated.summary_line()


# ---------------------------------------------------------------------------
# Bridge transport
# ---------------------------------------------------------------------------

def test_bridge_confirmed_delivery() -> None:
    opener = _FakeOpener(body=b'{"ok": true}')
    gw = WatchGateway(transport=BridgeTransport("http://127.0.0.1:9/push", opener=opener))
    item = gw.queue("notification", "deploy done")
    assert item.state == "displayed"
    assert item.delivered_via == "bridge"


def test_bridge_accepted_but_unconfirmed() -> None:
    """HTTP 200 without {"ok": true} is their claim of receipt, not a confirmation."""
    opener = _FakeOpener(body=b'{"queued": true}')
    gw = WatchGateway(transport=BridgeTransport("http://127.0.0.1:9/push", opener=opener))
    item = gw.queue("notification", "deploy done")
    assert item.state == "delivered"
    assert "without confirming" in item.reply_note


def test_bridge_failure_leaves_the_item_queued() -> None:
    import urllib.error

    opener = _FakeOpener(error=urllib.error.URLError("refused"))
    gw = WatchGateway(transport=BridgeTransport("http://127.0.0.1:9/push", opener=opener))
    item = gw.queue("notification", "deploy done")
    assert item.state == "queued"
    assert "did not answer" in gw.bridge_error


def test_bridge_rejects_a_redirectish_status() -> None:
    opener = _FakeOpener(body=b"{}", status=302)
    gw = WatchGateway(transport=BridgeTransport("http://127.0.0.1:9/push", opener=opener))
    assert gw.queue("notification", "x").state == "queued"


def test_bridge_pushes_a_real_http_payload(bridge_server: HTTPServer) -> None:
    url = f"http://127.0.0.1:{bridge_server.server_port}/push"
    gw = WatchGateway(bridge_url=url)
    item = gw.queue("confirmation", "run study mode?", confirmation_id="CONF-1", routine="study")
    assert item.state == "displayed"
    assert _BridgeHandler.received[0]["confirmation_id"] == "CONF-1"
    assert _BridgeHandler.received[0]["choices"] == ["CONFIRM", "CANCEL"]


def test_bridge_without_confirmation_stays_delivered(bridge_server: HTTPServer) -> None:
    _BridgeHandler.confirm = False
    gw = WatchGateway(bridge_url=f"http://127.0.0.1:{bridge_server.server_port}/push")
    assert gw.queue("notification", "x").state == "delivered"


def test_transport_mode_defaults() -> None:
    gw = WatchGateway()
    assert gw.mode == "queue"
    assert isinstance(gw.transport, QueueTransport)
    assert WatchGateway(simulate=True).mode == "simulate"
    assert WatchGateway(bridge_url="http://x/y").mode == "bridge"


def test_queue_transport_reports_why() -> None:
    result = QueueTransport().push({"item_id": "x"})
    assert result.delivered is False
    assert "no watch bridge" in result.detail
    assert SimulatedTransport().push({}).confirmed is True


# ---------------------------------------------------------------------------
# Answering: the wearer's side, with its guards
# ---------------------------------------------------------------------------

def test_reply_guards(paired: WatchGateway) -> None:
    item, error = paired.reply("WATCH-nope", "CONFIRM")
    assert item is None and "no watch prompt" in error

    buzz = paired.queue("vibration", "Vibrate: short", pattern="short")
    assert paired.reply(buzz.item_id, "CONFIRM")[1] == "That watch item has nothing to answer."

    prompt = paired.queue("confirmation", "delete the folder?", choices=("CONFIRM", "CANCEL"))
    # A prompt still in the outbox cannot be answered: the wearer has not seen it.
    early, error = paired.reply(prompt.item_id, "CONFIRM")
    assert "has not reached your watch" in error
    assert prompt.state == "queued" and prompt.reply == ""

    paired.take()                                   # the companion calls in and shows it
    unknown, error = paired.reply(prompt.item_id, "MAYBE")
    assert "CONFIRM or CANCEL" in error
    assert unknown.state != "replied"

    paired.reply(prompt.item_id, "cancel")          # case-insensitive
    assert prompt.reply == "CANCEL" and prompt.state == "replied"
    again, error = paired.reply(prompt.item_id, "CONFIRM")
    assert "Already answered" in error


def test_reply_by_confirmation_id_too(paired: WatchGateway) -> None:
    paired.queue("confirmation", "copy the file", confirmation_id="CONF-42")
    paired.take()
    item, error = paired.reply("CONF-42", "CONFIRM")
    assert not error and item.reply == "CONFIRM"


def test_a_queued_prompt_cannot_be_answered_from_the_laptop(paired: WatchGateway) -> None:
    """The strict rule that keeps a wrist tap a wrist tap."""
    prompt = paired.queue("confirmation", "unlock the door?")
    item, error = paired.reply(prompt.item_id, "CONFIRM")
    assert "not reached your watch" in error
    assert paired.counts()["replied"] == 0


def test_ack_can_report_it_was_never_shown(paired: WatchGateway) -> None:
    """Not shown is not the same as declined."""
    prompt = paired.queue("confirmation", "run the backup?")
    paired.take()
    paired.ack(prompt.item_id, displayed=False)
    assert prompt.state == "delivered"
    assert prompt.reply == "", "an unshown prompt must not record an answer"
    assert "not shown" in prompt.reply_note

    notification = paired.queue("notification", "fyi")
    paired.take()
    paired.reply(notification.item_id, "DISMISSED")
    assert notification.state == "replied" and notification.reply == "DISMISSED"


def test_battery_is_only_what_the_watch_said(paired: WatchGateway) -> None:
    assert paired.status()["battery"] is None
    assert "never reported" in paired.status()["battery_reported_by"]

    paired.note_device_report(battery=81.5, charging=False)
    assert paired.status()["battery"] == 81.5

    paired.note_device_report(battery=4000)           # implausible: ignored
    assert paired.status()["battery"] == 81.5


def test_status_summary_when_paired_and_idle(paired: WatchGateway) -> None:
    line = paired.summary_line()
    assert "unpaired" not in line
    assert "prompts wait in the outbox" in line


# ---------------------------------------------------------------------------
# Quick routines
# ---------------------------------------------------------------------------

async def test_routine_asks_and_does_not_run(paired: WatchGateway) -> None:
    result = await run_watch_routine(ctx, routine="study")
    assert result.ok is True
    assert result.output["started"] is False
    assert result.output["command"] == "run study mode"
    assert "nothing has started yet" in result.message.lower()


async def test_emergency_routine_is_high_priority(paired: WatchGateway) -> None:
    result = await run_watch_routine(ctx, routine="emergency")
    item = paired.items[0]
    assert item.priority == "high"
    assert item.pattern == "sos"
    assert "stop all physical actions" in result.output["command"]


async def test_unknown_routine_lists_the_real_ones(paired: WatchGateway) -> None:
    result = await run_watch_routine(ctx, routine="launch_missiles")
    assert result.ok is False
    assert "study" in result.output["available"]


async def test_routine_needs_a_paired_watch() -> None:
    result = await run_watch_routine(ctx, routine="study")
    assert result.output["paired"] is False
    assert result.output["queued"] is False


# ---------------------------------------------------------------------------
# Outbox view
# ---------------------------------------------------------------------------

async def test_outbox_lists_progress(paired: WatchGateway) -> None:
    await send_watch_notification(ctx, title="one")
    await send_watch_notification(ctx, title="two")
    paired.take(limit=1)

    listing = await get_watch_outbox(ctx)
    assert listing.output["counts"]["queued"] == 1
    assert listing.output["counts"]["delivered"] == 1

    queued_only = await get_watch_outbox(ctx, state="queued")
    assert len(queued_only.output["items"]) == 1

    bad = await get_watch_outbox(ctx, state="shiny")
    assert bad.ok is False


async def test_outbox_when_empty(paired: WatchGateway) -> None:
    result = await get_watch_outbox(ctx)
    assert "nothing" in result.message.lower()


# ---------------------------------------------------------------------------
# ask_watch_confirmation: displays, never decides
# ---------------------------------------------------------------------------

async def test_confirmation_prompt_needs_something_to_ask(paired: WatchGateway) -> None:
    _expire_pending_confirmations()
    result = await ask_watch_confirmation(ctx)
    assert result.ok is True
    assert "nothing is waiting for confirmation" in result.message.lower()
    assert paired.items == []


async def test_the_newest_pending_confirmation_is_offered_unasked(
    paired: WatchGateway, core: JarvisCore
) -> None:
    """"ask my watch to confirm" with no argument means *the thing that is waiting*."""
    action = PlannedAction(tool="delete_file", params={"path": "/tmp/jarvis-watch-ask.txt"},
                           device="laptop_main")
    verdict = core.policy.evaluate(action)
    assert verdict.decision is PolicyDecision.CONFIRM

    result = await ask_watch_confirmation(ctx)
    assert result.output["queued"] is True
    queued = [i for i in paired.items if i.kind == "confirmation"]
    assert verdict.confirmation_id in [i.confirmation_id for i in queued]


async def test_unknown_confirmation_id_is_refused(paired: WatchGateway) -> None:
    result = await ask_watch_confirmation(ctx, confirmation_id="CONF-nope")
    assert result.ok is False
    assert "no pending confirmation" in (result.error or "")


def test_no_tool_can_resolve_a_confirmation() -> None:
    """The approval path is the companion's POST, never a callable tool."""
    registry = build_default_registry()
    watch_tools = [s for s in registry.all() if s.agent == "watch"]
    assert len(watch_tools) == 6
    for spec in watch_tools:
        assert int(spec.risk) <= 1, f"{spec.name} must stay at READ or LOW"
        assert spec.requires_confirmation is False
        assert not any(word in spec.name for word in ("approve", "resolve", "cancel"))


def test_watch_tools_are_registered_under_the_watch_agent() -> None:
    from jarvis.agents.base import build_agents

    registry = build_default_registry()
    names = {s.name for s in registry.all() if s.agent == "watch"}
    assert names == {
        "get_watch_status", "get_watch_outbox", "send_watch_notification",
        "vibrate_watch", "ask_watch_confirmation", "run_watch_routine",
    }
    agent = build_agents(registry)["watch"]
    assert agent.capabilities() == names


# ---------------------------------------------------------------------------
# The confirm_channel = watch wiring (plan §25)
# ---------------------------------------------------------------------------

DELETE_SOMETHING = "delete /tmp/jarvis-watch-confirm-me.txt"


async def test_confirm_channel_watch_offers_the_prompt(core: JarvisCore, paired: WatchGateway,
                                                       make_request, monkeypatch) -> None:
    monkeypatch.setattr(core.settings, "confirm_channel", "watch")
    response = await core.handle(make_request(DELETE_SOMETHING))
    assert response.needs_confirmation is True
    assert "watch channel" in response.reply.lower()
    assert "queued" in response.reply.lower()
    assert "delivered" not in response.reply.lower(), "queue mode must not claim delivery"
    assert [i.confirmation_id for i in paired.items] == [response.confirmation_id]


async def test_confirm_channel_watch_shows_when_a_companion_is_attached(
    core: JarvisCore, simulated: WatchGateway, make_request, monkeypatch
) -> None:
    """A simulated companion is named as one; it is never described as a screen.

    Phase 10 tightened this. The assertion used to be ``"shown on your watch"``,
    which a simulated transport satisfied only because it reports a confirmed
    delivery -- a fact about the record, not evidence anyone is wearing a watch.
    That wording also promised a wrist answer the gateway now refuses.
    """
    monkeypatch.setattr(core.settings, "confirm_channel", "watch")
    response = await core.handle(make_request(DELETE_SOMETHING))
    lower = response.reply.lower()
    assert "simulated watch companion" in lower, response.reply
    assert "shown on your watch" not in lower, "a stand-in must not be reported as a real watch"
    assert "nothing can be answered there" in lower, response.reply
    assert simulated.unanswered(), "the prompt still waits for an answer that cannot come"


async def test_confirm_channel_defaults_to_staying_quiet(core: JarvisCore, paired: WatchGateway,
                                                         make_request) -> None:
    """Nothing changes for a user who never opted into the watch channel."""
    assert core.settings.confirm_channel != "watch"
    response = await core.handle(make_request(DELETE_SOMETHING))
    assert "watch channel" not in response.reply.lower()
    assert paired.items == []


async def test_unpaired_watch_says_the_prompt_stayed_here(core: JarvisCore, make_request,
                                                          monkeypatch) -> None:
    monkeypatch.setattr(core.settings, "confirm_channel", "watch")
    response = await core.handle(make_request(DELETE_SOMETHING))
    assert "not paired" in response.reply.lower()
    assert "stayed here" in response.reply.lower()


# ---------------------------------------------------------------------------
# API: the companion's call-in path
# ---------------------------------------------------------------------------

@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-JARVIS-Token": TEST_TOKEN})


@pytest.fixture()
def anon_client() -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_watch_routes_require_a_token(anon_client: TestClient) -> None:
    assert anon_client.get("/watch/status").status_code == 401
    assert anon_client.post("/watch/collect", json={}).status_code == 401
    assert anon_client.post("/watch/reply", json={"item_id": "x", "decision": "CONFIRM"}).status_code == 401


def _hand_over(client: TestClient) -> dict:
    """The companion calls in, so every queued prompt reaches the screen."""
    return client.post("/watch/collect", json={"limit": 25}).json()


def test_companion_can_collect_ack_and_reply(client: TestClient, paired: WatchGateway) -> None:
    item = paired.queue("notification", "build finished")
    assert item.state == "queued"

    collected = client.post("/watch/collect", json={"limit": 5, "battery": 64, "charging": True})
    assert collected.status_code == 200
    payload = collected.json()
    assert payload["items"][0]["title"] == "build finished"
    assert payload["status"]["battery"] == 64
    assert item.state == "delivered"

    acked = client.post("/watch/ack", json={"item_id": item.item_id, "displayed": True})
    assert acked.status_code == 200 and acked.json()["item"]["state"] == "displayed"

    prompt = paired.queue("confirmation", "run the render?", confirmation_id="CONF-API-1")
    _hand_over(client)
    replied = client.post("/watch/reply", json={"item_id": prompt.item_id, "decision": "CANCEL"})
    assert replied.status_code == 200
    assert replied.json()["decision"] == "CANCEL"


def test_reply_validates_before_touching_anything(client: TestClient, paired: WatchGateway) -> None:
    assert client.post("/watch/reply", json={"item_id": "nope", "decision": "CONFIRM"}).status_code == 400

    prompt = paired.queue("confirmation", "copy the file?")
    _hand_over(client)
    assert client.post("/watch/reply", json={"item_id": prompt.item_id, "decision": "MAYBE"}).status_code == 400
    assert prompt.state != "replied", "a rejected answer changes nothing"

    assert client.post("/watch/ack", json={"item_id": "nope"}).status_code == 404


def test_a_wrist_cancel_runs_nothing(client: TestClient, paired: WatchGateway,
                                     tmp_area, core: JarvisCore) -> None:
    """CANCEL is final: nothing copied, and the same prompt cannot be re-answered."""
    source = tmp_area / "note.txt"
    source.write_text("hello from the laptop\n")
    destination = tmp_area / "copy.txt"

    action = PlannedAction(
        tool="copy_file",
        params={"source": str(source), "destination": str(destination)},
        device="laptop_main",
    )
    verdict = core.policy.evaluate(action)
    assert verdict.decision is PolicyDecision.CONFIRM
    item = paired.queue("confirmation", verdict.preview or "copy the file",
                        confirmation_id=verdict.confirmation_id)
    _hand_over(client)

    denied = client.post("/watch/reply", json={"item_id": item.item_id, "decision": "CANCEL"})
    assert denied.status_code == 200
    assert denied.json()["action"] == "confirmation_resolved"
    assert not destination.exists(), "a CANCEL must not have copied anything"
    assert core.policy.get_confirmation(verdict.confirmation_id)["state"] == "denied"

    changed_mind = client.post("/watch/reply", json={"item_id": item.item_id, "decision": "CONFIRM"})
    assert changed_mind.status_code == 400, "one prompt, one answer"
    assert not destination.exists()


def test_a_wrist_confirm_runs_the_action(client: TestClient, paired: WatchGateway,
                                        tmp_area, core: JarvisCore) -> None:
    source = tmp_area / "one.txt"
    source.write_text("content\n")
    destination = tmp_area / "two.txt"

    action = PlannedAction(
        tool="copy_file",
        params={"source": str(source), "destination": str(destination)},
        device="laptop_main",
    )
    verdict = core.policy.evaluate(action)
    item = paired.queue("confirmation", "copy the file?", confirmation_id=verdict.confirmation_id)
    _hand_over(client)

    response = client.post("/watch/reply", json={"item_id": item.item_id, "decision": "CONFIRM"})
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "confirmation_resolved"
    assert destination.exists(), "the wearer said CONFIRM, so the copy ran"
    assert core.policy.get_confirmation(verdict.confirmation_id)["state"] == "approved"


def test_a_routine_confirm_runs_its_command(client: TestClient, paired: WatchGateway) -> None:
    """A routine prompt dispatches through the real pipeline as source="watch"."""
    item = paired.queue("confirmation", "Start study mode?", command="run study mode",
                        routine="study")
    _hand_over(client)
    response = client.post("/watch/reply", json={"item_id": item.item_id, "decision": "CONFIRM"})
    body = response.json()
    assert response.status_code == 200
    assert body["action"] == "routine_dispatched"
    assert body["reply"], "the pipeline answered, whatever the home gateway's state was"

    cancelled = paired.queue("confirmation", "EMERGENCY: stop everything?",
                             command="stop all physical actions", routine="emergency")
    _hand_over(client)
    gone = client.post("/watch/reply", json={"item_id": cancelled.item_id, "decision": "CANCEL"})
    assert gone.json()["action"] == "routine_cancelled"
    assert "was not started" in gone.json()["reply"]


def test_a_routine_confirm_engages_the_emergency_stop(client: TestClient,
                                                      paired: WatchGateway) -> None:
    """A wearer answering the SOS prompt on the wrist runs the stop, and the stop
    reports what it actually did.

    This used to assert `"emergency stop" in reply`, which only passed because
    emergency_stop claimed "Emergency stop engaged. Physical actions cancelled"
    while cancelling nothing (Phase 9 P0-3). Nothing in this environment can
    reach an actuator, so the honest reply names the JARVIS-side work and says
    the physical layer was not stopped.
    """
    item = paired.queue("confirmation", "EMERGENCY: stop everything?",
                        command="stop all physical actions", routine="emergency",
                        priority="high", pattern="sos")
    _hand_over(client)
    body = client.post("/watch/reply", json={"item_id": item.item_id,
                                             "decision": "CONFIRM"}).json()
    assert body["action"] == "routine_dispatched"
    reply = body["reply"].lower()
    assert "cancel" in reply or "emergency-stop" in reply, body["reply"]
    assert "physical actions cancelled" not in reply, "must not claim a physical stop"
    assert "not stopped by this command" in reply, body["reply"]


# ---------------------------------------------------------------------------
# Planner routing
# ---------------------------------------------------------------------------

@pytest.fixture()
def planner(core: JarvisCore) -> RulePlanner:
    return core.planner


@pytest.mark.parametrize(
    "phrase,tool",
    [
        ("how is my watch", "get_watch_status"),
        ("what is my watch battery", "get_watch_status"),
        ("is my watch paired", "get_watch_status"),
        ("buzz my watch", "vibrate_watch"),
        ("make my watch vibrate long", "vibrate_watch"),
        ("send a notification to my watch that the build finished", "send_watch_notification"),
        ("notify my watch \"deploy done\"", "send_watch_notification"),
        ("ask my watch to confirm", "ask_watch_confirmation"),
        ("what is waiting on my watch", "get_watch_outbox"),
        ("start study mode on my watch", "run_watch_routine"),
        ("run the emergency routine on my watch", "run_watch_routine"),
    ],
)
def test_planner_routes_watch_phrases(planner: RulePlanner, phrase: str, tool: str) -> None:
    plan = planner.parse(phrase)
    assert plan.actions, f"{phrase!r} produced no action"
    assert plan.actions[0].tool == tool


def test_watch_battery_is_not_laptop_battery(planner: RulePlanner) -> None:
    assert planner.parse("what is my watch battery").actions[0].tool == "get_watch_status"
    assert planner.parse("what is my battery").actions[0].tool == "get_battery_status"


def test_watch_as_a_verb_is_not_the_device(planner: RulePlanner) -> None:
    """'watch this folder' and 'watch out' must not open the wearable."""
    for phrase in ("watch this folder for changes", "watch out", "I will watch it later"):
        tools = {a.tool for a in planner.parse(phrase).actions}
        assert not any(t.startswith("get_watch") or t.endswith("_watch") for t in tools), phrase


def test_watch_rule_leaves_other_surfaces_alone(planner: RulePlanner) -> None:
    assert planner.parse("run study mode").actions[0].tool == "run_home_routine"
    transfer = {a.tool for a in planner.parse("transfer this file to my phone").actions}
    assert "send_watch_notification" not in transfer
    assert planner.parse("check my car").actions[0].tool == "get_car_status"
    assert planner.parse("stop everything").actions[0].tool == "emergency_stop"


def test_notification_text_keeps_its_case(planner: RulePlanner) -> None:
    plan = planner.parse('notify my watch "Build Finished OK"')
    assert plan.actions[0].params["title"] == "Build Finished OK"


def test_planner_only_emits_registered_watch_tools(planner: RulePlanner) -> None:
    for phrase in ("how is my watch", "buzz my watch", "start study mode on my watch"):
        for action in planner.parse(phrase).actions:
            assert planner.tools.has(action.tool), action.tool


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

async def test_e2e_watch_status_unpaired_is_honest(core: JarvisCore, make_request) -> None:
    response = await core.handle(make_request("how is my watch"))
    assert response.actions and response.actions[0].ok
    assert "not paired" in response.reply.lower()
    assert response.needs_confirmation is False


async def test_e2e_notification_is_queued_not_claimed(core: JarvisCore, make_request,
                                                      paired: WatchGateway) -> None:
    response = await core.handle(make_request('notify my watch "tea is ready"'))
    assert "queued" in response.reply.lower()
    assert "nothing has reached your watch" in response.reply.lower()
    assert response.actions[0].output["delivered"] is False
    assert paired.items[0].state == "queued"
