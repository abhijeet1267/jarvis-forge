"""Phase 10: a watch that is not real must not authorise a real action.

Phase 9 reproduced the same class of defect twice: a confirmation prompt could be
approved from a watch surface that never showed it, and the approved confirmation
then ran a destructive tool for real. This file pins the authority rule down for
:meth:`jarvis.watch.gateway.WatchGateway.reply` and for the tool that displays a
prompt.

The rule the gateway has always documented
(``jarvis/watch/gateway.py`` docstring, "simulate"): *"It **delivers** items; it
never answers one. A simulated wearer cannot approve a confirmation, because a
runner that approves on the user's behalf is exactly what this system forbids."*
Until Phase 10 that sentence described an intention the code did not enforce.

Authorisation here is derived from **provenance**, never from an item-state
string, never from a tool name and never from the literal word ``watch``:

* the transport the gateway resolves -- a :class:`SimulatedTransport`, or
  ``simulate=True`` / ``JARVIS_WATCH_SIMULATE`` -- says whether anything outside
  this process was ever addressed at all;
* ``delivered_via`` says what actually took the item: a companion calling in, a
  configured bridge relay, the built-in stand-in, or nothing;
* the device registry says whether the watch is paired.

Nothing in this file pretends a device exists. A simulated send still reports
``delivered=True`` with ``mode="simulate"`` and its item still lands in the
outbox, because that part is true; what it can no longer do is resolve a
confirmation. Where a test below documents behaviour that is *not* yet safe, its
docstring says so plainly instead of asserting a property the code lacks.

Every test builds its own gateway and pairs the watch explicitly, so no real data
directory is touched and nothing is ever sent anywhere: the only transports used
are the queue-only transport, the built-in simulated companion, and a bridge
pointed at a closed localhost port or an in-test relay object.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from jarvis.api.app import app
from jarvis.core.models import PlannedAction, PolicyDecision
from jarvis.core.orchestrator import JarvisCore
from jarvis.db.database import get_db
from jarvis.registry.devices import get_device_registry
from jarvis.tools.base import ToolContext
from jarvis.tools.watch import ask_watch_confirmation, run_watch_routine, send_watch_notification
from jarvis.watch.gateway import (
    DEVICE_ID,
    BridgeTransport,
    DeliveryResult,
    SimulatedTransport,
    WatchGateway,
    WatchItem,
    get_watch_gateway,
    reset_watch_gateway,
)

TEST_TOKEN = os.environ["JARVIS_API_TOKEN"]
ctx = ToolContext()

QUEUE_NOTIFICATION_MESSAGE = (
    "Notification \"build finished\" is queued on the laptop, not delivered. "
    "Nothing has reached your watch yet."
)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch):
    """No leaked env flags, no leaked outbox, no leaked pairing state."""
    for var in ("JARVIS_WATCH_SIMULATE", "JARVIS_WATCH_BRIDGE", "JARVIS_WATCH_BRIDGE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    get_device_registry().set_auth_state(DEVICE_ID, "unpaired")
    reset_watch_gateway()
    yield
    reset_watch_gateway()
    get_device_registry().set_auth_state(DEVICE_ID, "unpaired")


@pytest.fixture()
def paired() -> WatchGateway:
    """A paired watch with the default transport: nothing is delivered by us."""
    get_device_registry().set_auth_state(DEVICE_ID, "paired", fingerprint="AAAA BBBB CCCC DDDD")
    gw = WatchGateway()
    reset_watch_gateway(gw)
    return gw


@pytest.fixture()
def simulated() -> WatchGateway:
    """A paired watch plus the auto-acknowledging companion -- the Phase 9 setup.

    This is the configuration the bug lived in: everything the outbox reports for
    a delivery is a claim made by our own stand-in, not by a device anyone is
    wearing.
    """
    get_device_registry().set_auth_state(DEVICE_ID, "paired", fingerprint="AAAA BBBB CCCC DDDD")
    gw = WatchGateway(simulate=True)
    reset_watch_gateway(gw)
    return gw


@pytest.fixture()
def client() -> TestClient:
    return TestClient(
        app, base_url="http://127.0.0.1:8765", headers={"X-JARVIS-Token": TEST_TOKEN}
    )


class UnshownSimulatedTransport(SimulatedTransport):
    """The other sub-state a fabricated delivery can occupy.

    A companion that took the item but never acknowledged the screen: the item
    lands in ``delivered`` instead of ``displayed``, and the pre-fix ``reply()``
    answered that too, because it only ever rejected ``queued``/``failed``.
    """

    def push(self, item: Dict[str, Any]) -> DeliveryResult:
        return DeliveryResult(True, False, "simulated", "taken by the stand-in, not shown")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _victim(tmp_area, name: str = "victim.txt"):
    victim = tmp_area / name
    victim.write_text("data that must survive\n")
    return victim


def _delete_permanently(path) -> PlannedAction:
    """The most destructive file operation the registry offers.

    ``permanent=True`` so that, if the guard ever fails, the damage is an
    unlinked file in this test's own scratch directory rather than something
    parked in the developer's real Trash.
    """
    return PlannedAction(
        tool="delete_file",
        params={"path": str(path), "permanent": True},
        device="laptop_main",
    )


def _copy(source, destination) -> PlannedAction:
    return PlannedAction(
        tool="copy_file",
        params={"source": str(source), "destination": str(destination)},
        device="laptop_main",
    )


def _needs_confirmation(core: JarvisCore, action: PlannedAction) -> str:
    """Run the real policy over a real action and return the confirmation id."""
    verdict = core.policy.evaluate(action)
    assert verdict.decision is PolicyDecision.CONFIRM, verdict.reason
    assert verdict.confirmation_id
    return verdict.confirmation_id


async def _show_on_watch(gw: WatchGateway, core: JarvisCore, confirmation_id: str) -> WatchItem:
    """Put an already-pending confirmation on the wrist through the real tool.

    ``ask_watch_confirmation`` reads the pending row out of the database and
    queues a prompt carrying exactly that id, so the prompt and the authorisation
    are the same pair the pipeline created.
    """
    assert confirmation_id in [p["confirmation_id"] for p in core.policy.pending()]
    result = await ask_watch_confirmation(ctx, confirmation_id=confirmation_id)
    assert result.ok is True, result.error
    item = gw.items[-1]
    assert item.confirmation_id == confirmation_id
    return item


def _drive(coro):
    """Run one coroutine from a test that is itself synchronous.

    The watch tools are ``async def``; pytest-asyncio gives the async tests a
    loop, and this is the only place a second one is opened.
    """
    return asyncio.run(coro)


def _hand_over(gw: WatchGateway) -> List[WatchItem]:
    """Stand in for the companion calling in: it takes what is queued.

    This is the *real* delivery path behind the API's ``/watch/collect``, so an
    item that comes back through it has genuine provenance.
    """
    return gw.take(limit=25)


def _existing_permissions() -> set:
    rows = get_db().query("SELECT permission_id FROM permissions", ())
    return {row["permission_id"] for row in rows}


def _forget_permissions_added_since(before: set) -> None:
    """Delete only the permission rows this test wrote.

    ``grant``/``revoke`` land in the session database, and test_policy.py exists in
    its current shape precisely because a leaked standing grant would otherwise
    follow the real tools into other modules: keep the ledger as you found it.
    """
    for row in get_db().query("SELECT permission_id FROM permissions", ()) or []:
        if row["permission_id"] not in before:
            get_db().execute(
                "DELETE FROM permissions WHERE permission_id = ?", (row["permission_id"],)
            )


# ---------------------------------------------------------------------------
# 1. The reproduction: a simulated wearer must not delete a real file.
# ---------------------------------------------------------------------------


async def test_simulated_wrist_confirm_does_not_delete_the_file(simulated, core, tmp_area):
    """Phase 9 reproduction, through the real tool and the real resolver.

    Before the fix ``simulated.reply(item_id, "CONFIRM")`` returned ``error == ""``,
    the caller went on to resolve the confirmation, and the file really was
    deleted. Now the answer is refused, the prompt stays unanswered, and the
    confirmation remains the laptop's to decide.
    """
    victim = _victim(tmp_area)
    cid = _needs_confirmation(core, _delete_permanently(victim))

    shown = await ask_watch_confirmation(ctx, confirmation_id=cid)
    item = simulated.items[-1]
    assert item.confirmation_id == cid
    assert item.delivered_via == "simulated"
    assert item.state == "displayed"
    # The wording and the guard are the same fact, said twice: the tool offers an
    # answer this prompt can never carry.
    assert "simulated wearer cannot confirm or cancel" in shown.message
    assert "Answer it on the watch" not in shown.message

    answered, error = simulated.reply(item.item_id, "CONFIRM")

    assert error, "a simulated delivery must not be able to authorise a real deletion"
    assert "simulat" in error.lower()
    assert answered.reply == "", "a refused answer must not be recorded as the wearer's"
    assert answered.state == "displayed"
    assert simulated.counts()["replied"] == 0
    assert core.policy.get_confirmation(cid)["state"] == "pending"
    assert victim.exists(), "the file must still be there"


def test_reply_endpoint_refuses_a_simulated_delivery(client, simulated, core, tmp_area):
    """The fix must compose with ``/watch/reply`` exactly as it already stands.

    The endpoint raises 400 whenever ``reply()`` returns a reason, and only
    touches the resolver after that point, so refusing inside the gateway closes
    the escape with no change to app.py.
    """
    victim = _victim(tmp_area)
    cid = _needs_confirmation(core, _delete_permanently(victim))
    shown = _drive(_show_on_watch(simulated, core, cid))

    response = client.post("/watch/reply", json={"item_id": shown.item_id, "decision": "CONFIRM"})

    assert response.status_code == 400
    assert "simulat" in response.json()["detail"].lower()
    assert victim.exists()
    assert core.policy.get_confirmation(cid)["state"] == "pending"
    assert get_watch_gateway().find(shown.item_id).state == "displayed"


@pytest.mark.parametrize("decision", ["CONFIRM", "CANCEL"])
async def test_a_cancel_from_a_simulated_wearer_is_refused_too(
    simulated, core, tmp_area, decision
):
    """Recording a decline nobody made is as false as recording an approval.

    A CANCEL also flips the pending confirmation to denied, so that direction is
    not the harmless one it looks like.
    """
    _victim(tmp_area)
    cid = _needs_confirmation(core, _delete_permanently(tmp_area / "victim.txt"))
    item = await _show_on_watch(simulated, core, cid)

    answered, error = simulated.reply(item.item_id, decision)

    assert error
    assert answered.reply == ""
    assert core.policy.get_confirmation(cid)["state"] == "pending"


# ---------------------------------------------------------------------------
# 2. Truthful wording: the prompt must not read as a claim about hardware.
# ---------------------------------------------------------------------------


async def test_simulated_confirmation_says_no_real_watch_showed_it(simulated):
    """The sentence meant for the user used to claim a physical device, flatly."""
    result = await ask_watch_confirmation(ctx, question="start the render?")

    assert result.output["mode"] == "simulate"
    assert result.output["item"]["delivered_via"] == "simulated"
    assert result.output["state"] == "displayed"
    assert "simulat" in result.message.lower()
    assert "shown on the watch" not in result.message
    assert "shown on your watch" not in result.message
    # Stated as "nothing was shown", never as "nobody answered".
    assert "not answered" not in result.message.lower()
    assert "Nothing has been shown on a real watch" in result.message
    assert "Answer it on the watch" not in result.message
    assert simulated.items[-1].outcome_word != "shown on the watch"
    assert simulated.items[-1].outcome_word == (
        "handed to the simulated companion, not shown on a real watch"
    )


async def test_a_real_delivery_still_reads_as_a_real_delivery(paired):
    """Truth in both directions: a companion that called in is not a stand-in."""
    prompt = paired.queue("confirmation", "copy the file?")
    paired.take()
    paired.ack(prompt.item_id, displayed=True)

    assert prompt.outcome_word == "shown on the watch"
    assert prompt.delivered_via == "companion_poll"


# ---------------------------------------------------------------------------
# 3. The honest notification path stays as it was.
# ---------------------------------------------------------------------------


async def test_queue_mode_notification_still_says_queued_not_delivered(paired):
    """The sentence this project already got right must not change."""
    result = await send_watch_notification(ctx, title="build finished", body="42 tests")

    assert result.output["queued"] is True
    assert result.output["delivered"] is False
    assert result.message == QUEUE_NOTIFICATION_MESSAGE
    assert paired.items[0].outcome_word == "queued on the laptop, not delivered"


async def test_simulated_notification_stops_short_of_claiming_a_screen(simulated):
    """A stand-in taking a notification is not a watch that showed it."""
    result = await send_watch_notification(ctx, title="laundry finished")

    assert result.ok is True
    assert result.output["delivered"] is True, "the simulated companion really did take it"
    assert result.output["mode"] == "simulate"
    assert result.output["item"]["delivered_via"] == "simulated"
    assert "simulat" in result.message.lower()
    assert "shown on the watch" not in result.message

    # Dismissing a notification decides nothing, so it stays answerable: outbox
    # bookkeeping must not be collateral damage of the authority guard.
    item = simulated.items[0]
    dismissed, error = simulated.reply(item.item_id, "DISMISSED")
    assert not error
    assert dismissed.state == "replied"


async def test_routine_prompt_from_a_simulated_wearer_runs_nothing(simulated):
    """A routine CONFIRM dispatches a command into the pipeline, so it needs the
    same provenance a confirmation does -- the emergency stop included.
    """
    result = await run_watch_routine(ctx, routine="emergency")
    assert result.output["started"] is False
    item = simulated.items[-1]
    assert item.command == "stop all physical actions"
    # Conditional wording only: the promise "answer CONFIRM on the watch" would be
    # a claim about a button press no wearer was shown.
    assert "It would run" in result.message
    assert "when you answer CONFIRM." not in result.message
    assert "Nothing has started yet" in result.message

    answered, error = simulated.reply(item.item_id, "CONFIRM")

    assert error
    assert answered.reply == ""
    assert simulated.counts()["replied"] == 0


# ---------------------------------------------------------------------------
# 4. Authority matrix: unpaired, pending, revoked device, offline.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("auth_state", ["unpaired", "pending", "revoked"])
def test_a_watch_that_is_not_paired_cannot_authorise(paired, core, tmp_area, auth_state):
    """Pairing is a device-registry fact, so withdrawing it mid-flight cuts authority.

    The prompt reached the wrist while the watch was paired and a companion did
    call in; the answer arrives after the pairing was withdrawn. The event is
    genuine, the authority behind it is not.
    """
    victim = _victim(tmp_area)
    cid = _needs_confirmation(core, _delete_permanently(victim))
    prompt = paired.queue("confirmation", f"Delete {victim.name}?", confirmation_id=cid)
    paired.take()
    paired.ack(prompt.item_id, displayed=True)
    assert prompt.state == "displayed" and prompt.delivered_via == "companion_poll"

    get_device_registry().set_auth_state(DEVICE_ID, auth_state)
    answered, error = paired.reply(prompt.item_id, "CONFIRM")

    assert error, f"auth_state={auth_state} must not carry authority"
    assert "pair" in error.lower()
    assert answered.reply == ""
    assert victim.exists()
    assert core.policy.get_confirmation(cid)["state"] == "pending"


def test_offline_paired_watch_confirm_is_refused(core, tmp_area):
    """Paired but unreachable: the bridge did not answer, so nothing was shown."""
    get_device_registry().set_auth_state(DEVICE_ID, "paired", fingerprint="AAAA BBBB CCCC DDDD")
    gw = WatchGateway(
        transport=BridgeTransport("http://127.0.0.1:9/push", timeout=0.2)  # port 9: closed
    )
    reset_watch_gateway(gw)

    victim = _victim(tmp_area)
    cid = _needs_confirmation(core, _delete_permanently(victim))
    prompt = gw.queue("confirmation", f"Delete {victim.name}?", confirmation_id=cid)

    assert prompt.state == "queued"
    assert gw.paired is True
    assert gw.status()["online"] is False, "nothing outside this process has been heard from"

    answered, error = gw.reply(prompt.item_id, "CONFIRM")
    assert "has not reached your watch" in error
    assert victim.exists()

    # The sharper half: a record that *claims* the prompt was displayed with no
    # delivery provenance behind the claim. A state string is not evidence.
    prompt.state = "displayed"
    assert prompt.delivered_via == ""
    answered, error = gw.reply(prompt.item_id, "CONFIRM")
    assert error
    assert answered.reply == ""
    assert victim.exists()
    assert core.policy.get_confirmation(cid)["state"] == "pending"


def test_simulated_confirm_is_refused_in_the_delivered_substate(core, tmp_area):
    """A fabricated delivery that never even claims a screen is still fabricated."""
    get_device_registry().set_auth_state(DEVICE_ID, "paired")
    gw = WatchGateway(transport=UnshownSimulatedTransport())
    reset_watch_gateway(gw)

    victim = _victim(tmp_area)
    cid = _needs_confirmation(core, _delete_permanently(victim))
    prompt = gw.queue("confirmation", f"Delete {victim.name}?", confirmation_id=cid)
    assert prompt.state == "delivered"
    assert prompt.delivered_via == "simulated"

    answered, error = gw.reply(prompt.item_id, "CONFIRM")

    assert error
    assert answered.reply == ""
    assert victim.exists()
    assert core.policy.get_confirmation(cid)["state"] == "pending"


# ---------------------------------------------------------------------------
# 5. Expiry, substitution, replay -- an answer is one answer.
# ---------------------------------------------------------------------------


def _backdate(confirmation_id: str, seconds: int) -> None:
    """Age a confirmation past its window instead of sleeping for it."""
    stale = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    get_db().execute(
        "UPDATE confirmations SET created_at = ? WHERE confirmation_id = ?",
        (stale, confirmation_id),
    )


async def test_a_late_answer_on_an_expired_confirmation_runs_nothing(paired, core, tmp_area):
    """Expiry is enforced by the resolver, and the composition holds.

    Reported as it is: the gateway does not itself consult the confirmation's
    clock -- it records a wrist answer from a real delivery and leaves the decision
    to the resolver, which refuses. Either way the file must survive.
    """
    source = tmp_area / "one.txt"
    source.write_text("content\n")
    destination = tmp_area / "two.txt"
    cid = _needs_confirmation(core, _copy(source, destination))

    prompt = paired.queue("confirmation", "copy the file?", confirmation_id=cid)
    _hand_over(paired)
    _backdate(cid, core.policy._expiry_seconds() + 10)
    assert core.policy.is_expired(core.policy.get_confirmation(cid)) is True

    answered, error = paired.reply(prompt.item_id, "CONFIRM")
    assert not error, "the answer came from a genuine delivery; expiry is the resolver's call"

    response = await core.resolve_confirmation(cid, True, session_id="s1")

    assert "expired" in response.reply.lower()
    assert response.actions == [], "nothing may execute once the window has closed"
    assert not destination.exists()
    assert core.policy.get_confirmation(cid)["state"] == "expired"


async def test_answering_one_prompt_never_runs_another_confirmation(paired, core, tmp_area):
    """Approve A and B stays undone.

    ``find()`` matches an item id *or* a confirmation id, so an answer can only
    ever travel to the confirmation written on the very item being answered.
    """
    source = tmp_area / "source.txt"
    source.write_text("payload\n")
    dst_a = tmp_area / "a.txt"
    dst_b = tmp_area / "b.txt"
    cid_a = _needs_confirmation(core, _copy(source, dst_a))
    cid_b = _needs_confirmation(core, _copy(source, dst_b))

    item_a = paired.queue("confirmation", "copy to a?", confirmation_id=cid_a)
    item_b = paired.queue("confirmation", "copy to b?", confirmation_id=cid_b)
    _hand_over(paired)

    _, error = paired.reply(item_a.item_id, "CONFIRM")
    assert not error
    await core.resolve_confirmation(cid_a, True, session_id="s2")

    assert dst_a.exists(), "the prompt the wearer answered was A"
    assert not dst_b.exists(), "answering A must never run B"
    assert item_b.reply == "" and item_b.state == "delivered"
    assert core.policy.get_confirmation(cid_b)["state"] == "pending"


async def test_a_replayed_answer_changes_nothing(paired, core, tmp_area):
    """One prompt, one answer -- and no second execution off the same record."""
    source = tmp_area / "one.txt"
    source.write_text("content\n")
    destination = tmp_area / "two.txt"
    cid = _needs_confirmation(core, _copy(source, destination))
    prompt = paired.queue("confirmation", "copy the file?", confirmation_id=cid)
    _hand_over(paired)

    first, error = paired.reply(prompt.item_id, "CONFIRM")
    assert not error
    executed = await core.resolve_confirmation(cid, True, session_id="s3")
    assert executed.actions, "the wearer's one answer did run the action"
    destination.write_text("an edit made after the copy\n")

    second, error = paired.reply(prompt.item_id, "CONFIRM")
    assert "Already answered" in error
    assert second.state == "replied"
    again = await core.resolve_confirmation(cid, True, session_id="s3")
    assert "already" in again.reply.lower()
    assert again.actions == [], "a resolved confirmation must not execute twice"
    assert destination.read_text() == "an edit made after the copy\n"


# ---------------------------------------------------------------------------
# 6. Revoked permission at resolution time: what actually happens today.
# ---------------------------------------------------------------------------


async def test_a_permission_revoked_after_the_prompt_blocks_execution(paired, core, tmp_area):
    """A revocation issued after the watch prompt must stop the recorded action.

    This test was written by the watch-authority work to *record an open gap*:
    the answer came from a genuinely real delivery, so provenance was intact, yet
    ``resolve_confirmation`` executed the stored action even though
    ``policy.evaluate`` on the same action now returned BLOCK. Phase 10 P1-1
    closed it -- ``pre_execute_check`` re-runs the refusal gates at resolution --
    so the assertions below now demand the refusal instead of the execution.
    """
    source = tmp_area / "one.txt"
    source.write_text("content\n")
    destination = tmp_area / "two.txt"
    action = _copy(source, destination)
    cid = _needs_confirmation(core, action)

    before = _existing_permissions()
    try:
        core.policy.revoke("copy_file", "laptop_main")
        recheck = core.policy.evaluate(action)
        assert recheck.decision is PolicyDecision.BLOCK
        assert "revoked" in recheck.reason

        prompt = paired.queue("confirmation", "copy the file?", confirmation_id=cid)
        _hand_over(paired)
        answered, error = paired.reply(prompt.item_id, "CONFIRM")
        assert not error, "a real companion took this prompt: provenance is intact"

        response = await core.resolve_confirmation(cid, True, session_id="s5")

        # GAP CLOSED: the answer is genuine, and still not sufficient.
        assert response.decision is PolicyDecision.BLOCK, response.reply
        assert not response.actions, "a revoked tool executed anyway"
        assert not destination.exists(), "the copy ran on a permission that no longer stands"
        assert "no longer allowed" in response.reply and "revoked" in response.reply
    finally:
        _forget_permissions_added_since(before)

    # With the standing revocation gone, an unrelated watch answer is still judged
    # on provenance: the same prompt with a fabricated delivery stays unanswerable.
    cid_two = _needs_confirmation(core, _copy(source, tmp_area / "three.txt"))
    get_device_registry().set_auth_state(DEVICE_ID, "paired")
    fake = WatchGateway(simulate=True)
    reset_watch_gateway(fake)
    simulated_prompt = fake.queue("confirmation", "copy the file?", confirmation_id=cid_two)
    assert fake.reply(simulated_prompt.item_id, "CONFIRM")[1]
    assert not (tmp_area / "three.txt").exists()


# ---------------------------------------------------------------------------
# 7. Positive control: a real wrist answer still authorises.
# ---------------------------------------------------------------------------


async def test_a_real_companion_answer_still_authorises_the_action(paired, core, tmp_area):
    """The guard is about provenance, not about being difficult.

    Paired watch, no simulate anywhere, and a companion that called in and took
    the prompt: this is the flow the feature exists for, and a CONFIRM in it still
    runs the copy.
    """
    source = tmp_area / "one.txt"
    source.write_text("content\n")
    destination = tmp_area / "two.txt"
    cid = _needs_confirmation(core, _copy(source, destination))

    prompt = paired.queue("confirmation", "copy the file?", confirmation_id=cid)
    assert paired.reply(prompt.item_id, "CONFIRM")[1], "still in the outbox: nothing to answer"
    _hand_over(paired)

    answered, error = paired.reply(prompt.item_id, "CONFIRM")
    assert not error
    response = await core.resolve_confirmation(cid, True, session_id="s4")

    assert answered.reply == "CONFIRM" and answered.state == "replied"
    assert destination.exists(), "the wearer said CONFIRM, so the copy ran"
    assert response.actions and response.actions[0].ok
    assert core.policy.get_confirmation(cid)["state"] == "approved"


def test_a_bridge_relay_delivery_is_its_own_provenance(core, tmp_area):
    """A configured relay that answers ``{"ok": true}`` is a real transport.

    Not proof that a human saw anything -- the item is on the relay -- but not our
    own stand-in either, so the answer is not rejected for provenance.
    """
    get_device_registry().set_auth_state(DEVICE_ID, "paired")
    taken: List[Dict[str, Any]] = []

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"ok": true}'

    def relay(request, timeout=0):  # noqa: ARG002 - urllib opener signature
        taken.append({"headers": dict(request.headers), "data": request.data})
        return _Response()

    gw = WatchGateway(transport=BridgeTransport("http://relay.invalid/push", opener=relay))
    reset_watch_gateway(gw)
    assert gw.mode == "bridge" and gw.simulate is False

    prompt = gw.queue("confirmation", "copy the file?", confirmation_id="CONF-RELAY")
    assert prompt.state == "displayed" and prompt.delivered_via == "bridge"
    assert len(taken) == 1, "the prompt was really POSTed to the configured relay"

    answered, error = gw.reply(prompt.item_id, "CANCEL")
    assert not error and answered.reply == "CANCEL"
