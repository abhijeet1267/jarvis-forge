"""Phase 31: the device that changes state must be the device the user named.

Measured on the live tree, before anything was changed:

```
resolve("heater")                     -> study.ac / Study AC / ac
resolve("hvac")                       -> study.ac / Study AC / ac
control_home_device(device="heater")  -> ok=True   "…I could not switch on Study AC…"
control_home_device spec              -> risk=LOW, requires_confirmation=False
"Jarvis, turn on heater."             -> decision=EXECUTE, nothing asked of the user
```

The cause is one line: the gateway files ``heater`` and ``hvac`` as synonyms of the
*kind* ``ac``. Heating is not cooling. The consequence is that a word for an appliance
this house does not have is silently rerouted onto an appliance it does -- and the
ambiguity guard Phase 22 added cannot catch it, because the conflation makes the wrong
device look like the single correct answer. The plan's §45 matrix has printed FAIL for
this phrase since Phase 22 (22-G), and the reason no heater-like thing ever got switched
is that the gateway happens to be offline: an accident of the environment, not a policy.

Three tightenings, in the same commit because they are one defect seen from three layers:

* the conflation is removed from the vocabulary;
* an **actuation** resolves strictly -- by id, by name, or by a word that is genuinely a
  kind of the device it points at -- where a read may keep guessing; a word that merely
  scores well enough asks instead of switching;
* ``control_home_device`` requires confirmation, so a physical change crosses the same
  gate everything else does. A standing grant still makes repeat requests quiet, which is
  why this costs the user one prompt rather than a habit.

And the permission engine grows a device guard beside the path guard, because an approval
requested for the wrong reason is not consent: JARVIS must not ask "switch on Study AC?"
when the user said "heater".
"""

from __future__ import annotations

import asyncio
import json

import pytest
from jarvis.core.models import CommandRequest, PolicyDecision, PlannedAction, RiskLevel
from jarvis.home.gateway import KIND_SYNONYMS, get_home_gateway

#: Words this house has no device for. Each one used to reach Study AC.
NOT_IN_THIS_HOUSE = ["heater", "hvac", "radiator", "boiler", "furnace"]

#: The gateway's state as the module loads, so "nothing was switched" is a comparison
#: rather than an assumption that every device starts off (the study desk lamp does not).
_SWITCH_STATE = None

#: Words that genuinely name the air conditioner, and must keep working.
ACTUALLY_THE_AC = ["ac", "aircon", "air", "conditioner", "study ac", "study.ac"]


@pytest.fixture(autouse=True)
def _registry_restored(core):
    """The registry is session-scoped: a capability registered or a handler stubbed for
    one test would answer sentences in every later one. Snapshot, then put it back.

    Phase 33 found this class of leak by running its file after Phase 30's: a stub
    handler left installed made a full-suite run fail and a single-file run pass, which
    is the worst shape a test signal can have.
    """
    import copy

    specs = dict(core.tools._specs)
    handlers = dict(core.tools._handlers)
    nested = {name: (copy.deepcopy(s.provides_identifiers), copy.deepcopy(s.parameters))
              for name, s in specs.items()}
    yield
    core.tools._specs.clear()
    core.tools._specs.update(specs)
    core.tools._handlers.clear()
    core.tools._handlers.update(handlers)
    for name, (provides, parameters) in nested.items():
        spec = core.tools._specs.get(name)
        if spec is None:
            continue
        spec.provides_identifiers.clear()
        spec.provides_identifiers.update(provides)
        spec.parameters.clear()
        spec.parameters.update(parameters)


def _run(core, text: str, session_id: str = "phase31"):
    return asyncio.run(core.handle(CommandRequest(
        text=text, source="phase31", session_id=session_id)))


def _control(core, device: str, action: str = "on", value=None):
    class Ctx:
        device_id = "laptop_main"
        session_id = "p31-direct"

    handler = core.tools.handler("control_home_device")
    return asyncio.run(handler(Ctx(), device=device, action=action, value=value))


@pytest.fixture(autouse=True)
def _gateway_state_snapshot():
    """Capture the gateway's on/off map once, and put it back after each test.

    The assertions about "nothing was switched" are comparisons against this, not
    against an assumption that devices start off -- the default inventory has the study
    desk lamp on -- and a control call that did change state must not leak into the
    next test.
    """
    global _SWITCH_STATE
    gw = get_home_gateway()
    if _SWITCH_STATE is None:
        _SWITCH_STATE = {d.device_id: d.on for d in gw.devices}
    yield
    for device in gw.devices:
        device.on = _SWITCH_STATE[device.device_id]


