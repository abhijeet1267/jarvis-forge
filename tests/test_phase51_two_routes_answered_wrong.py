"""Phase 51 -- the two sentences that were answered by the wrong capability.

Measured before anything was written, against the real registry (134 tools):

    open Mail           -> declined: "I can search your mail, read a message, ..."
    open Calendar       -> open_url https://calendar.google.com
    open Google Calendar-> open_url https://www.google.com
    open Mail.app       -> open_url https://Mail.app

None of those four is a gap in capability. `open_application` exists, Mail.app and
Calendar.app are installed, and the app registry is the thing that knows them. Each one is a
*route* defect: a more specific rule (mail search, the known-sites table, a domain-shaped
token) matched first and answered a different question than the one was asked.

Two of them are worse than "not helpful". `open Calendar` putting a web login page on screen
is a browser pointed at an account the user did not ask to open, and `open Mail.app` building
`https://Mail.app` is the planner inventing a domain -- which this repository's own browser
module says never to do, because somebody may have registered that name.

So the rules are: a name that is an installed application means the application unless the
sentence explicitly asks for the web; the longest known site name wins so that a brand-qualified
name is not truncated to its vendor; `.app` is an application and not a TLD when the part before
it resolves; and a launch is only "open" once the process has been seen -- an exit code from
`open -a` proves a request was made, not that an app appeared.
"""

from __future__ import annotations

import asyncio
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


# ---------------------------------------------------------------------------
# 1. the two applications exist in the registry that both the planner and the tool read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key,display", [("mail", "Mail"), ("calendar", "Calendar")])
def test_the_app_registry_knows_the_application(key, display):
    from jarvis.registry.apps import AppRegistry

    spec = AppRegistry().get(key)
    assert spec is not None, f"{key!r} is not a registered application"
    assert spec.display_name == display


@pytest.mark.parametrize("phrase,key", [
    ("Mail", "mail"),
    ("mail", "mail"),
    ("Calendar", "calendar"),
    ("calendar", "calendar"),
])
def test_a_spoken_name_resolves_to_it(phrase, key):
    from jarvis.registry.apps import AppRegistry

    got = AppRegistry().resolve(phrase, platform="macos")
    assert got is not None and got.key == key, f"{phrase!r} resolved to {got}"


@pytest.mark.parametrize("key", ["mail", "calendar"])
def test_it_declares_how_to_launch_on_macos(key):
    """A registered app with no launch target is a route that can only fail."""
    from jarvis.registry.apps import AppRegistry

    spec = AppRegistry().get(key)
    assert spec.launch.get("macos"), f"{key} has no macOS launch name"


# ---------------------------------------------------------------------------
# 2. "open Mail" means the application, and the mail capability is still reachable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("utterance", [
    "open Mail",
    "open mail",
    "open the mail app",
    "launch Mail",
    "open Mail.app",
])
def test_the_mail_app_is_reachable_by_saying_so(planner, utterance):
    plan = planner.parse(utterance)
    assert "open_application" in _tools(plan), (
        f"{utterance!r} routed to {_tools(plan)} with reply_kind={plan.reply_kind!r} -- "
        "the capability exists, so refusing it is not honesty about a gap"
    )
    assert _params(plan, "open_application")[0]["app"] == "mail"


@pytest.mark.parametrize("utterance", ["open Mail", "open the mail app", "open Mail.app"])
def test_it_is_not_declined_as_an_unsupported_request(planner, utterance):
    plan = planner.parse(utterance)
    assert plan.reply_kind != "declined", (
        f"{utterance!r} was declined while an application tool that can do it is registered"
    )


def test_the_mail_app_is_not_opened_in_a_browser(planner):
    """The whole point: a login page on screen is not Mail, and it is not what was asked."""
    for utterance in ("open Mail", "open mail"):
        plan = planner.parse(utterance)
        assert "open_url" not in _tools(plan), f"{utterance!r} opened a browser"


@pytest.mark.parametrize("utterance,tool", [
    ("check my unread emails", "search_email"),
    ("draft an email to someone@example.com", "draft_email"),
    ("summarise recent emails", "summarize_email"),
])
def test_the_mail_capabilities_the_decline_advertises_still_work(planner, utterance, tool):
    """The decline text promised these; the fix must not take them away by matching earlier."""
    assert tool in _tools(planner.parse(utterance)), f"{utterance!r} lost {tool}"


