"""Answering a factual question with no model, by asking an encyclopedia.

Asked to "add ai features, when I ask questions it gives answers, like trains, common
questions", the honest options are: a model (needs a key, Phase 42), a computation the rules
already do, or **lookup**. This is the lookup. It reads the summary of a Wikipedia article and
reports it, names the article, and says when it could not find one.

Two things it deliberately is not:

* It is not reasoning. "What is a train" has an article; "what trains run from Mumbai to
  Pune" does not, and no paraphrase of that question should be sent to a search engine and
  dressed up as an answer. The caller's shape list decides what counts as a topic.
* It is not inventing a fallback. An unreachable or empty article returns a failure naming
  the topic, because an assistant that answers a question it could not look up is worse than
  one that says it could not.
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from typing import Any, List, Tuple

from ..core.models import RiskLevel, ToolSpec
from ..net import DeadlineExceeded, http_read
from .base import ToolContext, fail, ok

TOOL = "ask_question"

SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
#: The search API is the rescue for a name that is not an article title: "apj kalam" is
#: 404 as a title and resolves as a search, and without this the lookup answers almost
#: nothing a person would actually ask.
SEARCH_URL = ("https://en.wikipedia.org/w/api.php?action=query&list=search&format=json"
              "&srlimit=1&srsearch={query}")
TIMEOUT_S = 6.0


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": "Vantage/1.0 (personal agent)"})


def _json(raw: bytes) -> dict:
    import json

    return json.loads(raw.decode("utf-8", errors="replace"))


def _summary(title: str) -> dict:
    """Fetch one article's summary, or the exception the network raised."""
    quoted = urllib.parse.quote(title.replace(" ", "_"), safe="_")
    return _json(http_read(_request(SUMMARY_URL.format(title=quoted)),
                           total=TIMEOUT_S, what="Wikipedia"))


def _best_title(query: str) -> str:
    """The nearest article name, or "" when Wikipedia has nothing to offer."""
    body = http_read(_request(SEARCH_URL.format(query=urllib.parse.quote(query))),
                     total=TIMEOUT_S, what="Wikipedia's search")
    found = (_json(body).get("query") or {}).get("search") or []
    return str(found[0].get("title") or "") if found else ""


#: Words that make a "topic" a whole *question* rather than a subject to look
#: up. "how solar panels generate electricity" is not a subject -- there is no
#: Wikipedia article by that name, and searching the phrase returns the
#: nearest article on the subject (here "Solar power"), whose summary is a
#: set of generation STATISTICS rather than an explanation of the mechanism.
#: Presenting those as the answer to "explain how solar panels generate
#: electricity" is a confident answer to a different question.
_QUESTION_WORDS = re.compile(
    r"^\s*(?:how|why|when|where|who|what|which|does|do|is|are|was|were|can|"
    r"could|would|should|will|explain|describe|define|tell|about|for)\b",
    re.I,
)

#: A subject is a short noun phrase. A long one is a question that reached
#: the lookup tool by mistake.
_MAX_TOPIC_WORDS = 6

#: The verbs a question uses. A topic with one of them in it is a question.
_PHRASE_VERBS = re.compile(
    r"\b(?:generate|generates|generated|generate\s+electricity|work|works|"
    r"produced|produces|make|makes|made|happen|occurs|comes\s+from|"
    r"differ|difference|mean|means|used|use|uses|form|forms|"
    r"created|create|change|changes|affect|affects|cause|causes)\b",
    re.I,
)


def _is_a_question(topic: str) -> bool:
    """True when this "topic" is really a question.

    The lookup tool answers a *subject*. A question is not a subject, and the
    article a search returns for one is usually about something adjacent --
    which reads as an answer and is not one.
    """
    if _QUESTION_WORDS.match(topic):
        return True
    if _PHRASE_VERBS.search(topic):
        return True
    return len(topic.split()) > _MAX_TOPIC_WORDS


