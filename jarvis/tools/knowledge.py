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


async def ask_question(ctx: ToolContext, topic: str = "") -> Any:
    """Look a topic up in Wikipedia and report its summary, with the article named."""
    wanted = " ".join(str(topic or "").split())
    if not wanted:
        return fail(TOOL, "Ask me about something specific.", device=ctx.device_id)

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
