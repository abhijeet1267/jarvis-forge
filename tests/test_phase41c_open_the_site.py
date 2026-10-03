"""Phase 41c/41d: "open the github site", "find my file and open it".

Two sentences from the same list of eight. Measured before any code was written:

    open github.com             -> read_file  path=<home>/github.com
    open the github site in chrome -> open_app(chrome)   the site is dropped
    find my resume and open it  -> not_understood

The first one is the interesting defect: a domain was taken for a filename, so the system
built a path inside the user's home directory out of a hostname and offered to read it. That
is not a missing feature, it is a wrong one -- and it was already reachable, already
"understood", and already acting.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from jarvis.core.models import RiskLevel


def _browser():
    from jarvis.tools import browser

    return browser


def _ctx():
    from jarvis.tools.base import ToolContext

    return ToolContext(device_id="laptop_main", session_id="p41cd")


@pytest.fixture()
def launched(monkeypatch, tmp_path):
    """A machine with Chrome on disk, a captured launch, and a browser that answers."""
    browser_module = _browser()
    seen = {"argv": [], "report": "https://github.com"}

    fake_apps = tmp_path / "Applications"
    (fake_apps / "Google Chrome.app").mkdir(parents=True)
    from jarvis.tools import agents as agents_module

    monkeypatch.setattr(agents_module, "BUNDLE_ROOTS", (str(fake_apps),))

    async def fake_run(argv, **kw):
        seen["argv"].append(list(argv))
        if argv[0] == "osascript":
            return 0, seen["report"] + "\n", ""
        return 0, "", ""

    monkeypatch.setattr(browser_module, "run_command", fake_run)
    return seen


# ---------------------------------------------------------------------------
# 41c — a domain is not a filename
# ---------------------------------------------------------------------------


def test_a_domain_never_becomes_a_path_in_the_home_directory(core):
    plan = core.planner.parse("open github.com")
    tools = [a.tool for a in plan.actions]
    assert tools == ["open_url"], (
        f"a hostname is still being read as a file: {tools} {plan.actions and plan.actions[0].params}")
    assert "github.com" in plan.actions[0].params["url"]


def test_a_real_file_still_opens_as_a_file(core, tmp_path):
    """The domain rule must not swallow a path that exists."""
    document = tmp_path / "resume.pdf"
    document.write_text("x")
    plan = core.planner.parse(f"open {document}")
    assert [a.tool for a in plan.actions] == ["read_file"], plan.actions


def test_naming_the_browser_keeps_the_site_too(core):
    plan = core.planner.parse("open the github site in chrome")
    actions = [(a.tool, a.params) for a in plan.actions]
    assert actions and actions[0][0] == "open_url", actions
    assert "github.com" in actions[0][1]["url"], actions
    assert "chrome" in actions[0][1].get("browser", "").lower(), actions


def test_a_known_name_opens_the_real_address_and_reports_what_it_opened(core, launched):
    plan = core.planner.parse("open github.com")
    result = asyncio.run(_browser().open_url(_ctx(), **plan.actions[0].params))
    assert result.ok is True, result.error
    assert result.output["opened"] == "https://github.com", result.output
    assert "github.com" in result.message


def test_the_browser_is_asked_what_it_is_showing(core, launched):
    """`open` exiting 0 says a launch happened, not that the page is on screen."""
    result = asyncio.run(_browser().open_url(_ctx(), url="https://github.com/"))
    assert result.verification is not None
    assert result.output["shown_url"] == "https://github.com"


def test_a_browser_that_cannot_be_quietly_queried_says_so(core, launched, monkeypatch):
    async def denied(argv, **kw):
        if argv[0] == "osascript":
            return 1, "", "Not authorized to send Apple events to Google Chrome."
        return 0, "", ""

    monkeypatch.setattr(_browser(), "run_command", denied)
    result = asyncio.run(_browser().open_url(_ctx(), url="https://github.com/"))
    assert result.ok is True, "the site did open; refusing to confirm it is not a failure"
    assert not result.verified, "without the browser's own answer this is UNKNOWN"
    assert "could not confirm" in result.message.lower() or "not confirm" in result.message.lower()


def test_a_machine_with_no_browser_still_opens_and_says_it_could_not_confirm(tmp_path,
                                                                              monkeypatch):
    """`open <url>` hands the choice to LaunchServices, which leaves nothing to ask."""
    browser_module = _browser()
    from jarvis.tools import agents as agents_module

    monkeypatch.setattr(agents_module, "BUNDLE_ROOTS", (str(tmp_path / "nothing"),))
    argvs = []

    async def fake_run(argv, **kw):
        argvs.append(list(argv))
        return 0, "", ""

    monkeypatch.setattr(browser_module, "run_command", fake_run)
    result = asyncio.run(browser_module.open_url(_ctx(), url="https://github.com"))
    assert result.ok is True
    assert argvs == [["open", "https://github.com"]], argvs
    assert not result.verified


def test_the_unknown_name_is_a_question_not_a_guessed_domain():
    """The refusal has to be usable: it lists what it does know."""
    browser_module = _browser()
    _url, problem = browser_module.normalise("flurbledink")
    assert problem and not _url
    assert "guess" in problem.lower()


def test_the_page_query_carries_its_own_deadline(monkeypatch, tmp_path):
    """Measured live: `get URL of active tab of front window` blocked for over a minute.

    A refused Apple event or a browser with no front window does not answer, so the
    sentence must not wait on it -- the launch is detached, and the query that follows
    has to be bounded the same way every other network and process read in this build is.
    """
    browser_module = _browser()
    from jarvis.tools import agents as agents_module

    fake_apps = tmp_path / "Applications"
    (fake_apps / "Google Chrome.app").mkdir(parents=True)
    monkeypatch.setattr(agents_module, "BUNDLE_ROOTS", (str(fake_apps),))
    queries = []

    async def spy(argv, **kw):
        if argv[0] == "osascript":
            queries.append(kw.get("timeout"))
            return 124, "", f"command timed out after {kw.get('timeout')}s"
        return 0, "", ""

    monkeypatch.setattr(browser_module, "run_command", spy)
    result = asyncio.run(browser_module.open_url(_ctx(), url="https://github.com"))
    assert queries, "the browser was never asked"
    assert queries[0] and queries[0] <= 6, f"unbounded wait: timeout={queries[0]}"
    assert not result.verified


def test_the_reason_a_browser_gives_is_passed_on(core, launched, monkeypatch):
    """"It will not answer" is only honest if it says that macOS is the one refusing."""
    browser_module = _browser()

    async def denied(argv, **kw):
        if argv[0] == "osascript":
            return -600, "", ("Not authorized to send Apple events to Google Chrome. "
                              "(-1743)")
        return 0, "", ""

    monkeypatch.setattr(browser_module, "run_command", denied)
    result = asyncio.run(browser_module.open_url(_ctx(), url="https://github.com"))
    assert result.ok is True
    assert not result.verified
    assert "authorized" in result.message.lower() or "automation" in result.message.lower(), \
        result.message


def test_an_unknown_name_is_not_guessed_into_a_domain(core):
    plan = core.planner.parse("open the flurbledink site")
    if plan.actions:
        assert plan.actions[0].tool != "open_url", (
            f"a made-up name became https://flurbledink.com: {plan.actions[0].params}")


def test_open_url_is_its_own_tool_with_a_declared_tier(core):
    spec = core.tools.get("open_url")
    assert spec is not None
    assert spec.risk is RiskLevel.LOW
    assert spec.requires_confirmation is False


# ---------------------------------------------------------------------------
# 41d — find it and open it, in one breath
# ---------------------------------------------------------------------------


def test_search_and_open_is_understood(core):
    plan = core.planner.parse("find my resume and open it")
    assert [a.tool for a in plan.actions] == ["find_and_open"], plan.actions
    assert "resume" in plan.actions[0].params["query"]


def test_the_top_hit_is_the_one_handed_over(core, monkeypatch, tmp_path):
    from jarvis.tools import files as files_module

    wanted = tmp_path / "resume-2026.pdf"
    wanted.write_text("x")
    other = tmp_path / "resume-old.pdf"
    other.write_text("x")

    async def fake_search(ctx, query="", **kw):
        return files_module.ok("search_files", "found", device=ctx.device_id,
                               verified=True,
                               results=[{"path": str(wanted), "name": wanted.name},
                                        {"path": str(other), "name": other.name}])

    opened = {}

    async def fake_run(argv, **kw):
        opened["argv"] = list(argv)
        return 0, "", ""

    monkeypatch.setattr(files_module, "search_files", fake_search)
    monkeypatch.setattr(files_module, "run_command", fake_run)

    result = asyncio.run(files_module.find_and_open(_ctx(), query="resume"))
    assert result.ok is True, result.error
    assert opened["argv"][-1] == str(wanted), opened


def test_nothing_found_is_reported_as_nothing_found(core, monkeypatch):
    from jarvis.tools import files as files_module

    async def fake_search(ctx, query="", **kw):
        return files_module.ok("search_files", "nothing", device=ctx.device_id,
                               verified=True, results=[])

    monkeypatch.setattr(files_module, "search_files", fake_search)
    result = asyncio.run(files_module.find_and_open(_ctx(), query="definitely-not-here"))
    assert result.ok is False
    assert "nothing" in result.error.lower() or "no file" in result.error.lower()


def test_an_opened_document_is_not_claimed_as_verified_without_evidence(core, monkeypatch,
                                                                        tmp_path):
    """`open <file>` exiting 0 is a launch, not an observation of the document on screen."""
    from jarvis.tools import files as files_module

    document = tmp_path / "notes.txt"
    document.write_text("x")

    async def fake_search(ctx, query="", **kw):
        return files_module.ok("search_files", "found", device=ctx.device_id,
                               verified=True,
                               results=[{"path": str(document), "name": document.name}])

    async def fake_run(argv, **kw):
        return 0, "", ""

    monkeypatch.setattr(files_module, "search_files", fake_search)
    monkeypatch.setattr(files_module, "run_command", fake_run)
    result = asyncio.run(files_module.find_and_open(_ctx(), query="notes"))
    assert result.ok is True
    assert not result.verified, (
        "no channel here proves a text editor took the file; say UNKNOWN instead")
