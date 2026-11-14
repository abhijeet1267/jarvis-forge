"""Phase 52 -- the §6 sentences that are about the laptop.

The build prompt's §6 lists 37 utterances. Measured against the real registry before this
phase, 13 of them reached nothing. They split by mechanism, and the split matters because two
of the classes need different fixes:

* **the application registry did not know the app** -- Firefox, Contacts, Messages, System
  Settings. The capability (`open_application`) works; the name was missing.
* **the sentence existed and the rule's regex did not** -- "what date is it?" (the tool
  answers with the date) and "what is my hostname?" (`get_machine_snapshot` already returns a
  `hostname` field).
* **no capability at all** -- the four standard folders, and "what is on my screen right now".
  Those get tools, because a route to nothing is a lie waiting to happen.

The self-observation set (pending confirmations, Vantage status, recent actions, stop the
current task, "search Google for X") is deliberately not here -- that is the next phase, and
mixing a new command surface into a routing phase hides which change broke what.
"""

from __future__ import annotations

import asyncio
import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def registry():
    from jarvis.tools import build_default_registry

    return build_default_registry()


@pytest.fixture(scope="module")
def planner(registry):
    from jarvis.core.planner import RulePlanner

    return RulePlanner(registry)


def _tools(plan):
    return [a.tool for a in plan.actions]


def _params(plan, tool):
    return [a.params for a in plan.actions if a.tool == tool]


def _ctx():
    from jarvis.tools.base import ToolContext

    return ToolContext(session_id="p52")


# ---------------------------------------------------------------------------
# 1. four more applications are known, by the name the OS uses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key,display", [
    ("firefox", "Firefox"),
    ("contacts", "Contacts"),
    ("messages", "Messages"),
    ("system_settings", "System Settings"),
])
def test_the_application_is_registered(key, display):
    from jarvis.registry.apps import AppRegistry

    spec = AppRegistry().get(key)
    assert spec is not None, f"{key!r} is not a registered application"
    assert spec.display_name == display
    assert spec.launch.get("macos") == display, (
        f"{key} must launch under the name macOS gives it, not a guessed one")


@pytest.mark.parametrize("utterance,key", [
    ("open Firefox", "firefox"),
    ("open Contacts", "contacts"),
    ("open Messages", "messages"),
    ("open System Settings", "system_settings"),
])
def test_the_spec_sentence_opens_the_application(planner, utterance, key):
    plan = planner.parse(utterance)
    assert "open_application" in _tools(plan), (
        f"{utterance!r} -> {_tools(plan)} kind={plan.reply_kind!r}")
    assert _params(plan, "open_application")[0]["app"] == key


def test_the_old_names_for_the_settings_app_still_work(planner):
    """macOS renamed System Preferences in 13; a person who learned the old word keeps it."""
    for utterance in ("open System Preferences", "open the settings app"):
        plan = planner.parse(utterance)
        assert _params(plan, "open_application"), f"{utterance!r} -> {_tools(plan)}"
        assert _params(plan, "open_application")[0]["app"] == "system_settings"


# ---------------------------------------------------------------------------
# 2. two sentences whose answer already existed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("utterance", ["what date is it?", "what is today's date?",
                                       "what's the date"])
def test_asking_the_date_reaches_the_tool_that_reports_it(planner, utterance):
    assert _tools(planner.parse(utterance)) == ["get_time"], utterance


def test_a_date_asking_about_something_else_is_not_stolen_by_the_clock(planner):
    """"what is the date of my meeting" is a calendar question, not a clock question."""
    plan = planner.parse("what is the date of my meeting tomorrow")
    assert "get_time" not in _tools(plan), _tools(plan)


def test_the_time_tool_actually_says_the_date(registry):
    """Routing to a tool is only a fix if the tool answers the question asked."""
    result = asyncio.run(registry.handler("get_time")(_ctx()))
    today = datetime.date.today()
    assert str(today.year) in result.message, result.message
    assert today.strftime("%d") in result.message or today.strftime("%-d") in result.message


@pytest.mark.parametrize("utterance", ["what is my hostname?", "what is my host name?",
                                       "what is my computer called?"])