@pytest.mark.parametrize("utterance,tool", [
    ("what is on my calendar tomorrow", "list_events"),
])
def test_asking_about_the_calendar_data_is_not_asking_to_open_the_app(planner, utterance, tool):
    """A sentence with a question in it must not be captured by the launch route."""
    plan = planner.parse(utterance)
    assert tool in _tools(plan), f"{utterance!r} -> {_tools(plan)}"
    assert "open_application" not in _tools(plan)


# ---------------------------------------------------------------------------
# 3. "open Calendar" is an application; the web is only for a sentence that says web
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("utterance", [
    "open Calendar",
    "open calendar",
    "open the calendar",
    "open the calendar app",
    "launch Calendar",
])
def test_the_calendar_app_is_reachable_by_saying_so(planner, utterance):
    plan = planner.parse(utterance)
    assert "open_application" in _tools(plan), (
        f"{utterance!r} routed to {_tools(plan)}; Calendar is installed, the site is not what "
        "was asked for"
    )
    assert _params(plan, "open_application")[0]["app"] == "calendar"


@pytest.mark.parametrize("utterance", ["open Calendar", "open calendar"])
def test_the_calendar_website_is_not_opened_instead(planner, utterance):
    plan = planner.parse(utterance)
    for params in _params(plan, "open_url"):
        raise AssertionError(f"{utterance!r} opened {params['url']} instead of the app")


@pytest.mark.parametrize("utterance,url", [
    ("open google calendar", "https://calendar.google.com"),
    ("open the calendar site", "https://calendar.google.com"),
    ("open calendar in the browser", "https://calendar.google.com"),
    ("open calendar website", "https://calendar.google.com"),
])
def test_a_sentence_that_asks_for_the_web_still_gets_it(planner, utterance, url):
    """The web calendar is real and some people mean it -- the words say which one."""
    plan = planner.parse(utterance)
    assert "open_url" in _tools(plan), f"{utterance!r} routed to {_tools(plan)}"
    assert _params(plan, "open_url")[0]["url"] == url


# ---------------------------------------------------------------------------
# 4. the longest known site name wins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("utterance,url", [
    ("open google calendar", "https://calendar.google.com"),
    ("open stack overflow", "https://stackoverflow.com"),
    ("open github", "https://github.com"),
    ("open youtube", "https://www.youtube.com"),
])
def test_a_multiword_site_name_is_not_truncated_to_its_vendor(planner, utterance, url):
    """`open Google Calendar` matched "google" first and returned https://www.google.com --
    the brand-qualified name lost its second half."""
    plan = planner.parse(utterance)
    got = _params(plan, "open_url")
    assert got and got[0]["url"] == url, f"{utterance!r} -> {got}"


# ---------------------------------------------------------------------------
# 5. .app is an application, never a guessed domain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("utterance,app", [
    ("open Mail.app", "mail"),
    ("open Calendar.app", "calendar"),
    ("open Notes.app", "notes"),
    ("open Safari.app", "safari"),
])
def test_a_bundle_suffix_names_an_application(planner, utterance, app):
    plan = planner.parse(utterance)
    assert "open_url" not in _tools(plan), (
        f"{utterance!r} built a URL from a bundle name -- that is an invented domain, and "
        "the browser module's own rule says a bare name opens nothing"
    )
    assert "open_application" in _tools(plan)
    assert _params(plan, "open_application")[0]["app"] == app


def test_an_unknown_dot_app_is_still_not_invented_as_a_site(planner):
    """No rule here may turn a name nobody knows into an address."""
    plan = planner.parse("open Flurbleapp.app")
    for params in _params(plan, "open_url"):
        assert "flurbleapp" not in params["url"].lower(), (
            f"a name with no owner was guessed into {params['url']}"
        )


# ---------------------------------------------------------------------------
# 6. a launch is verified by the process appearing, not by an exit code
# ---------------------------------------------------------------------------


def _stub_launch(monkeypatch, running: bool):
    """Answer as `open -a` does (exit 0) and control what the process table shows."""
    from jarvis.tools import apps as apps_module

    seen = []

    async def fake_run(argv, **kw):
        seen.append(list(argv))
        if argv[0] == "ps":
            return 0, ("/System/Applications/Mail.app/Contents/MacOS/Mail" if running else ""), ""
        return 0, "", ""

    monkeypatch.setattr(apps_module, "run_command", fake_run)
    return seen


def _call_open_app(monkeypatch, running: bool):
    from jarvis.core.models import PlannedAction
    from jarvis.tools import apps as apps_module
    from jarvis.tools.base import ToolContext

    seen = _stub_launch(monkeypatch, running)
    result = asyncio.run(apps_module.open_app(ToolContext(session_id="p51"), app="Mail"))
    return result, seen