def _is_a_list_article(page: Any) -> bool:
    """True when this summary is about a collection rather than a subject."""
    if not isinstance(page, dict):
        return False
    title = str(page.get("title") or "")
    desc = str(page.get("description") or "")
    if title.lower().startswith(("list of", "lists of", "index of", "outline of",
                                 "timeline of", "glossary of", "comparison of")):
        return True
    # Wikipedia describes a list article as a list; that is a reliable tell
    # when the title is not obviously one.
    return desc.strip().lower().startswith("list of")


def _named_thing(topic: str) -> str:
    """The proper name a question is really about, or "" when there is none.

    "the capital of France" is about France. Wikipedia's article for France
    names Paris in its own summary, so the article about the *subject* is the
    one that answers the question.
    """
    words = [w for w in re.split(r"[^A-Za-z0-9'’-]+", topic) if w]
    if not words:
        return ""
    lowered = [w.lower() for w in words]
    for marker in ("of", "in", "for", "on", "at"):
        if marker in lowered:
            idx = lowered.index(marker)
            tail = words[idx + 1:]
            if tail:
                return " ".join(tail)
    return ""


#: The role a question asks about, and the word an article's description
#: starts with when that article is the one. "the capital of France" wants the
#: article whose description begins "Capital ..." -- which is Paris, whose
#: description is "Capital and largest city of France".
_ROLE_WORDS = (
    ("capital", "capital"),
    ("largest city", "city"),
    ("largest", "city"),
    ("currency", "currency"),
    ("president", "president"),
    ("capital city", "capital"),
    ("population", "city"),
    ("language", "language"),
    ("discoverer", "scientist"),
    ("inventor", "scientist"),
    ("founder", "scientist"),
)


def _role_article(topic: str) -> str:
    """The subject a role question is about, or "".

    "the capital of France" is about France; "the capital of Japan" is about
    Japan. The subject's own article is the one that names the answer.
    """
    lowered = topic.lower()
    for phrase, _marker in _ROLE_WORDS:
        if phrase not in lowered:
            continue
        # The subject is what is left once the role phrase AND the connecting
        # words are removed. "the capital of France" leaves "France" -- the
        # "of" goes with the role phrase, not with the subject.
        subject = lowered.replace(phrase, " ")
        for word in ("the ", "a ", "an ", "of ", "in ", "for ", "on ", "at "):
            subject = subject.replace(word, " ")
        subject = " ".join(subject.split()).strip(" ?.,")
        return subject
    return ""


#: How the answer for a role is read out of the subject's article. The role
#: word, then the answer, in the shape Wikipedia writes it: "its capital,
#: largest city and main cultural and economic centre is Paris".
_ROLE_ANSWER = re.compile(
    r"capital[^.;]{0,120}?\b(?:is|are)\s+([A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*){0,3})",
)


def _role_answer(extract: str) -> str:
    """The answer to a role question, read out of the subject's article.

    Wikipedia's summaries say "its capital, largest city and main cultural and
    economic centre is Paris" -- the role and its answer in one sentence. That
    sentence is the answer to "what is the capital of France", and it is in
    the article the question is about.
    """
    match = _ROLE_ANSWER.search(extract or "")
    if not match:
        return ""
    answer = match.group(1).strip()
    # Strip a trailing clause that is not part of the name.
    answer = re.split(r",| with | which | where ", answer)[0].strip()
    return answer if 2 <= len(answer) <= 60 else ""


def _role_matches(page: Any, marker: str) -> bool:
    """True when this article's description starts with the role word."""
    if not isinstance(page, dict):
        return False
    desc = str(page.get("description") or "").strip().lower()
    return desc.startswith(marker.lower())


