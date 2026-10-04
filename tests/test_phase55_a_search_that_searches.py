"""Phase 55 -- a search that searches.

§6's last gap is ``search Google for FastAPI``. Measured before this phase, it reached nothing
(``not_understood``), and the tempting fix -- routing it at ``open_url`` with
``https://www.google.com`` -- is exactly the wrong answer, because it *looks* like a success:
a browser opens, a page loads, and nothing was searched. The operating prompt names that
failure mode twice, and this file exists to make it unrepresentable.

So the capability must:

* actually ask a search service and return what came back, or fail saying it could not;
* never invent a title or a URL -- an empty result is an empty result;
* treat an unreachable service as an outage, not as "no results" (the P1-5 rule this project
  already applies to mail and calendar);
* put the user's words into a query parameter, not into the URL's structure;
* and stay inside the sentence deadline (Phase 34) rather than inheriting a 20-second wait.

Risk tier, measured rather than guessed: **every one of the 13 EXTERNAL tools is
confirmation-gated**, so EXTERNAL here means "changes something outside". A search changes
nothing, and its closest precedent -- ``open_url``, which also reaches out and reads -- is LOW
and ungated. A search behind a confirmation per query would be a search nobody uses.
"""

from __future__ import annotations

import asyncio
import json

import pytest

TOOL = "search_web"


@pytest.fixture(scope="module")
def registry():
    from jarvis.tools import build_default_registry

    return build_default_registry()


@pytest.fixture(scope="module")
def planner(registry):
    from jarvis.core.planner import RulePlanner

    return RulePlanner(registry)


def _ctx():
    from jarvis.tools.base import ToolContext

    return ToolContext(session_id="p55")


def _run(coro):
    return asyncio.run(coro)


def _tools(plan):
    return [a.tool for a in plan.actions]


def _params(plan, tool):
    return [a.params for a in plan.actions if a.tool == tool]


# ---------------------------------------------------------------------------
# 1. the capability exists, and is gated like its closest precedent
# ---------------------------------------------------------------------------


def test_the_search_capability_is_registered(registry):
    assert registry.get(TOOL) is not None, "there is no web-search tool"


def test_a_search_that_changes_nothing_is_not_an_external_write(registry):
    """EXTERNAL means confirmation-gated in this registry (13 of 13). Putting a read there
    would make every search wait for approval, which is not a safety improvement."""
    spec = registry.get(TOOL)
    assert int(spec.risk) == 1, f"{TOOL} is not LOW tier"
    assert spec.requires_confirmation is False
    assert registry.get("open_url") is not None


# ---------------------------------------------------------------------------
# 2. routing -- the sentence means search, and never lands on a home page
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("utterance,query", [
    ("search Google for FastAPI", "FastAPI"),
    ("search google for fastapi", "fastapi"),
    ("search the web for python decorators", "python decorators"),
    ("search for python decorators", "python decorators"),
    ("jarvis, search Google for the heat death of the universe", "the heat death of the universe"),
])
def test_a_search_sentence_reaches_the_search_capability(planner, utterance, query):
    plan = planner.parse(utterance)
    assert TOOL in _tools(plan), (
        f"{utterance!r} -> {_tools(plan)} kind={plan.reply_kind!r}")
    assert _params(plan, TOOL)[0]["query"] == query


@pytest.mark.parametrize("utterance", [
    "search Google for FastAPI", "search the web for python decorators",
])
def test_a_search_is_never_answered_by_opening_a_home_page(planner, utterance):
    """The defect this phase exists to prevent: the browser opens, the page loads, nothing
    was searched, and the reply reads like a success."""
    plan = planner.parse(utterance)
    for params in _params(plan, "open_url"):
        assert "google.com" not in params.get("url", ""), (
            f"{utterance!r} was answered by navigating, not searching: {params}")
    assert "open_url" not in _tools(plan)


@pytest.mark.parametrize("utterance,tool", [
    ("open Google", "open_url"),
    ("open google.com", "open_url"),
    ("search files for my report", "search_files"),
    ("check my unread emails", "search_email"),
    ("what is a train", "ask_question"),
])
def test_search_does_not_steal_the_sentences_other_searches_own(planner, utterance, tool):
    """Four different capabilities already answer questions that use the word "search" or a
    lookup verb; a new route that swallows them is a regression dressed as a feature.

    "what is a train" is measured here rather than assumed: it reaches ``ask_question`` (the
    knowledge tool), not ``search_papers`` -- which is what the help text on an unrecognised
    sentence is pointing at when it offers that example.
    """
    assert tool in _tools(planner.parse(utterance)), (
        f"{utterance!r} -> {_tools(planner.parse(utterance))}")


