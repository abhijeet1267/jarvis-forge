"""Phase 61: desktop control that asks, and only then touches.

Measured first. The registry already had the *eyes*: `capture_screen`, `take_screenshot`,
`get_frontmost_application`, `get_running_applications`, `vision_capabilities` and the OCR tools.
It had no hands -- `grep` for a click, a keystroke or a mouse event across `jarvis/tools` and
`jarvis/agents` returns only comments saying there is none ("There is no action tool in this
module: nothing here clicks, types, moves or delete[s]"). `jarvis/permissions/service.py` had
already named the missing thing's permission: `accessibility`, `required_for="Advanced desktop
control: reading windows, clicking, typing"`, and on this machine its measured state is
`REQUIRES_MANUAL_SYSTEM_SETTINGS`.

So the honest scope is the hands, and the spec's own rule decides their shape: **§26.2 — untrusted
model output is data, never authority.** A feature that lets a model decide where the cursor goes
has handed the laptop to whatever text was last on the screen. Every test below is some form of
that sentence.

And what is *not* claimed anywhere here: **no click and no keystroke has ever been executed by this
code on a real desktop.** Proving that would move the user's cursor inside their own applications,
which is their decision to grant (Accessibility) and their decision to ask for. The live run in the
phase report exercises the refusal, which is the part that can be measured without touching anyone.
"""

from __future__ import annotations

import asyncio

import pytest

from jarvis.core.models import RiskLevel
from jarvis.tools import desktop
from jarvis.tools.base import ToolContext