def test_asking_the_hostname_reaches_the_snapshot_that_holds_it(planner, utterance):
    assert "get_machine_snapshot" in _tools(planner.parse(utterance)), utterance


def test_the_snapshot_answers_with_the_real_hostname(registry):
    import socket

    result = asyncio.run(registry.handler("get_machine_snapshot")(_ctx()))
    assert socket.gethostname().split(".")[0] in result.message, result.message


# ---------------------------------------------------------------------------
# 3. the standard folders, through an allowlist rather than a path
# ---------------------------------------------------------------------------


STANDARD = {"desktop": "Desktop", "downloads": "Downloads", "documents": "Documents",
            "pictures": "Pictures", "home": "Home"}


@pytest.mark.parametrize("utterance,folder", [
    ("open Desktop", "desktop"),
    ("open Downloads", "downloads"),
    ("open Documents", "documents"),
    ("open Pictures", "pictures"),
    ("open my home folder", "home"),
])
def test_a_standard_folder_is_reachable(planner, utterance, folder):
    plan = planner.parse(utterance)
    assert "open_user_folder" in _tools(plan), (
        f"{utterance!r} -> {_tools(plan)} kind={plan.reply_kind!r} -- §6 lists this sentence")
    assert _params(plan, "open_user_folder")[0]["folder"] == folder


def test_a_standard_folder_is_not_routed_to_the_editor(planner):
    """"open Downloads" and "open my project" are different capabilities; Phase 49a said so."""
    plan = planner.parse("open Downloads")
    assert "open_project_folder" not in _tools(plan)


def test_the_allowlist_is_the_only_way_in(registry):
    """The parameter names a folder; it is never a path the caller wrote."""
    ctx = _ctx()
    for bad in ("../../.ssh", "/etc", "~/Library/Mail", "Desktop/../../Documents",
                "passwords", ""):
        result = asyncio.run(registry.handler("open_user_folder")(ctx, folder=bad))
        assert result.ok is False, f"{bad!r} was accepted by an allowlist"
        assert "desktop" in result.error.lower(), (
            f"the refusal should name what is allowed, not just refuse: {result.error}")


def test_a_refused_folder_launches_nothing(registry, monkeypatch):
    from jarvis.tools import apps as apps_module

    seen = []

    async def fake_run(argv, **kw):
        seen.append(list(argv))
        return 0, "", ""

    monkeypatch.setattr(apps_module, "run_command", fake_run)
    asyncio.run(registry.handler("open_user_folder")(_ctx(), folder="~/.ssh"))
    assert seen == [], f"a refused folder still ran {seen}"


def test_the_named_folder_is_resolved_under_the_home(registry, monkeypatch):
    from jarvis.tools import apps as apps_module

    seen = []

    async def fake_run(argv, **kw):
        seen.append(list(argv))
        if argv and argv[0] == "osascript":
            return 0, "Downloads\n", ""
        return 0, "", ""

    monkeypatch.setattr(apps_module, "run_command", fake_run)
    asyncio.run(registry.handler("open_user_folder")(_ctx(), folder="downloads"))
    opened = [a for a in seen if a and a[0] == "open"]
    assert opened, seen
    assert opened[0][-1] == str(Path.home() / "Downloads"), opened[0]


def test_a_folder_is_only_reported_open_when_finder_says_so(registry, monkeypatch):
    from jarvis.core.models import Verification
    from jarvis.tools import apps as apps_module

    async def fake_run(argv, **kw):
        if argv and argv[0] == "osascript":
            return 0, "Downloads\n", ""
        return 0, "", ""

    monkeypatch.setattr(apps_module, "run_command", fake_run)
    result = asyncio.run(registry.handler("open_user_folder")(_ctx(), folder="downloads"))
    assert result.verification is Verification.SUCCESS, result.message
    assert "Downloads" in result.message