# ---------------------------------------------------------------------------
# 3. the query is data, not URL structure
# ---------------------------------------------------------------------------


def _captured_request(monkeypatch, body: bytes):
    from jarvis import net

    seen = {}

    class _Resp:
        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(net.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_a_query_cannot_escape_its_own_parameter(registry, monkeypatch):
    """A query containing ``&`` or ``?`` must not be able to add, remove or redirect a
    parameter on the request the tool makes."""
    seen = _captured_request(monkeypatch, json.dumps({"AbstractText": "x"}).encode())
    _run(registry.handler(TOOL)(_ctx(), query="a&format=xml&redirect=https://evil.example"))
    from urllib.parse import parse_qs, urlsplit

    url = seen.get("url", "")
    parts = urlsplit(url)
    assert parts.scheme == "https" and parts.netloc == "api.duckduckgo.com", url
    query = parse_qs(parts.query)
    # The contract: the whole original text arrives as one value, and nothing else about the
    # request changed. The host appearing *inside* the value is correct -- it is data. What
    # would be a defect is it arriving as a second parameter, or as the host.
    assert query.get("q") == ["a&format=xml&redirect=https://evil.example"], query
    assert query.get("format") == ["json"], query
    assert len([k for k in query if k == "format"]) == 1, query


def test_a_query_with_spaces_and_quotes_is_encoded_not_concatenated(registry, monkeypatch):
    seen = _captured_request(monkeypatch, json.dumps({}).encode())
    _run(registry.handler(TOOL)(_ctx(), query='the "what" of it'))
    assert "%22" in seen["url"] or "+" in seen["url"].split("q=")[1], seen["url"]


def test_the_fetch_carries_a_bounded_deadline(registry, monkeypatch):
    """Phase 34: one sentence, one deadline. The default here used to be 20 s, and a search
    is not allowed to be the reason a sentence takes that long."""
    seen = _captured_request(monkeypatch, json.dumps({}).encode())
    _run(registry.handler(TOOL)(_ctx(), query="anything"))
    assert seen, "no request was attempted"
    assert seen.get("timeout") and seen["timeout"] <= 6.0, seen


# ---------------------------------------------------------------------------
# 4. results are what came back, or nothing
# ---------------------------------------------------------------------------


def test_results_are_only_what_the_source_returned(registry, monkeypatch):
    payload = {
        "AbstractText": "FastAPI is a modern web framework.",
        "AbstractURL": "https://example.org/fastapi",
        "Heading": "FastAPI",
        "RelatedTopics": [{"FirstURL": "https://example.org/a", "Text": "A topic"}],
    }
    _captured_request(monkeypatch, json.dumps(payload).encode())
    result = _run(registry.handler(TOOL)(_ctx(), query="FastAPI"))
    assert result.ok is True, result.error
    urls = {r["url"] for r in result.output["results"]}
    assert urls == {"https://example.org/fastapi", "https://example.org/a"}, urls
    assert result.output["count"] == 2


def test_an_empty_answer_is_reported_as_empty_not_as_invented_help(registry, monkeypatch):
    """The Instant Answer API genuinely has nothing for many queries. Returning a plausible
    sentence from nowhere is the fabrication this whole file is written against."""
    _captured_request(monkeypatch, json.dumps({"RelatedTopics": [], "AbstractText": ""}).encode())
    result = _run(registry.handler(TOOL)(_ctx(), query="xyzzy plugh frobnicate"))
    assert result.output.get("count") == 0, result.output
    assert result.output.get("results") == []
    assert "nothing" in result.message.lower() or "no result" in result.message.lower(), (
        result.message)


def test_a_malformed_response_fails_rather_than_becoming_an_empty_success(registry, monkeypatch):
    _captured_request(monkeypatch, b"<html>not json at all")
    result = _run(registry.handler(TOOL)(_ctx(), query="anything"))
    assert result.ok is False, result.message
    assert "could not read" in (result.error or "").lower() or "unexpected" in (result.error or "").lower(), (
        result.error)


def test_an_unreachable_service_is_an_outage_not_an_empty_day(registry, monkeypatch):
    """P1-5's rule, applied to search: silence and emptiness are different answers, and
    collapsing them is how a network failure becomes a confident "nothing found"."""
    from jarvis import net

    def boom(request, timeout=None):
        raise OSError("network unreachable")

    monkeypatch.setattr(net.urllib.request, "urlopen", boom)
    result = _run(registry.handler(TOOL)(_ctx(), query="anything"))
    assert result.ok is False, result.message
    error = (result.error or "").lower()
    assert "unreachable" in error or "could not reach" in error or "did not answer" in error, (
        result.error)
    assert "no result" not in error and "nothing" not in error, (
        f"an outage was reported as an empty answer: {result.error}")


def test_an_empty_query_is_asked_for_without_touching_the_network(registry, monkeypatch):
    seen = _captured_request(monkeypatch, b"{}")
    for bad in ("", "   ", "\n"):
        result = _run(registry.handler(TOOL)(_ctx(), query=bad))
        assert result.ok is False, f"{bad!r} was accepted"
    assert seen == {} or "url" not in seen, "an empty query still went out over the network"


def test_a_long_query_is_bounded_not_rejected_silently(registry, monkeypatch):
    _captured_request(monkeypatch, json.dumps({}).encode())
    result = _run(registry.handler(TOOL)(_ctx(), query="word " * 400))
    assert result.ok is False
    assert "long" in (result.error or "").lower(), result.error


# ---------------------------------------------------------------------------
# 5. the source is named, and named honestly
# ---------------------------------------------------------------------------


def test_the_answer_says_which_source_it_used(registry, monkeypatch):
    _captured_request(monkeypatch, json.dumps({"AbstractText": "x", "AbstractURL": "https://e.org"})
                      .encode())
    result = _run(registry.handler(TOOL)(_ctx(), query="anything"))
    assert result.output.get("source"), result.output
    assert "duckduckgo" in result.output["source"].lower(), result.output


def test_the_provider_is_describable_without_being_called(registry):
    """§7's rule: a capability the user cannot ask about is a capability they cannot trust."""
    from jarvis.websearch import describe

    d = describe()
    assert set(["name", "reachable", "requires_key"]) <= set(d), d
    assert d["requires_key"] is False


# ---------------------------------------------------------------------------
# 6. regressions
# ---------------------------------------------------------------------------


def test_no_existing_gate_was_touched(registry):
    specs = list(registry.all())
    # A frozen total was here ("== 28"), which is a weak guard with a sharp edge: it can be
    # satisfied by taking one gate away and adding two somewhere else, and it blocks a phase
    # that legitimately adds gated capabilities. What actually must not change is which *kind*
    # of capability needs a person. Measured: every EXTERNAL and DESTRUCTIVE tool is gated, and
    # the single ungated PHYSICAL one is the emergency stop, deliberately.
    tiers = {s.name: int(s.risk) for s in specs}
    loose = sorted(s.name for s in specs
                   if int(s.risk) in (2, 3) and not s.requires_confirmation)
    assert loose == [], f"tools that change something outside lost their approval: {loose}"
    physical_ungated = sorted(s.name for s in specs
                              if int(s.risk) == 4 and not s.requires_confirmation)
    assert physical_ungated == ["emergency_stop"], \
        "the emergency stop is the only ungated physical action, on purpose"
    assert int(registry.get("emergency_stop").risk) == 4


def test_the_registry_publishes_one_row_per_capability(registry):
    specs = registry.all()
    assert len(specs) == len({s.name for s in specs})
    published = {s.name for s in specs}
    for spec in specs:
        for alias in spec.aliases:
            assert alias not in published


@pytest.mark.parametrize("utterance,tool", [
    ("open Mail", "open_application"),
    ("open Downloads", "open_user_folder"),
    ("what date is it?", "get_time"),
    ("show the current application", "get_frontmost_application"),
    ("show pending confirmations", "get_pending_confirmations"),
    ("stop the current task", "stop_current_task"),
    ("emergency stop", "emergency_stop"),
    ("take a screenshot", "take_screenshot"),
])
def test_the_sentences_that_worked_before_still_work(planner, utterance, tool):
    assert tool in _tools(planner.parse(utterance)), f"{utterance!r} regressed"