# ---------------------------------------------------------------------------
# 1. heating is not cooling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("word", NOT_IN_THIS_HOUSE)
def test_a_word_for_an_absent_appliance_names_no_appliance(core, word):
    """The old answer was a sentence about Study AC. The user never said AC; every
    word in this list was filed under the air conditioner's kind."""
    result = _control(core, word)
    assert result.ok is False, (word, result.message)
    said = result.error or result.message or ""
    assert "Study AC" not in said, (word, said)
    assert {d.device_id: d.on for d in get_home_gateway().devices} == _SWITCH_STATE, (
        word, "a device was switched on for a word that names none")


def test_the_vocabulary_stops_calling_a_heater_an_air_conditioner(core):
    assert "heater" not in KIND_SYNONYMS["ac"], sorted(KIND_SYNONYMS["ac"])
    assert "hvac" not in KIND_SYNONYMS["ac"], sorted(KIND_SYNONYMS["ac"])


@pytest.mark.parametrize("word", ACTUALLY_THE_AC)
def test_a_word_that_really_means_the_ac_still_reaches_it(core, word):
    """The fix is not "refuse everything": narrowing the vocabulary must not cost the
    phrases that were always correct."""
    result = _control(core, word)
    assert "heater" not in (result.message or "")
    assert "Study AC" in (result.message or ""), (word, result.message)


# ---------------------------------------------------------------------------
# 2. an actuation resolves strictly; a read may guess
# ---------------------------------------------------------------------------


def test_reading_the_gateway_may_still_answer_a_fuzzy_word(core):
    """The strict rule is about *changing the world*. ``resolve`` keeps its
    first-wins behaviour for read callers, and this test keeps that from being
    "fixed" into a second refusal path nobody asked for."""
    assert get_home_gateway().resolve("study ac") is not None
    assert get_home_gateway().resolve("the fan") is not None


def test_a_word_that_only_scores_high_enough_does_not_actuate(core):
    """`fridge` is a real device ("Kitchen fridge power"), so this asserts the positive
    too -- but `fridge door` must not reach it by score."""
    result = _control(core, "fridge door")
    assert result.ok is False, result.error or result.message
    assert "Kitchen fridge" not in (result.error or result.message or "")


def test_an_ambiguous_word_still_asks_and_names_the_candidates(core):
    """Phase 22's guard must survive this phase intact."""
    result = _control(core, "ceiling")
    assert result.ok is False
    said = result.error or result.message
    assert "Living room ceiling light" in said, said
    assert "Bedroom ceiling light" in said, said


def test_a_room_word_acts_on_nothing_in_the_room(core):
    """A room is not a device. Compared against the state before the call, because
    the default inventory ships with the study desk lamp already on."""
    gw = get_home_gateway()
    before = {d.device_id: d.on for d in gw.devices}
    result = _control(core, "study")
    assert result.ok is False, result.error or result.message
    assert {d.device_id: d.on for d in gw.devices} == before


# ---------------------------------------------------------------------------
# 3. 22-G -- a physical change crosses the gate
# ---------------------------------------------------------------------------


def test_switching_a_home_device_requires_confirmation(core):
    spec = core.tools.get("control_home_device")
    assert spec.requires_confirmation is True, "a physical actuation was auto-approved"
    assert spec.risk is RiskLevel.LOW, "the tier stays honest about what dimming a lamp is"


def test_a_named_device_is_offered_for_approval_not_executed(core):
    calls = {"n": 0}
    original = core.tools._handlers["control_home_device"]

    async def counting(ctx, **kw):
        calls["n"] += 1
        return await original(ctx, **kw)

    core.tools._handlers["control_home_device"] = counting
    try:
        response = _run(core, "turn on the study ac", session_id="p31-confirm")
    finally:
        core.tools._handlers["control_home_device"] = original
    assert calls["n"] == 0, "the handler ran before anyone approved it"
    assert response.decision is PolicyDecision.CONFIRM, response.reply


def test_a_standing_grant_keeps_repeat_requests_quiet(core):
    """The cost of confirming a lamp is one prompt, forever after waived. Without this,
    "require confirmation" would read as an argument about safety that ignores its own
    price."""
    # Rows are deleted rather than revoked: a revocation is permanent and the core is
    # session-scoped, so leaving one here would block the tool for every later test.
    try:
        core.policy.grant("control_home_device", "laptop_main")
        response = _run(core, "turn on the study ac", session_id="p31-granted")
        assert response.decision is PolicyDecision.EXECUTE, response.reply
    finally:
        core.db.execute("DELETE FROM permissions WHERE tool = ?", ("control_home_device",))


