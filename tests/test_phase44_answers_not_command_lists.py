"""Phase 44: answers to questions, and a question that is not a shell command.

The ask was plain: "make it like ChatGPT, Gemini -- what I say they understand", plus
"when I ask questions it gives answers, like trains, common questions".

Measured first, on the rules this build actually has:

    what is a train                      -> not_understood
    who is apj kalam                     -> not_understood
    explain how wifi works               -> not_understood
    hello                                 -> not_understood
    thanks                                -> not_understood
    what trains run from mumbai to pune  -> run_allowlisted_command  {command: "from",
                                                                       args: "mumbai to pune"}
    how do I run a marathon              -> run_allowlisted_command  {command: "a",
                                                                       args: "marathon"}

That last pair is the one worth stopping on. `\\brun\\b` matches the word *anywhere*, so an
ordinary English question about trains was parsed as a request to execute a program called
`from`. The allowlist gate is what kept that harmless -- it did, and that is the only reason
it is a routing bug rather than an incident -- but a question must never reach an executor.

So this phase does three things: stops the misreading, answers greetings like one, and gives
the system a way to answer a factual question with no model and no key -- an encyclopedia
lookup, extractive, with the article named.
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest

from jarvis.core.models import CommandRequest, RiskLevel

WIKI_SUMMARY = {
    "title": "Train",
    "extract": "A train is a form of rail transport consisting of a series of vehicles that "
               "usually run along rails to a destination.",
    "content_urls": {"desktop": {"en": {"url": "https://en.wikipedia.org/wiki/Train"}}},
}


class _Response:
    def __init__(self, payload, status=200):
        self._body = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture()
def wiki(monkeypatch):
    """A Wikipedia-shaped answer, a record of what was asked, and a swappable responder."""
    import urllib.request

    seen = {"urls": [], "payload": WIKI_SUMMARY, "handler": None}

    def default(url):
        if seen["payload"] is None:
            raise OSError("network down")
        return _Response(seen["payload"])

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        seen["urls"].append(url)
        return (seen["handler"] or default)(url)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


# ---------------------------------------------------------------------------
# 1. a question is not a command
# ---------------------------------------------------------------------------


def test_a_question_containing_run_never_reaches_the_shell(core):
    for said in ("what trains run from mumbai to pune",
                 "how do I run a marathon",
                 "when do trains run in pune"):
        plan = core.planner.parse(said)
        tools = [a.tool for a in plan.actions]
        assert "run_allowlisted_command" not in tools, (said, plan.actions[0].params)


def test_an_imperative_run_still_reaches_the_shell(core):
    """The fix must not break the thing the route is for."""
    plan = core.planner.parse("run ls -la /tmp")
    assert [a.tool for a in plan.actions] == ["run_allowlisted_command"], plan.actions


def test_the_gate_still_gates_the_real_command(core):
    """A routing fix is not a permission change: check the unchanged verdict."""
    from jarvis.core.models import PlannedAction

    verdict = core.policy.evaluate(PlannedAction(tool="run_allowlisted_command",
                                                params={"command": "ls", "args": "-la"}))
    assert verdict.decision.name in ("EXECUTE", "CONFIRM"), verdict.decision


# ---------------------------------------------------------------------------
# 2. it is being spoken to, not programmed
# ---------------------------------------------------------------------------


def test_a_greeting_is_answered_like_one(core):
    response = asyncio.run(core.handle(CommandRequest(text="hello", source="phase44")))
    assert response.reply.strip()
    assert "understood the words but not the goal" not in response.reply, response.reply
    assert response.actions == [], "a greeting is not a request to do something"


def test_thanks_is_answered_like_one(core):
    response = asyncio.run(core.handle(CommandRequest(text="thanks", source="phase44")))
    assert "understood the words but not the goal" not in response.reply


# ---------------------------------------------------------------------------
# 3. an answer, from a named source, with no model
# ---------------------------------------------------------------------------


def test_common_questions_reach_the_lookup(core):
    for said, topic in (("what is a train", "train"),
                        ("who is apj kalam", "apj kalam"),
                        ("explain how wifi works", None),
                        ("tell me about the moon", "moon"),
                        ("what is the capital of France", None)):
        plan = core.planner.parse(said)
        tools = [a.tool for a in plan.actions]
        assert tools == ["ask_question"], (said, tools)
        if topic:
            assert plan.actions[0].params["topic"].lower() == topic, (said, plan.actions[0].params)


def test_the_lookup_is_its_own_tool_with_a_read_tier(core):
    spec = core.tools.get("ask_question")
    assert spec is not None, "a factual question has nowhere to go"
    assert spec.risk is RiskLevel.READ, "reading an encyclopedia is not an actuation"
    assert spec.requires_confirmation is False


def test_an_answer_names_the_article_it_came_from(core, wiki, monkeypatch):
    from jarvis.tools import knowledge as knowledge_module
    from jarvis.tools.base import ToolContext

    result = asyncio.run(knowledge_module.ask_question(
        ToolContext(device_id="laptop_main", session_id="p44"), topic="train"))
    assert result.ok is True, result.error
    assert "train" in result.message.lower()
    assert "en.wikipedia.org/wiki/Train" in result.message, result.message
    assert "/summary/Train" in wiki["urls"][0] or "/summary/train" in wiki["urls"][0], wiki["urls"]
    assert result.verified is not None


def test_no_network_is_not_an_invented_answer(core, wiki):
    from jarvis.tools import knowledge as knowledge_module
    from jarvis.tools.base import ToolContext

    wiki["payload"] = None
    result = asyncio.run(knowledge_module.ask_question(
        ToolContext(device_id="laptop_main", session_id="p44"), topic="xyzzyplugh"))
    assert result.ok is False, "an unreachable source answered anyway"
    assert "could not" in result.error.lower() or "no answer" in result.error.lower()


def test_an_article_that_says_nothing_says_that_too(core, wiki):
    from jarvis.tools import knowledge as knowledge_module
    from jarvis.tools.base import ToolContext

    wiki["payload"] = {"title": "Xyzzy", "extract": ""}
    result = asyncio.run(knowledge_module.ask_question(
        ToolContext(device_id="laptop_main", session_id="p44"), topic="xyzzy"))
    assert result.ok is False
    assert "xyzzy" in result.error.lower(), result.error


# ---------------------------------------------------------------------------
# a name that is not an article title still deserves a search
# ---------------------------------------------------------------------------


def test_a_name_that_404s_is_searched_before_giving_up(wiki):
    """"who is apj kalam" failed live with HTTP 404: the article is titled
    "A. P. J. Abdul Kalam". A lookup that gives up on the first miss answers
    almost nothing a person would actually ask."""
    from jarvis.tools import knowledge as knowledge_module
    from jarvis.tools.base import ToolContext

    kalam = {"title": "A. P. J. Abdul Kalam",
             "extract": "Avul Pakir Jainulabdeen Kalam was an Indian scientist and "
                        "politician who served as the 11th president of India.",
             "content_urls": {"desktop": {"en": {"url":
                         "https://en.wikipedia.org/wiki/A._P._J._Abdul_Kalam"}}}}

    def handler(url):
        if "action=query" in url:
            return _Response({"query": {"search": [{"title": "A. P. J. Abdul Kalam"}]}})
        if "Abdul_Kalam" in url:
            return _Response(kalam)
        raise OSError("HTTP Error 404: Not Found")

    wiki["handler"] = handler
    result = asyncio.run(knowledge_module.ask_question(
        ToolContext(device_id="laptop_main", session_id="p44"), topic="apj kalam"))
    assert result.ok is True, result.error
    assert "president of India" in result.message
    assert "A._P._J._Abdul_Kalam" in result.message, result.message
    searched = [u for u in wiki["urls"] if "action=query" in u]
    found = [i for i, u in enumerate(wiki["urls"]) if "Abdul_Kalam" in u]
    assert searched and found, wiki["urls"]
    assert wiki["urls"].index(searched[0]) < found[0], "search is the fallback, not the guess"


def test_a_name_in_no_article_stays_unanswered(wiki):
    """The search fallback must not turn a shrug into a confident sentence."""
    from jarvis.tools import knowledge as knowledge_module
    from jarvis.tools.base import ToolContext

    def handler(url):
        if "action=query" in url:
            return _Response({"query": {"search": []}})
        raise OSError("HTTP Error 404: Not Found")

    wiki["handler"] = handler
    result = asyncio.run(knowledge_module.ask_question(
        ToolContext(device_id="laptop_main", session_id="p44"), topic="zzqx vvoop"))
    assert result.ok is False, result
    assert "zzqx" in result.error


def test_the_fallback_hint_names_the_things_it_can_now_do(core):
    """The sentence people see when lost was written before half these abilities existed.

    It is the product's front door, and a stale one advertises a smaller system than the
    one on the machine -- which is how a person ends up believing they have to memorise
    commands.
    """
    reply = core.planner.parse("wobbleth from glintar").direct_reply.lower()
    for hint in ("what is a train", "open github.com", "check storage", "open my project"):
        assert hint in reply, f"{hint!r} missing from the hint: {reply}"