def test_a_window_finder_will_not_about_is_said_so(registry, monkeypatch):
    """Without Automation permission the folder may well have opened and nothing can confirm
    it. That is UNKNOWN, and the message has to name the permission rather than guess."""
    from jarvis.core.models import Verification
    from jarvis.tools import apps as apps_module

    async def fake_run(argv, **kw):
        if argv and argv[0] == "osascript":
            return 1, "", "osascript.exe: execution error: Not authorised (err -1743)"
        return 0, "", ""

    monkeypatch.setattr(apps_module, "run_command", fake_run)
    result = asyncio.run(registry.handler("open_user_folder")(_ctx(), folder="downloads"))
    assert result.verification is Verification.UNKNOWN, result.message
    assert "Automation" in result.message or "permission" in result.message.lower()
    assert "is open" not in result.message


# ---------------------------------------------------------------------------
# 4. "show the current application"
# ---------------------------------------------------------------------------


def test_the_sentence_reaches_a_tool(planner):
    plan = planner.parse("show the current application")
    assert "get_frontmost_application" in _tools(plan), (
        f"-> {_tools(plan)} kind={plan.reply_kind!r}")


def test_the_answer_names_the_application_that_is_frontmost(registry, monkeypatch):
    from jarvis.tools import apps as apps_module

    async def fake_run(argv, **kw):
        return 0, "Safari\n", ""

    monkeypatch.setattr(apps_module, "run_command", fake_run)
    result = asyncio.run(registry.handler("get_frontmost_application")(_ctx()))
    assert result.ok is True, result.error
    assert "Safari" in result.message


def test_a_refused_observation_names_accessibility_and_invents_nothing(registry, monkeypatch):
    from jarvis.tools import apps as apps_module

    async def fake_run(argv, **kw):
        return 1, "", "osascript: execution error: Not authorised to send Apple events (err -1743)"

    monkeypatch.setattr(apps_module, "run_command", fake_run)
    result = asyncio.run(registry.handler("get_frontmost_application")(_ctx()))
    assert "Accessibility" in (result.error or result.message), result
    assert "is not authorised" not in (result.message or "")
    assert result.verification.value != "success"


# ---------------------------------------------------------------------------
# 5. shape of the new surface, and what must not have moved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,risk,confirmation", [
    ("open_user_folder", 1, False),
    ("get_frontmost_application", 0, False),
])
def test_the_new_tools_declare_their_own_gate(registry, name, risk, confirmation):
    """A new capability must arrive with its gate set, not inherit a default."""
    spec = registry.get(name)
    assert spec is not None, f"{name} is not registered"
    assert int(spec.risk) == risk, f"{name} risk drifted"
    assert spec.requires_confirmation is confirmation


def test_the_two_capabilities_this_phase_added_are_there(registry):
    """Was `assert len(...) == 136`, a frozen total.

    Phase 53 added five more capabilities and the assertion failed for no defect at all --
    the same trap that turned Phase 49's `== 134` into an obstacle. What is worth guarding is
    that this phase's two tools exist and that the number the README publishes is the number
    the registry reports, because a stale published count is the defect this project keeps
    finding.
    """
    import re

    for name in ("open_user_folder", "get_frontmost_application"):
        assert registry.get(name) is not None, f"{name} is gone"
    readme = (REPO / "README.md").read_text()
    # The README's count column is padded, so the cell may be `| Tools registered   | **148**`
    # rather than tightly joined. Match the label and the bold number with flexible
    # whitespace between them; the invariant is that the published count equals the
    # live registry, which the formatting cannot change.
    m = re.search(r"Tools registered\s*\|\s*\*\*(\d+)\*\*", readme)
    assert m, "README no longer publishes a tool count"
    claimed = int(m.group(1))
    assert claimed == len(registry.names()), (
        f"the README publishes {claimed} tools; the registry has {len(registry.names())}")


@pytest.mark.parametrize("utterance,tool", [
    ("open Mail", "open_application"),
    ("open Calendar", "open_application"),
    ("open the calendar site", "open_url"),
    ("open my project", "open_project_folder"),
    ("open github.com", "open_url"),
    ("check my unread emails", "search_email"),
    ("what is my battery", "get_battery_status"),
    ("take a screenshot", "take_screenshot"),
    ("emergency stop", "emergency_stop"),
])
def test_the_sentences_that_worked_before_still_work(planner, utterance, tool):
    assert tool in _tools(planner.parse(utterance)), f"{utterance!r} regressed"