def test_the_approval_question_names_the_device_that_would_change(core):
    """A confirmation is a claim about what will happen. The preview must not say
    "Study AC" because the word "heater" scored its way there."""
    response = _run(core, "turn on the study ac", session_id="p31-preview")
    assert response.confirmation_id, response.reply
    # The prompt has to name the thing that will change, in the words that identify it.
    assert "study ac" in response.reply.lower(), response.reply
    assert "heater" not in response.reply.lower(), response.reply


# ---------------------------------------------------------------------------
# 4. no approval for a device that cannot be identified
# ---------------------------------------------------------------------------


def test_the_plan_heater_phrase_is_refused_not_executed(core):
    """§45's phrase. It has printed FAIL since Phase 22; the expected outcomes are
    REJECTED / BLOCKED / AWAITING APPROVAL / NOTHING EXECUTED."""
    response = _run(core, "Jarvis, turn on heater.", session_id="p31-plan")
    assert response.decision in (PolicyDecision.BLOCK, PolicyDecision.CONFIRM), response.reply
    assert not response.actions, [a.tool for a in response.actions]
    assert {d.device_id: d.on for d in get_home_gateway().devices} == _SWITCH_STATE, (
        "an appliance was switched for a word that names none of them")


def test_no_approval_is_requested_for_an_unidentifiable_device(core):
    """Asking "switch on Study AC?" when the user said "heater" is consent gathered for
    the wrong reason -- the reason the path guard blocks instead of confirming."""
    action = PlannedAction(step_id=1, tool="control_home_device",
                           params={"device": "heater", "action": "on"}, device="laptop_main")
    result = core.policy.evaluate(action, session_id="p31-guard")
    assert result.decision is PolicyDecision.BLOCK, result.reason
    assert result.confirmation_id in (None, ""), "a confirmation was minted for a guess"


def test_a_guarded_refusal_tells_the_user_what_does_exist(core):
    response = _run(core, "Jarvis, turn on heater.", session_id="p31-candidates")
    assert "heater" in response.reply.lower()
    assert any(name in response.reply for name in
               ("Study AC", "Study desk lamp", "Living room", "kitchen")), response.reply


def test_the_refusal_is_in_the_ledger_as_a_block(core):
    response = _run(core, "Jarvis, turn on heater.", session_id="p31-ledger")
    row = next(r for r in core.audit.recent(15) if r.get("request_id") == response.request_id)
    assert row["result"] == "blocked", row


def test_a_blocked_action_records_why_the_gate_said_no(core):
    """Phase 16 made a planner's refusal accountable by naming its reason in the chain.
    The permission engine's own blocks -- the security-relevant ones -- recorded
    ``{"ok": false, "message": ""}``: the verdict had the reason and the call site
    simply did not pass it, so the most important rows in the ledger were the least
    explained."""
    response = _run(core, "Jarvis, turn on heater.", session_id="p31-why")
    row = next(r for r in core.audit.recent(15) if r.get("request_id") == response.request_id)
    detail = row.get("detail") or {}
    assert "heater" in json.dumps(detail), detail
    assert detail.get("message"), detail


def test_a_routine_step_with_a_bad_device_id_is_refused_by_the_same_guard(core):
    """The scheduler re-evaluates through this engine on every run, so the guard has to
    live in the engine rather than in the typed-sentence path."""
    action = PlannedAction(step_id=1, tool="control_home_device",
                           params={"device": "kitchen.heater", "action": "on"},
                           device="laptop_main")
    result = core.policy.evaluate(action, session_id="p31-scheduled")
    assert result.decision is PolicyDecision.BLOCK, result.reason


def test_the_guard_does_not_touch_tools_that_carry_no_home_device(core):
    """A guard that fires on every action is a second permission system, which is not
    what this is for."""
    for tool, params in (("get_time", {}), ("get_battery_status", {}),
                         ("list_workspaces", {}), ("read_file", {"path": "README.md"})):
        action = PlannedAction(step_id=1, tool=tool, params=params, device="laptop_main")
        result = core.policy.evaluate(action, session_id="p31-others")
        assert result.decision is not PolicyDecision.BLOCK or "home" not in result.reason, (
            tool, result.reason)
