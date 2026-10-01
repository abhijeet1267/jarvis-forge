"""Phase 32: the words the router accepts and the thing the approval names must be one thing.

Two items Phase 31 carried, both measured on the live tree first.

**31-A -- two vocabularies for one request.** The gateway understands 26 device words, in
``KIND_SYNONYMS``, which is also what the actuation resolver matches against. The planner's
home route matched a hardcoded eleven. Nine words the system can genuinely act on were
therefore answered as nonsense:

```
turn on the power       -> intent=unknown  kind=not_understood
turn on the outlet      -> intent=unknown  kind=not_understood
turn on the bulb        -> intent=unknown  kind=not_understood
turn on the ceiling     -> intent=unknown  kind=not_understood
turn on the conditioner -> intent=unknown  kind=not_understood
turn on the motion      -> intent=unknown  kind=not_understood
```

"power" is a synonym for a plug in the table the gateway uses to decide what a request
means. This is the same shape Phase 30 fixed for *tool* names -- the router's private list
being narrower than the registry it routes into -- and it has the same cure: derive the list.

```
turn on the power       -> Which power did you mean -- Study standing desk, Living room media
                           power, Kitchen fridge power, Bedroom alarm clock? I will not switch
                           one of them on a guess.
```

**31-B -- consent gathered against a string.** The permission engine builds a ``preview`` for
every confirmation, the dashboard renders it, and the chat prompt *ignored the argument it
was handed* and re-derived its own line from the raw parameters:

```
stored preview: control_home_device on laptop_main | device=kitchen light | action=set | value=40
chat reply:     Details: device=kitchen light, action=set, value=40
```

So the same approval exists in two renderings that are free to drift, and neither says which
device the gateway will actually change (``kitchen.light`` -- "Kitchen light") or what will
happen to it. An approval is a question about one specific thing, which is exactly the
argument Phase 31 used to justify blocking an unidentifiable device; the same argument applies
one step later, to the device that *was* identified.
"""

from __future__ import annotations

import asyncio
import copy

import pytest
from jarvis.core.models import CommandRequest, PolicyDecision
from jarvis.home.gateway import KIND_SYNONYMS, get_home_gateway

#: The words the gateway itself declares. Asserted over the live table rather than a
#: copy of it, because the point of this phase is that there is only one table.
DEVICE_WORDS = sorted({w for words in KIND_SYNONYMS.values() for w in words})


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


def _run(core, text: str, session_id: str = "phase32"):
    return asyncio.run(core.handle(CommandRequest(
        text=text, source="phase32", session_id=session_id)))


def _parse(core, text: str):
    return core.planner.parse(text)


# ---------------------------------------------------------------------------
# 1. one vocabulary, and it is the gateway's
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("word", DEVICE_WORDS)
def test_a_device_word_the_gateway_knows_is_never_answered_as_nonsense(core, word):
    """The sweep that found nine unreachable words.

    The honest failure for a request the system *can* serve is a question or a refusal,
    never "I understood the words but not the goal".
    """
    plan = _parse(core, f"turn on the {word}")
    assert plan.reply_kind != "not_understood", (word, plan.direct_reply)


def test_the_router_follows_the_gateway_when_a_word_is_added(core):
    """Derivation, not coincidence: teach the gateway a new word and the router accepts it.

    A hardcoded list would keep refusing "whirligig" forever, and no test written against
    the current eleven words would notice the two lists drifting apart again. "fan" is
    chosen because this house has exactly one fan, so the word resolves to a device rather
    than to an ambiguity question -- which is what proves the router used the new word, not
    merely recognised that a home sentence arrived.
    """
    original = copy.deepcopy(KIND_SYNONYMS)
    try:
        KIND_SYNONYMS["fan"].add("whirligig")
        plan = _parse(core, "turn on the whirligig")
        assert plan.reply_kind != "not_understood", plan.direct_reply
        assert [a.tool for a in plan.actions] == ["control_home_device"], plan.direct_reply
        assert plan.actions[0].params["device"] == "whirligig"
    finally:
        KIND_SYNONYMS.clear()
        KIND_SYNONYMS.update(original)


def test_the_router_stops_accepting_a_word_the_gateway_drops(core):
    """The other direction, because a one-way sync is still two lists."""
    original = copy.deepcopy(KIND_SYNONYMS)
    try:
        KIND_SYNONYMS["light"].discard("bulb")
        plan = _parse(core, "turn on the bulb")
        assert not plan.actions, [a.tool for a in plan.actions]
    finally:
        KIND_SYNONYMS.clear()
        KIND_SYNONYMS.update(original)


def test_a_category_word_asks_and_names_the_plugs_rather_than_choosing_one(core):
    """The new reach must not become the old substitution: "power" describes four plugs.

    Before this phase the phrase was refused for the wrong reason (an unrecognised word);
    now it is recognised and *still* actuates nothing, which is the answer the gateway's
    own data supports.
    """
    response = _run(core, "Jarvis, turn on the power.")
    assert not response.actions, [a.tool for a in response.actions]
    assert "power" in response.reply.lower(), response.reply
    assert "Kitchen fridge power" in response.reply, response.reply
    for device in get_home_gateway().devices:
        assert device.on in (True, False), device.name


