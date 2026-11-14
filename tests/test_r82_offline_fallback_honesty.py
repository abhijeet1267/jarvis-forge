"""The offline fallback must not answer a different question.

When no AI provider is configured, the backend falls back to a Wikipedia
lookup (`ask_question`). That fallback is fine for a *subject* — "who is Ada
Lovelace" — but it was answering a *question* with an article about
something adjacent, and presenting it as the answer.

**Measured, before the fix, in an isolated home with no provider key:**

```
Q: Explain how solar panels generate electricity.
A: Solar power has a growing role in electricity production in the United
   Kingdom, contributing around 6.4% of the UK's annual power generation in
   2025. ...  (model_used: rules)
```

That is a set of UK generation **statistics** — the "Solar power" article,
found by searching the phrase "how solar panels generate electricity". It
answers a *different question*, it looks exactly like an answer, and nothing
in the response says a model was not involved.

**A second, quieter version of the same defect.** "what is the capital of
France" returned the Wikipedia article "List of capitals of France" — a
chronology, where the answer is "Paris". Wikipedia redirects "Capital of
France" to that list. The subject's own article says the answer outright
("its capital, largest city and main cultural and economic centre is Paris"),
and that is the article the answer should come from.

These tests pin the two rules:

1. A question is not a subject. The lookup refuses it and says it needs a
   model, rather than answering something adjacent.
2. A role question is answered from the subject's article, by the role's
   own answer — not from a list article about the role.

Everything here runs against Wikipedia over the real network, so the skips
are honest: no network means no result to check.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from jarvis.tools.knowledge import (
    _is_a_list_article,
    _is_a_question,
    _named_thing,
    _role_answer,
    _role_article,
)


# ---------------------------------------------------------------------------
# The rule: a question is not a subject
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "topic",
    [
        "how solar panels generate electricity",
        "why is the sky blue",
        "how do vaccines work",
        "what causes the seasons",
    ],
)
def test_a_question_is_not_a_subject(topic):
    """These are questions. There is no article to look up, and the nearest
    article is about something adjacent."""
    assert _is_a_question(topic), (
        f"{topic!r} is a question; routing it to a lookup produced an article "
        "about a different subject")


@pytest.mark.parametrize(
    "topic",
    [
        "the Great Barrier Reef",
        "Ada Lovelace",
        "photosynthesis",
        "the capital of France",
        "Python programming language",
    ],
)
def test_a_subject_is_not_a_question(topic):
    """These ARE subjects, and the lookup answers them well. They must not
    be refused."""
    assert not _is_a_question(topic), (
        f"{topic!r} is a subject; refusing it would lose a good answer")


# ---------------------------------------------------------------------------
# The rule: a role question is answered from the subject's article
# ---------------------------------------------------------------------------
def test_the_subject_of_a_role_question_is_found():
    assert _role_article("the capital of France") == "france"
    assert _role_article("capital of Japan") == "japan"


def test_a_list_article_is_recognised():
    assert _is_a_list_article({"title": "List of capitals of France"})
    assert _is_a_list_article({"title": "Lists of something"})
    assert not _is_a_list_article({"title": "France"})
    assert not _is_a_list_article({"title": "Paris"})


def test_the_named_thing_is_the_subject():
    assert _named_thing("the capital of France") == "France"
    assert _named_thing("Great Barrier Reef") == ""


def test_the_role_answer_is_read_out_of_the_subjects_article():
    """The shape Wikipedia actually writes it in.

    "France ... its capital, largest city and main cultural and economic
    centre is Paris" — the role and its answer in one sentence.
    """
    extract = (
        "France, officially the French Republic, is a country primarily located "
        "in Western Europe. Its capital, largest city and main cultural and "
        "economic centre is Paris, with a metropolitan area of over 12 million."
    )
    assert _role_answer(extract) == "Paris"


def test_no_role_answer_is_invented():
    """An article that does not say must not produce an answer."""
    assert _role_answer("France is a country in Western Europe.") == ""
    assert _role_answer("") == ""


# ---------------------------------------------------------------------------
# End to end, against the real Wikipedia and an isolated home
# ---------------------------------------------------------------------------
def _has_network() -> bool:
    """Whether Wikipedia answers us.

    The User-Agent matters: Wikipedia refuses a request without one with a
    403, so a probe that omits it would skip these tests even when the
    network is fine -- and the tool itself always sends one.
    """
    import urllib.request
    try:
        request = urllib.request.Request(
            "https://en.wikipedia.org/api/rest_v1/page/summary/France",
            headers={"User-Agent": "Vantage/1.0 (personal agent)"})
        urllib.request.urlopen(request, timeout=8)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _has_network(), reason="Wikipedia is not reachable here")
def test_the_capital_question_is_answered_with_the_capital():
    """The end-to-end version, through the tool itself."""
    import asyncio

    from jarvis.tools.base import ToolContext
    from jarvis.tools.knowledge import ask_question

    async def run():
        ctx = ToolContext(device_id="laptop_main")
        return await ask_question(ctx, topic="the capital of France")

    result = asyncio.run(run())
    assert result.ok, result.summary
    text = str(result.message or "")
    assert "Paris" in text, (
        f"the answer to 'the capital of France' did not name Paris: {text!r}")
    assert "List of capitals" not in text, (
        f"the answer came from a list article: {text!r}")


@pytest.mark.skipif(not _has_network(), reason="Wikipedia is not reachable here")
def test_a_question_is_refused_rather_than_answered_with_the_wrong_article():
    """The end-to-end version of the original defect."""
    import asyncio

    from jarvis.tools.base import ToolContext
    from jarvis.tools.knowledge import ask_question

    async def run():
        ctx = ToolContext(device_id="laptop_main")
        return await ask_question(
            ctx, topic="how solar panels generate electricity")

    result = asyncio.run(run())
    assert not result.ok, (
        "a question was answered by a lookup; the answer would be an article "
        "about a different subject")
    assert "AI provider" in str(result.summary or ""), (
        "the refusal did not say that a model is what is needed: "
        f"{result.summary!r}")


# ---------------------------------------------------------------------------
# The whole path, with no provider configured
# ---------------------------------------------------------------------------
def test_with_no_provider_a_question_is_not_answered_from_a_wrong_article():
    """The full pipeline, in an isolated home with no provider key.

    The point is the *shape* of the answer, not its wording: an answer that
    comes from a lookup must say so, and must not be a different subject.
    """
    import asyncio

    home = tempfile.mkdtemp(prefix="vantage_nollm_")
    os.environ["JARVIS_HOME"] = home

    from jarvis.core.models import CommandRequest
    from jarvis.core.orchestrator import JarvisCore

    async def run():
        core = JarvisCore()
        return await core.handle(CommandRequest(
            text="Explain how solar panels generate electricity.",
            source="native"))

    reply = asyncio.run(run()).reply or ""
    assert "United Kingdom" not in reply, (
        "the lookup answered with an article about UK solar STATISTICS, which "
        f"is a different subject from the one that was asked about: {reply!r}")
    assert "AI provider" in reply, (
        f"the answer did not say a model is needed: {reply!r}")