async def ask_question(ctx: ToolContext, topic: str = "") -> Any:
    """Look a topic up in Wikipedia and report its summary, with the article named."""
    wanted = " ".join(str(topic or "").split())
    if not wanted:
        return fail(TOOL, "Ask me about something specific.", device=ctx.device_id)

    # A question is not a subject. Saying so is better than answering a
    # different question: the caller is told plainly that this needs a model
    # rather than a lookup, and nothing is presented as an answer that is not
    # one.
    if _is_a_question(wanted):
        return fail(
            TOOL,
            f"{wanted!r} is a question rather than a subject I can look up. "
            "I would need an AI provider for that one.",
            device=ctx.device_id,
            topic=wanted,
            needs_model=True,
        )

    title = wanted[0].upper() + wanted[1:]
    page: Any = None
    tried = title
    try:
        page = _summary(title)
    except DeadlineExceeded as exc:
        return fail(TOOL, f"I could not reach Wikipedia for {wanted!r}: {exc}",
                    device=ctx.device_id, topic=wanted)
    except Exception:
        # The title was not an article. Search once before saying so.
        try:
            tried = _best_title(wanted) or title
            if tried != title:
                page = _summary(tried)
        except DeadlineExceeded as exc:
            return fail(TOOL, f"I could not reach Wikipedia for {wanted!r}: {exc}",
                        device=ctx.device_id, topic=wanted)
        except Exception:
            page = None

    # A "List of ..." article is about a collection, not about the subject that
    # was asked about. Wikipedia redirects "Capital of France" to "List of
    # capitals of France", whose summary is a chronology -- which answers
    # "what is the capital of France" with a list rather than with Paris.
    #
    # The subject's own article is the one wanted, and Wikipedia knows it: the
    # summary's `description` names the role ("Capital and largest city of
    # France"). So the article whose description matches the question's own
    # words wins over the list.
    if page and _is_a_list_article(page):
        subject = _named_thing(wanted)
        if subject:
            try:
                better = _summary(subject)
            except Exception:
                better = None
            if better and not _is_a_list_article(better):
                page = better
                tried = str(better.get("title") or tried)

    # A "what is the capital of France" question wants the capital, and the
    # article about France says what it is: "its capital, largest city and
    # main cultural and economic centre is Paris". Wikipedia's own summary
    # is the answer, and it is in the article the question is about.
    role_subject = _role_article(wanted)
    if role_subject:
        try:
            subject_page = _summary(role_subject)
        except Exception:
            subject_page = None
        if subject_page:
            answer = _role_answer(str(subject_page.get("extract") or ""))
            if answer:
                link = str(((subject_page.get("content_urls") or {}).get("desktop")
                            or {}).get("en") or "").strip()
                article = link or f"https://en.wikipedia.org/wiki/{urllib.parse.quote(role_subject)}"
                # The role, said the way a person would: "The capital of France
                # is Paris." The subject's own name is the subject of the
                # question, and the role is what was asked for.
                role = wanted.split(" of ")[0].replace("the ", "").strip() or "answer"
                name = subject_page.get("title") or role_subject
                return ok(
                    TOOL,
                    f"The {role} of {name} is {answer}.  — {article}",
                    device=ctx.device_id, verified=True,
                    topic=wanted, title=name,
                    extract=answer, source=article,
                )

    extract = str((page or {}).get("extract") or "").strip()
    if not extract:
        return fail(TOOL, f"Wikipedia has no article for {wanted!r}, so I have no answer "
                    "for that -- I will not guess one.", device=ctx.device_id,
                    topic=wanted, tried=tried)

    link = str(((page.get("content_urls") or {}).get("desktop") or {}).get("en") or "").strip()
    article = link or f"https://en.wikipedia.org/wiki/{urllib.parse.quote(tried)}"
    return ok(TOOL, f"{extract}  — {article}", device=ctx.device_id, verified=True,
              topic=wanted, title=page.get("title") or tried, extract=extract,
              source=article)


SPECS: List[Tuple[ToolSpec, Any]] = [
    (
        ToolSpec(
            name=TOOL,
            description="Look up a factual topic and report its Wikipedia summary, with the article named.",
            agent="laptop",
            risk=RiskLevel.READ,
            reversible=True,
            parameters={"topic": "string"},
            required_parameters=["topic"],
            tags=["knowledge", "read", "web", "question"],
        ),
        ask_question,
    ),
]