def test_no_device_changes_state_from_a_category_word(core):
    gw = get_home_gateway()
    before = {d.device_id: (d.on, d.level, d.speed, d.setpoint) for d in gw.devices}
    for word in ("power", "outlet", "bulb", "ceiling", "air", "cam"):
        _run(core, f"turn on the {word}")
    assert {d.device_id: (d.on, d.level, d.speed, d.setpoint) for d in gw.devices} == before


def test_a_word_that_names_only_a_sensor_does_not_pretend_to_switch_it(core):
    """"motion" and "energy" are sensor vocabulary. Recognising the word must not turn a
    read-only thing into a switchable one."""
    response = _run(core, "turn on the motion")
    if response.actions:
        assert response.decision is PolicyDecision.CONFIRM, response.reply
        approved = asyncio.run(core.resolve_confirmation(response.confirmation_id, True))
        claim = (approved.reply or "").lower()
        assert "sensor" in claim or "cannot" in claim or "which" in claim, approved.reply
    else:
        assert "sensor" in response.reply.lower() or "which" in response.reply.lower(), (
            response.reply)


def test_the_refusal_for_an_appliance_that_is_not_here_still_stands(core):
    """Phase 31's guard must survive a wider vocabulary: more words recognised is not
    more devices allowed."""
    response = _run(core, "Jarvis, turn on heater.")
    assert response.decision is PolicyDecision.BLOCK, response.reply
    assert not response.actions


def test_a_home_word_does_not_steal_a_sentence_another_route_owns(core):
    """Widening one route is only safe if the chain order still holds."""
    for text, tool in (("take a screenshot", "take_screenshot"),
                       ("what is my battery", "battery_status"),
                       ("open VS Code", None)):
        plan = _parse(core, text)
        if tool:
            assert [a.tool for a in plan.actions] == [tool], (text, plan.direct_reply)


# ---------------------------------------------------------------------------
# 2. the approval names the device that will change
# ---------------------------------------------------------------------------


def test_the_approval_names_the_device_rather_than_the_words_typed(core):
    response = _run(core, "turn on the living room fan", session_id="p32-naming")
    assert response.decision is PolicyDecision.CONFIRM, response.reply
    record = core.policy.get_confirmation(response.confirmation_id)
    assert "Living room fan" in record["preview"], record["preview"]
    assert "living.fan" in record["preview"], record["preview"]


def test_the_approval_says_what_will_happen_not_only_the_parameters(core):
    response = _run(core, "dim the kitchen light to 40", session_id="p32-dim")
    record = core.policy.get_confirmation(response.confirmation_id)
    preview = record["preview"]
    assert "Kitchen light" in preview, preview
    assert "40" in preview, preview
    assert "set" in preview.lower() or "dim" in preview.lower(), preview


def test_the_prompt_shown_and_the_record_shown_are_one_string(core):
    """The dashboard renders the stored preview and the chat used to re-derive its own
    line from the raw parameters. Two renderings of one consent question is how they
    start to disagree, and the drift is invisible to whoever reads either one."""
    response = _run(core, "turn on the study ac", session_id="p32-drift")
    record = core.policy.get_confirmation(response.confirmation_id)
    assert record["preview"] in response.reply, (response.reply, record["preview"])
    assert "Study AC" in response.reply, response.reply


def test_a_non_home_confirmation_still_shows_what_it_will_act_on(core, tmp_area, monkeypatch):
    """The preview is not only a home-control feature: a file deletion has to say which
    path, and that must not be lost while fixing the device wording."""
    from jarvis.config import get_settings

    monkeypatch.setattr(get_settings(), "file_workspace", str(tmp_area))
    victim = tmp_area / "p32-victim.txt"
    victim.write_text("deletable\n")
    response = _run(core, f"delete {victim}", session_id="p32-file")
    assert response.decision is PolicyDecision.CONFIRM, response.reply
    record = core.policy.get_confirmation(response.confirmation_id)
    assert "p32-victim.txt" in record["preview"], record["preview"]
    assert "p32-victim.txt" in response.reply, response.reply


def test_an_approval_is_still_phrased_as_a_question_not_a_result(core):
    response = _run(core, "turn on the kitchen light", session_id="p32-question")
    lower = response.reply.lower()
    assert "needs your confirmation" in lower or "confirm" in lower, response.reply
    assert "switched on" not in lower, response.reply
    assert not response.actions


def test_the_preview_resolves_to_the_same_device_the_guard_allowed(core):
    """The guard and the preview must agree about which device a word means, or the
    thing approved is not the thing that will be sent to the gateway."""
    word = "study ac"
    target, described = get_home_gateway().actuation_target(word)
    assert target is not None, described
    response = _run(core, f"turn on the {word}", session_id="p32-agree")
    assert target.name in core.policy.get_confirmation(response.confirmation_id)["preview"]