def test_a_seen_process_is_reported_as_open(monkeypatch):
    result, seen = _call_open_app(monkeypatch, running=True)
    assert any(a[:3] == ["open", "-a", "Mail"] for a in seen), seen
    assert result.ok is True, result.error
    assert "Mail is open" in result.message


def test_an_unseen_process_is_not_reported_as_open(monkeypatch):
    """`open -a` exits 0 for a launch request, not for an application on screen."""
    from jarvis.core.models import Verification

    result, _seen = _call_open_app(monkeypatch, running=False)
    assert result.verification is not Verification.SUCCESS, (
        f"claimed {result.verification!r} without ever observing the process"
    )
    assert "could not" in result.message.lower() or "not confirm" in result.message.lower()
    assert "Mail is open." not in result.message


def test_a_helper_process_is_not_the_application(monkeypatch):
    """Found on the live machine, after the first version of the probe shipped.

    Calendar.app was closed and the process table still held::

        /System/Applications/Calendar.app/Contents/PlugIns/CalendarWidgetExtension.appex/...

    A probe that greps for "/Calendar.app/" reports success for a widget the user never
    opened -- the exact claim this function exists to refuse to make.
    """
    from jarvis.core.models import Verification
    from jarvis.tools import apps as apps_module

    async def fake_run(argv, **kw):
        if argv[0] == "ps":
            return 0, ("/System/Applications/Calendar.app/Contents/PlugIns/"
                       "CalendarWidgetExtension.appex/Contents/MacOS/CalendarWidgetExtension"), ""
        return 0, "", ""

    monkeypatch.setattr(apps_module, "run_command", fake_run)
    from jarvis.tools.base import ToolContext

    result = asyncio.run(apps_module.open_app(ToolContext(session_id="p51"), app="Calendar"))
    assert result.verification is not Verification.SUCCESS, (
        f"a widget extension was reported as the application being open: {result.message!r}"
    )


def test_the_refusal_is_still_a_refusal(monkeypatch):
    from jarvis.tools import apps as apps_module
    from jarvis.tools.base import ToolContext

    _stub_launch(monkeypatch, running=True)
    result = asyncio.run(apps_module.open_app(ToolContext(session_id="p51"), app="Watson"))
    assert result.ok is False, "an unknown application must not be guessed into a launch"


# ---------------------------------------------------------------------------
# 7. nothing outside the two routes moved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("utterance,tool", [
    ("open Safari", "open_application"),
    ("open github.com", "open_url"),
    ("open the google site", "open_url"),
    ("open my project", "open_project_folder"),
])
def test_the_sentences_that_already_worked_still_work(planner, utterance, tool):
    assert tool in _tools(planner.parse(utterance)), f"{utterance!r} regressed"


def test_the_risk_tier_of_the_application_tool_is_unchanged(registry):
    """Routing more sentences at a tool must not quietly change what it is allowed to do."""
    spec = registry.get("open_application")
    assert int(spec.risk) == 1, "open_application left LOW risk"
    assert spec.requires_confirmation is False


# ---------------------------------------------------------------------------
# 8. the button labelled "Open a site" cannot open an application
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("page", ["phone.html", "index.html"])
def test_a_button_promising_a_site_says_so_to_the_router(planner, page):
    """The slot prompt asks for a site; if the person answers "calendar", the sentence the
    button types must still mean the web page. Once the word alone meant the application, a
    button that silently changed what it opens is the defect this phase exists to avoid."""
    import re

    html = (REPO / "dashboard" / page).read_text(encoding="utf-8")
    # The page declares quick actions as a phrase table -- `["site", "open_url",
    # "open the {site} site"]` -- not a labelled object, so pull the `say` out of the
    # entry whose tool is open_url. The invariant is unchanged: that sentence must name
    # a medium, so "calendar" types a URL and never an application.
    m = re.search(r'\[\s*"[^"]+"\s*,\s*"open_url"\s*,\s*"([^"]+)"\s*\]', html)
    assert m, f"{page} has no open_url quick action to inspect"
    say = m.group(1)
    assert "site" in say or "website" in say, f"{page} types {say!r}, which names no medium"
    for answer in ("calendar", "google", "github.com"):
        typed = say.replace("{site}", answer)
        plan = planner.parse(typed)
        assert "open_url" in _tools(plan), f"{page} typed {typed!r} -> {_tools(plan)}"
        assert "open_application" not in _tools(plan)