# ---------------------------------------------------------------------------
# 6. three defects the live drive found and the suite had not
# ---------------------------------------------------------------------------


def test_the_installed_check_looks_where_macos_keeps_its_own_apps():
    """"Mail is not installed on this laptop" -- said with a straight face, by the live tool.

    Mail, Contacts and Calendar ship in ``/System/Applications``, which was not one of the
    roots the installed check searched, so the answer for half of §6's application list was a
    confident wrong fact. Measured on this machine before the fix.
    """
    from jarvis.tools.agents import BUNDLE_ROOTS

    assert "/System/Applications" in BUNDLE_ROOTS, BUNDLE_ROOTS


@pytest.mark.parametrize("app", ["Mail", "Contacts", "Calendar"])
def test_an_apple_application_reports_as_installed(registry, app):
    """Not a courtesy to the app: the sentence "is Mail installed" must not be answered wrong."""
    result = asyncio.run(registry.handler("check_agent_status")(_ctx(), app=app))
    assert result.output.get("found") is True, result.message


def test_an_app_that_is_really_absent_still_reports_absent(registry):
    result = asyncio.run(
        registry.handler("check_agent_status")(_ctx(), app="Flurbledink"))
    assert result.output.get("found") is False, result.message


def test_a_launch_that_was_never_seen_says_the_app_is_not_installed(registry, monkeypatch):
    """`open -a Firefox` exits 1 for an app that is not there, but the launch is deliberately
    detached -- so the exit code is never read and only the observation remains. An
    unobserved launch whose application is not installed is a failure with a reason, not a
    shrug. Firefox is genuinely absent on this machine; that is why this was found."""
    from jarvis.tools import apps as apps_module

    async def fake_run(argv, **kw):
        if argv and argv[0] == "ps":
            return 0, "", ""
        return 0, "", ""

    monkeypatch.setattr(apps_module, "run_command", fake_run)
    monkeypatch.setattr(apps_module, "bundle_installed", lambda target: False)
    result = asyncio.run(registry.handler("open_application")(_ctx(), app="Firefox"))
    assert result.ok is False, result.message
    assert "not installed" in (result.error or "").lower(), result.error


def test_an_unobserved_but_installed_launch_stays_unverified(registry, monkeypatch):
    """The two cases must not collapse into one message: "it is not here" and "it was told and
    I could not see it" are different answers to different questions."""
    from jarvis.core.models import Verification
    from jarvis.tools import apps as apps_module

    async def fake_run(argv, **kw):
        if argv and argv[0] == "ps":
            return 0, "", ""
        return 0, "", ""

    monkeypatch.setattr(apps_module, "run_command", fake_run)
    monkeypatch.setattr(apps_module, "bundle_installed", lambda target: True)
    result = asyncio.run(registry.handler("open_application")(_ctx(), app="Mail"))
    assert result.verification is Verification.UNKNOWN, result.message
    assert "not installed" not in (result.message or "").lower()


def test_an_application_query_cannot_hold_a_sentence_for_twenty_seconds(registry, monkeypatch):
    """Measured live: `open Downloads` took 20.25 s, because the Finder AppleScript inherits
    the 20 s default and the folder may have been opened the whole time. Phase 34's rule is
    that one sentence gets one deadline; a confirmation query that nobody answered is not a
    reason to make the user wait."""
    from jarvis.tools import apps as apps_module

    timeouts: list = []

    async def fake_run(argv, **kw):
        if argv and argv[0] == "osascript":
            timeouts.append(kw.get("timeout"))
            return 124, "", "command timed out after 4.0s"
        return 0, "", ""

    monkeypatch.setattr(apps_module, "run_command", fake_run)
    result = asyncio.run(registry.handler("open_user_folder")(_ctx(), folder="downloads"))
    assert timeouts, "the folder was opened without ever asking what Finder shows"
    assert all(t is not None and t <= 5.0 for t in timeouts), (
        f"an unanswered AppleScript would be waited on for {timeouts}")
    assert result.verification.value == "unknown", result.message
    assert "is open" not in result.message