class _Sent:
    """The seam where an event would leave the process. Records, and answers however told."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def __call__(self, script, args, timeout=8.0):
        self.calls.append({"script": script, "args": list(args)})
        for needle, reply in self.answers.items():
            if needle in script:
                return reply
        return 0, ""


def _grant(monkeypatch, status="GRANTED"):
    monkeypatch.setattr(desktop, "_accessibility",
                        lambda ctx: {"status": status}, raising=False)


def _click(sent, x=120, y=240):
    return asyncio.run(desktop.click_desktop(ToolContext(), x=x, y=y))


# ---------------------------------------------------------------------------
# 1. the gate is the tier, and the tier is not negotiable
# ---------------------------------------------------------------------------


def test_both_actuations_are_destructive_and_ask_first():
    specs = {spec.name: spec for spec, _ in desktop.SPECS}
    for name in ("click_desktop", "type_desktop_text"):
        assert specs[name].risk == RiskLevel.DESTRUCTIVE, name
        assert specs[name].requires_confirmation is True, name
        assert specs[name].reversible is False, name


def test_neither_actuation_teaches_the_planner_its_own_sentence():
    """`examples` are how a router learns what a tool is for. A click is the one capability in
    this product that must not become reachable because a sentence happened to look like one of
    its sample utterances -- so the field is left empty and a test keeps it empty."""
    specs = {spec.name: spec for spec, _ in desktop.SPECS}
    for name in ("click_desktop", "type_desktop_text"):
        assert not getattr(specs[name], "examples", None), name


def test_the_registry_invariant_still_holds_with_hands_added(core):
    """The measured rule this project has kept since Phase 22: every DESTRUCTIVE tool is
    confirmation-gated. Adding the most irreversible tools in the product may not be the turn
    that rule gets bent to fit."""
    gated = {spec.name for spec in core.tools.all() if spec.requires_confirmation}
    destructive = [spec.name for spec in core.tools.all() if spec.risk == RiskLevel.DESTRUCTIVE]
    assert {"click_desktop", "type_desktop_text"} <= set(destructive)
    assert set(destructive) <= gated, sorted(set(destructive) - gated)


def test_a_sentence_cannot_put_a_click_in_the_plan(core):
    """§26.2 executed as a test: text is data. Whatever the planner makes of the words, no action
    is emitted for a coordinate a person never approved."""
    import json

    from jarvis.core.models import CommandRequest

    response = asyncio.run(core.handle(CommandRequest(
        text="click at 100 200", device_id="laptop_main", source="phase61")))
    tools_run = [action.tool for action in response.actions]
    assert "click_desktop" not in tools_run, tools_run
    assert json.dumps(tools_run)  # the answer is still a normal answer, not a crash


# ---------------------------------------------------------------------------
# 2. the permission is checked before anything leaves the process
# ---------------------------------------------------------------------------


def test_an_ungranted_accessibility_sends_nothing(monkeypatch):
    sent = _Sent()
    monkeypatch.setattr(desktop, "_osascript", sent)
    _grant(monkeypatch, "REQUIRES_MANUAL_SYSTEM_SETTINGS")

    result = _click(sent)

    assert result.ok is False, result.error
    assert "did not click" in result.error, result.error
    assert "Accessibility" in result.error, "the refusal has to name the thing to change"
    assert sent.calls == [], "an ungated path was reached before the permission was consulted"


def test_an_unreadable_permission_sends_nothing_either(monkeypatch):
    """Not-determined is not granted. The failure mode this guards is the confident one: a
    helper that answers nothing, read as a yes."""
    sent = _Sent()
    monkeypatch.setattr(desktop, "_osascript", sent)
    _grant(monkeypatch, "NOT_DETERMINED")

    assert asyncio.run(desktop.type_desktop_text(ToolContext(), text="hello")).ok is False
    assert sent.calls == []


def test_typing_is_refused_before_the_keystroke_too(monkeypatch):
    sent = _Sent()
    monkeypatch.setattr(desktop, "_osascript", sent)
    _grant(monkeypatch, "DENIED")

    result = asyncio.run(desktop.type_desktop_text(ToolContext(), text="rm -rf"))
    assert result.ok is False
    assert "did not type" in result.error
    assert sent.calls == []


# ---------------------------------------------------------------------------
# 3. arguments are checked before they are trusted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("x,y", [(None, 5), (5, None), (-1, 5), (5, -1),
                                 (999999, 5), ("eight", 5)])
def test_a_coordinate_outside_a_screen_never_reaches_the_os(monkeypatch, x, y):
    sent = _Sent()
    monkeypatch.setattr(desktop, "_osascript", sent)
    _grant(monkeypatch)

    result = asyncio.run(desktop.click_desktop(ToolContext(), x=x, y=y))
    assert result.ok is False, (x, y)
    assert sent.calls == [], (x, y)


def test_an_empty_or_overlong_text_is_refused(monkeypatch):
    sent = _Sent()
    monkeypatch.setattr(desktop, "_osascript", sent)
    _grant(monkeypatch)

    assert asyncio.run(desktop.type_desktop_text(ToolContext(), text="")).ok is False
    assert asyncio.run(desktop.type_desktop_text(
        ToolContext(), text="a" * (desktop.MAX_TEXT_CHARS + 1))).ok is False
    assert sent.calls == []


# ---------------------------------------------------------------------------
# 4. what a script may not contain
# ---------------------------------------------------------------------------


def test_the_payload_is_an_argument_and_never_part_of_the_script(monkeypatch):
    """AppleScript concatenation is the shell-injection of this surface: a payload spliced into
    the source can carry `" & do shell script "…`. The scripts here take `on run argv` and read
    their values from arguments, which cannot change the program being run."""
    sent = _Sent({})
    monkeypatch.setattr(desktop, "_osascript", sent)
    _grant(monkeypatch)
    payload = '" & do shell script "rm -rf ~'

    asyncio.run(desktop.type_desktop_text(ToolContext(), text=payload))

    assert sent.calls, "the keystroke was never attempted, so the test proved nothing"
    keystroke = [c for c in sent.calls if "keystroke" in c["script"]]
    assert keystroke, sent.calls
    call = keystroke[0]
    assert payload not in call["script"], "the payload was spliced into the script source"
    assert payload in call["args"], "the payload should travel as an argument"
    assert "do shell script" not in call["script"]

    # Negative control, so the assertion above is known to be able to fail: the obvious way to
    # write this call -- interpolating the text -- puts the payload in the program itself.
    naive = 'tell application "System Events" to keystroke "%s"' % payload
    assert payload in naive, "the control must actually be the unsafe form, or it proves nothing"


# ---------------------------------------------------------------------------
# 5. success is only what a re-read observed
# ---------------------------------------------------------------------------


def test_a_click_reports_the_application_it_can_see_afterwards(monkeypatch):
    sent = _Sent({"frontmost": (0, "Notes"), "click": (0, "")})
    monkeypatch.setattr(desktop, "_osascript", sent)
    _grant(monkeypatch)

    result = _click(sent)

    assert result.ok is True, result.error
    assert result.output["focused"] == "Notes"
    assert "Notes is the application in front now" in result.message


def test_a_click_that_cannot_be_observed_is_reported_unverified(monkeypatch):
    """The event was sent and the read that would confirm it failed. "Done" would be the
    fake-success message §26.1 names as a threat, so the answer carries the uncertainty."""
    from jarvis.core.models import Verification

    sent = _Sent({"click": (0, ""), "frontmost": (1, "System Events error: -1719")})
    monkeypatch.setattr(desktop, "_osascript", sent)
    _grant(monkeypatch)

    result = _click(sent)

    assert result.verification is Verification.UNKNOWN, result.verification
    assert result.ok is False or result.verified is False, result
    assert "unverified" in result.message.lower()


def test_macos_refusing_the_event_is_a_failure_with_its_own_words(monkeypatch):
    sent = _Sent({"click": (1, "Not authorized to send keyboard events (-25211)"),
                  "frontmost": (0, "Notes")})
    monkeypatch.setattr(desktop, "_osascript", sent)
    _grant(monkeypatch)

    result = _click(sent)

    assert result.ok is False
    assert "-25211" in result.error, "the OS said why; the user gets told why"


def test_a_keystroke_that_cannot_be_read_back_is_never_claimed_as_typed(monkeypatch):
    """Typing has no observable effect this code can reach: the characters went into a field
    Vantage cannot see into. The honest answer is unverified even when the send succeeded."""
    from jarvis.core.models import Verification

    sent = _Sent({"keystroke": (0, ""), "frontmost": (1, "")})
    monkeypatch.setattr(desktop, "_osascript", sent)
    _grant(monkeypatch)

    result = asyncio.run(desktop.type_desktop_text(ToolContext(), text="hello"))

    assert result.verification is Verification.UNKNOWN
    assert "unverified" in result.message.lower()


# ---------------------------------------------------------------------------
# 6. the read half, which is what a person asks before trusting any of this
# ---------------------------------------------------------------------------


def test_the_state_read_names_the_permission_and_the_target(monkeypatch):
    sent = _Sent({"frontmost": (0, "TextEdit")})
    monkeypatch.setattr(desktop, "_osascript", sent)
    _grant(monkeypatch)

    result = asyncio.run(desktop.get_desktop_control_state(ToolContext()))

    assert result.ok is True
    assert result.output["can_control"] is True
    assert result.output["focused"] == "TextEdit"


def test_the_state_read_refuses_to_say_ready_when_the_read_failed(monkeypatch):
    from jarvis.core.models import Verification

    sent = _Sent({"frontmost": (1, "")})
    monkeypatch.setattr(desktop, "_osascript", sent)
    _grant(monkeypatch)

    result = asyncio.run(desktop.get_desktop_control_state(ToolContext()))

    assert result.verification is Verification.UNKNOWN
    assert "could not be read" in result.message


def test_the_state_read_works_without_touching_anything_when_ungranted(monkeypatch):
    sent = _Sent()
    monkeypatch.setattr(desktop, "_osascript", sent)
    _grant(monkeypatch, "REQUIRES_MANUAL_SYSTEM_SETTINGS")

    result = asyncio.run(desktop.get_desktop_control_state(ToolContext()))

    assert result.ok is True, result.error
    assert result.output["can_control"] is False
    assert "Accessibility" in result.message
    assert sent.calls == [], "reading the state must not send an event"


def test_the_three_tools_are_registered(core):
    names = set(core.tools.names())
    assert {"get_desktop_control_state", "click_desktop", "type_desktop_text"} <= names

def test_the_question_about_reachability_reaches_the_read(core):
    """Phase 28's rule in a new place: a capability that exists must be askable about.

    "Can you control my computer" is a question about a permission state, and the planner sends
    it to the read -- never to a click, which is the only route this phase had to be careful not
    to open.
    """
    for sentence in ("can you control my computer", "can you click on my screen",
                     "is desktop control available", "do you have accessibility permission"):
        plan = core.planner.parse(sentence)
        tools = [action.tool for action in plan.actions]
        assert tools == ["get_desktop_control_state"], (sentence, tools)


def test_no_wording_of_a_sentence_routes_an_actuation(core):
    """The route table is swept, not trusted. If any of these ever resolve to a click or a
    keystroke, the gate has been moved from the confirmation card into the parser."""
    for sentence in ("click at 100 200", "click my save button", "type hello world",
                     "press the button at 5 5", "click on desktop", "type into the screen"):
        tools = [action.tool for action in core.planner.parse(sentence).actions]
        assert "click_desktop" not in tools and "type_desktop_text" not in tools, (sentence, tools)

