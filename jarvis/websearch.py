"""A web search that searches, and says which of the three answers it has.

There are three outcomes a search can have and they are not interchangeable:

```text
the service answered with something      -> results
the service answered with nothing        -> "nothing came back for that"
the service could not be reached/read    -> an outage, which is NOT an empty answer
```

Collapsing the last two is the defect this module is written against, and it is the same one
Phase 15's offline work found in mail and calendar: silence reported as an empty day. The other
defect it is written against is the more tempting one -- answering ``search Google for X`` by
opening google.com, which produces a successful-looking page and no search.

The provider is DuckDuckGo's Instant Answer API: no key, documented, JSON, and honest about
having nothing for a query. It is an encyclopaedic index rather than a general web ranking
engine, so the tool says where an answer came from and never dresses an absent result up.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from . import net

#: The endpoint. HTTPS, no credentials, and the parameters are built by urlencode below so a
#: query can never contribute structure to the request -- only a value.
ENDPOINT = "https://api.duckduckgo.com/"

#: A search is a step inside someone's sentence, not a background job. ``net.http_read``
#: enforces this as a whole-call budget and passes GAP_TIMEOUT to urlopen as the per-socket
#: silence limit; both stay under the budget Phase 34 gave a sentence.
TOTAL_BUDGET_S = 5.0

#: A query longer than this is a paragraph someone pasted, not a question. Refusing is kinder
#: than sending it: the API would answer with nothing and the user would read that as "not
#: found" rather than "too long to ask".
MAX_QUERY_CHARS = 400

SOURCE_NAME = "duckduckgo-instant-answer"


class SearchUnavailable(Exception):
    """The service could not be reached, or did not answer in time.

    Distinct from an empty result on purpose: an outage is a failure of the tool, an empty
    answer is a complete answer from the service.
    """


class SearchUnreadable(Exception):
    """The service answered, but not with what was expected."""


def build_url(query: str) -> str:
    """The request address, with the query confined to its own parameter."""
    params = {
        "q": query,
        "format": "json",
        "no_html": "1",
        "no_redirect": "1",
        "skip_disambig": "1",
    }
    return f"{ENDPOINT}?{urllib.parse.urlencode(params)}"


def fetch(query: str) -> Dict[str, Any]:
    """Ask the service. Raises SearchUnavailable / SearchUnreadable; never returns None."""
    request = urllib.request.Request(
        build_url(query),
        headers={"User-Agent": "Vantage/1.0 (+local personal assistant)"},
    )
    try:
        body = net.http_read(request, total=TOTAL_BUDGET_S, what="the search service")
    except (net.DeadlineExceeded, urllib.error.URLError, OSError) as exc:
        raise SearchUnavailable(str(exc) or exc.__class__.__name__) from exc

    try:
        payload = json.loads(body.decode("utf-8", errors="replace") or "{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise SearchUnreadable(f"the answer was not the expected shape ({exc})") from exc
    if not isinstance(payload, dict):
        raise SearchUnreadable("the answer was a JSON value rather than an object")
    return payload


def parse(payload: Dict[str, Any], limit: int = 5) -> List[Dict[str, str]]:
    """Only what the service actually said. Nothing here is generated.

    ``Type`` is checked because the API answers "A" (article), "D" (disambiguation) and "N"
    (no answer); an "N" payload can still carry a non-empty ``RelatedTopics`` from a previous
    shape, and trusting the shape rather than the type is how a stale field becomes a result.
    """
    wanted = max(1, min(int(limit or 5), 10))
    results: List[Dict[str, str]] = []

    if payload.get("Type") not in (None, "", "A", "D"):
        return results

    abstract = str(payload.get("AbstractText") or "").strip()
    url = str(payload.get("AbstractURL") or "").strip()
    if abstract and url:
        results.append({"title": str(payload.get("Heading") or "").strip() or url,
                        "url": url, "snippet": abstract})

    for topic in payload.get("RelatedTopics") or []:
        if len(results) >= wanted:
            break
        if not isinstance(topic, dict):
            continue
        # A disambiguation groups its topics one level deeper.
        for item in ([topic] if topic.get("FirstURL") else topic.get("Topics") or []):
            if not isinstance(item, dict):
                continue
            link = str(item.get("FirstURL") or "").strip()
            text = str(item.get("Text") or "").strip()
            if link and len(results) < wanted:
                results.append({"title": (text.split(" - ")[0] if text else link)[:120],
                                "url": link, "snippet": text})

    return results


def search(query: str, limit: int = 5) -> List[Dict[str, str]]:
    """Ask and parse. Raises rather than returning an empty list on a failure."""
    return parse(fetch(query), limit)


def describe() -> Dict[str, Any]:
    """What this capability is, without making a request.

    ``reachable`` is deliberately not probed here: a status call that reached the network
    would let a description endpoint decide whether a feature exists. The tool reports
    reachability when it actually asks.
    """
    return {
        "name": SOURCE_NAME,
        "endpoint": ENDPOINT,
        "requires_key": False,
        "reachable": "unknown until asked",
        "budget_seconds": TOTAL_BUDGET_S,
        "max_query_chars": MAX_QUERY_CHARS,
        "kind": "encyclopaedic instant answers, not a ranked web index",
    }
