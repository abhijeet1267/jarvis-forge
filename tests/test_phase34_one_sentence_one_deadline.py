"""Phase 34: one sentence gets one deadline, not one deadline per pause.

29-B was recorded as "no bounded read timeout on the arXiv path (measured 41,247 ms)".
Measured again here, that understates it. The code does set a timeout:

    jarvis/research/sources.py:45   REQUEST_TIMEOUT = 20.0
    jarvis/research/sources.py:186  urlopen(request, timeout=REQUEST_TIMEOUT)   # twice

but `urlopen`'s timeout is a *socket inactivity* timeout. A server that sends a byte every
few seconds never trips it, so the wait is not 20 s or 40 s -- it has no end. Proven against
a local trickler before anything was written:

    STILL WAITING after 60.0s -- socket timeout is 20.0s per inactivity gap, total unbounded

The same hole is at the two other `urlopen` sites (embeddings: 30 s, vision provider: 60 s).
A stalled network read is not a slow answer; it is the request thread, the dashboard polling
it, and the voice pipeline waiting behind it. The fix is a **total** deadline that covers
every attempt and every chunk read, so the worst case is a number someone can read.

Rules pinned here:

* a trickling server is stopped by the deadline, in roughly the deadline;
* a retry shares the remaining budget rather than being handed a fresh one;
* the expiry is reported as a timeout, with its number in the sentence, and never as an
  empty result -- "arXiv did not answer" and "there are no papers about that" are different
  claims, and Phase 21 spent a phase keeping them apart;
* every `urlopen` in the codebase goes through the deadline helper;
* the sites this phase did not convert are *counted and pinned*, so the known gap cannot
  grow silently: a new silence-only network call makes the suite red.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from pathlib import Path

import pytest

from jarvis.research.sources import ArxivSource

ROOT = Path(__file__).resolve().parents[1]

BODY = (
    '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
    "<entry><id>http://arxiv.org/abs/2401.00001v1</id>"
    "<title>Growing Agents</title><summary>slow servers</summary>"
    "<author><name>A. Owner</name></author><updated>2024-01-01T00:00:00Z</updated>"
    "</entry></feed>"
)


class _Trickler:
    """An HTTP server that answers, then dribbles a byte at a time forever.

    Silence would trip a socket timeout; this server is never silent, which is the point.
    The declared Content-Length is far larger than what it will ever send, so a client that
    trusts the socket timeout keeps waiting for bytes that are technically still arriving.
    """

    ENDLESS = 10 ** 7

    def __init__(self, body: str = BODY, gap: float = 0.01, chunk: int = 1,
                 endless: bool = True):
        self.endless = endless
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.body = body.encode()
        self.gap = gap
        self.chunk = chunk
        self.requests = 0
        self.stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/query"

    def _serve(self) -> None:
        endless = self.endless
        while not self.stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.requests += 1
            with conn:
                try:
                    conn.recv(65536)
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n"
                        % (self.ENDLESS if endless else len(self.body))
                    )
                    if endless:
                        while True:
                            conn.sendall(self.body[:self.chunk])
                            time.sleep(self.gap)
                    else:
                        conn.sendall(self.body)
                except OSError:
                    pass

    def close(self) -> None:
        self.stop = True
        try:
            self.sock.close()
        except OSError:
            pass


class _Instant(_Trickler):
    """A well-behaved server, to prove the deadline does not break the fast path: it
    declares the length it actually sends."""

    def __init__(self, body: str = BODY):
        super().__init__(body=body, gap=0.0, chunk=10_000, endless=False)


@pytest.fixture()
def arxiv_trickler():
    server = _Trickler()
    yield server
    server.close()


@pytest.fixture()
def arxiv_instant():
    server = _Instant()
    yield server
    server.close()


# ---------------------------------------------------------------------------
# 1. the wait is bounded, and bounded by the number on the label
# ---------------------------------------------------------------------------


def test_a_trickling_server_is_stopped_by_the_deadline(monkeypatch, arxiv_trickler):
    import jarvis.research.sources as sources

    monkeypatch.setattr(sources, "ARXIV_ENDPOINT", arxiv_trickler.url)
    monkeypatch.setattr(sources, "REQUEST_TIMEOUT", 1.0)
    source = ArxivSource()

    started = time.monotonic()
    with pytest.raises(Exception) as raised:
        source.search("growing agents", limit=2)
    elapsed = time.monotonic() - started

    assert elapsed < 4.0, (
        f"the read ran {elapsed:.1f}s against a server that trickles every 0.05s -- "
        "an inactivity timeout does not bound that")
    assert "1" in str(raised.value) or "second" in str(raised.value).lower(), str(raised.value)


def test_the_fast_path_still_returns_real_results(monkeypatch, arxiv_instant):
    """A deadline that only fires on a stall must not become a deadline that fires on
    ordinary latency, or the fix is just a way to break search."""
    import jarvis.research.sources as sources

    monkeypatch.setattr(sources, "ARXIV_ENDPOINT", arxiv_instant.url)
    monkeypatch.setattr(sources, "REQUEST_TIMEOUT", 5.0)
    hits = ArxivSource().search("growing agents", limit=2)
    assert [h.title for h in hits] == ["Growing Agents"], [h.title for h in hits]


def test_a_retry_shares_the_remaining_budget_not_a_fresh_one(monkeypatch, arxiv_trickler):
    """The path retries once. Two full budgets in series is not a bound a reader can use.

    Measured against a trickler with a 1.2s total: the first attempt consumes nearly all of
    it, so the second must fail immediately rather than restart the wait.
    """
    import jarvis.research.sources as sources

    monkeypatch.setattr(sources, "ARXIV_ENDPOINT", arxiv_trickler.url)
    monkeypatch.setattr(sources, "REQUEST_TIMEOUT", 1.2)
    started = time.monotonic()
    with pytest.raises(Exception):
        ArxivSource().search("growing agents", limit=2)
    elapsed = time.monotonic() - started

    assert elapsed < 4.0, f"two attempts cost {elapsed:.1f}s against a 1.2s budget"
    assert arxiv_trickler.requests <= 2, (
        "the retry loop kept sending requests after the deadline had passed")


def test_the_tool_reports_a_timeout_rather_than_an_empty_shelf(core, monkeypatch,
                                                                arxiv_trickler, tmp_path):
    """"No papers found" and "the source did not answer" are different claims.

    Phase 21 spent a phase on exactly this line (offline-as-empty), and a timeout that
    lands in the same reply as an empty result reopens it: the user is told the literature
    is quiet when the network was not.
    """
    import asyncio
    from jarvis.core.models import CommandRequest
    import jarvis.research.sources as sources

    monkeypatch.setattr(sources, "ARXIV_ENDPOINT", arxiv_trickler.url)
    monkeypatch.setattr(sources, "REQUEST_TIMEOUT", 1.0)

    result = asyncio.run(core.handle(CommandRequest(
        text="search papers about growing agents", source="phase34")))
    reply = result.reply.lower()
    # The timeout has to be in the sentence, and the emptiness it exposes has to be
    # scoped to the source that really was empty. "Nothing exists about that" would be a
    # different claim from "the local shelf is empty and arXiv did not answer".
    assert "did not answer" in reply, reply
    assert "arxiv" in reply, reply
    assert "local" in reply, reply


def test_no_papers_are_invented_when_the_clock_runs_out(core, monkeypatch, arxiv_trickler):
    import asyncio
    from jarvis.core.models import CommandRequest
    import jarvis.research.sources as sources
    from jarvis.research import papers as papers_module

    monkeypatch.setattr(sources, "ARXIV_ENDPOINT", arxiv_trickler.url)
    monkeypatch.setattr(sources, "REQUEST_TIMEOUT", 1.0)
    # The store is session-wide, so a paper another test filed under "agents"
    # makes the *local* fallback answer and this assertion fail -- measured: it
    # failed in the full run and passed alone, and a seeded paper reproduces it.
    # The property under test is that a source going quiet invents nothing, not
    # that the shelf is empty, so start from an empty shelf rather than from
    # whatever the rest of the session left behind.
    monkeypatch.setattr(papers_module.PaperStore, "search",
                        lambda self, query, limit=10: [])
    monkeypatch.setattr(papers_module.PaperStore, "all", lambda self, limit=500: [])
    result = asyncio.run(core.handle(CommandRequest(
        text="search papers about growing agents", source="phase34")))
    # The honest outcome is a result set with nothing invented in it. A local shelf
    # that really is empty is allowed to answer ok=True; what is not allowed is a
    # plausible-looking paper appearing because a source went quiet.
    for action in result.actions:
        papers = (action.output or {}).get("papers") or (action.output or {}).get("results") or []
        assert papers == [], (action.summary, papers)


# ---------------------------------------------------------------------------
# 2. the other readers of the same class
# ---------------------------------------------------------------------------


def test_the_vision_endpoint_read_is_bounded_too(monkeypatch, arxiv_trickler, tmp_path):
    """The vision provider has the identical shape at timeout=60 -- a dribble never trips
    it either, and the sentence waiting on it is a chat turn, not a background job."""
    import importlib

    providers = importlib.import_module("jarvis.vision.providers")
    monkeypatch.setattr(providers, "REQUEST_TIMEOUT", 1.0)
    image = tmp_path / "p34.png"
    image.write_bytes(_PNG)
    provider = providers.RemoteVisionProvider(
        _settings(base_url=arxiv_trickler.url.rsplit("/api", 1)[0], key="test-key"))

    started = time.monotonic()
    outcome = provider.describe_image(str(image))
    assert time.monotonic() - started < 4.0, "the vision read waited on a dribble"
    assert outcome.get("ok") is False, outcome
    error = str(outcome.get("error", "")).lower()
    # The claim is that a reader can tell a deadline fired and how long it was, not that
    # it uses one particular verb.
    assert "within 1s" in error, outcome


def test_the_embedding_read_is_bounded_too(monkeypatch, arxiv_trickler):
    import jarvis.memory.embeddings as embeddings

    monkeypatch.setattr(embeddings, "REQUEST_TIMEOUT", 1.0)
    embedder = embeddings.RemoteEmbedder(
        _settings(base_url=arxiv_trickler.url.rsplit("/api", 1)[0], key="test-key"))
    started = time.monotonic()
    with pytest.raises(Exception):
        embedder.embed("growing agents")
    assert time.monotonic() - started < 4.0


def _settings(base_url: str, key: str):
    from jarvis.config import get_settings

    settings = get_settings().model_copy(update={"llm_base_url": base_url,
                                                 "llm_api_key": key})
    return settings


#: A 1x1 PNG, so the vision read under test is a real request rather than a stub.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def test_every_urlopen_in_the_codebase_goes_through_the_deadline():
    """Three call sites, one rule. A fourth added with a bare `timeout=` would pass review
    and silently reintroduce the unbounded wait this phase exists to end."""
    offenders = []
    for path in sorted((ROOT / "jarvis").rglob("*.py")):
        if path.name == "net.py":
            continue  # the one place the rule is allowed to live
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"urlopen\(", text):
            line = text[:match.start()].count("\n") + 1
            window = "\n".join(text.splitlines()[max(0, line - 4):line + 2])
            if "http_read" not in window and "def http_read" not in window:
                offenders.append(f"{path.relative_to(ROOT)}:{line}")
    assert offenders == [], f"urlopen without a total deadline at: {offenders}"


def test_the_silence_only_sites_are_counted_not_forgotten():
    """A ratchet over what this phase did not convert.

    The httpx and socket readers bound silence, not duration, and were left alone because
    each needs its own reading loop and none of them was measured hanging. Listing them
    with a number keeps the gap real: if the count grows, the suite says so.

    The set is re-recorded when a new site is verified and added deliberately. Current
    set (2026-10-16): the six original sites plus the companion-gateway proxy
    (jarvis/companion/gateway.py, timeout=30.0) — a read of the *local* backend over
    loopback, so silence-bounding is an acceptable risk there until measured otherwise.

    **2026-10-22:** the backend's own pairing-code request
    (jarvis/api/app.py, timeout=10.0) joins the set, for the same reason and on
    the same reasoning as the gateway's proxy: it reads the *local* companion
    gateway over loopback, and a gateway that is not running answers by
    refusing rather than by hanging. It is here so the count is real rather
    than because the gap is acceptable — a read of a remote host would not
    be.
    """
    silence_only = []
    for path in sorted((ROOT / "jarvis").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"httpx\.(?:Async)?Client\(\s*timeout=([\d.]+)", text):
            silence_only.append(f"{path.relative_to(ROOT)}:{match.group(1)}")
    # The exact set of silence-only sites, pinned as a multiset (calendar_provider
    # has four distinct call sites, each 20.0). Re-record only on a deliberate change.
    from collections import Counter
    expected = Counter([
        "jarvis/api/app.py:10.0",
        "jarvis/companion/gateway.py:30.0",
        "jarvis/integrations/calendar_provider.py:20.0",
        "jarvis/integrations/calendar_provider.py:20.0",
        "jarvis/integrations/calendar_provider.py:20.0",
        "jarvis/integrations/calendar_provider.py:20.0",
        "jarvis/transport/endpoint.py:15.0",
        "jarvis/voice/stt.py:60.0",
    ])
    assert Counter(silence_only) == expected, (
        "the silence-only set changed — add or remove a site deliberately:\n"
        + "\n".join(sorted(set(silence_only) ^ set(expected))))


# ---------------------------------------------------------------------------
# 3. the budget belongs to the sentence, not to each read inside it
# ---------------------------------------------------------------------------


def test_two_reads_in_one_sentence_share_one_deadline(monkeypatch, arxiv_trickler):
    """Bounded reads do not add up to a bounded sentence.

    Measured after the first part of this phase landed: "remember that ..." with a remote
    embedder configured performs *two* outbound reads, each with its own 30s budget -- so
    the wait a person faces was 60s, and the number written in the code stopped meaning
    anything. The budget now belongs to the request; a read that arrives after it is spent
    says so instead of starting a fresh wait.
    """
    import asyncio

    import jarvis.core.orchestrator as orchestrator
    import jarvis.net as net
    from jarvis.core.models import CommandRequest

    monkeypatch.setattr(orchestrator, "SENTENCE_BUDGET", 4.0)
    core = _core_with_embedder_pointed_at(monkeypatch, arxiv_trickler)

    # Observed from inside the request: the budget is a context variable, so asking for it
    # after handle() returns would read "no request in flight", which is true and proves
    # nothing about what the second read saw.
    seen: list = []
    real = net.http_read

    def watching(request, **kw):
        seen.append(net.remaining_budget())
        return real(request, **kw)

    import jarvis.memory.embeddings as embeddings

    monkeypatch.setattr(embeddings, "http_read", watching)
    started = time.monotonic()
    asyncio.run(core.handle(CommandRequest(
        text="remember that the heater coil arrived", source="phase34")))
    elapsed = time.monotonic() - started

    assert len(seen) >= 2, f"only {len(seen)} read(s) ran; the sentence never tested a budget"
    assert seen[1] <= 1.0, (
        f"the second read started with {seen[1]:.1f}s of a 4.0s budget left -- reads are "
        "still being handed fresh budgets")
    assert elapsed < 9.0, f"the sentence spent {elapsed:.1f}s on a 4.0s budget"


def test_the_budget_is_returned_after_a_sentence(monkeypatch, arxiv_instant):
    """A per-request budget that leaked between requests would, in time, refuse every
    network call the process makes."""
    import asyncio

    import jarvis.net as net
    from jarvis.core.models import CommandRequest

    core = _core_with_embedder_pointed_at(monkeypatch, arxiv_instant)
    asyncio.run(core.handle(CommandRequest(text="remember that the coil arrived",
                                           source="phase34")))
    assert net.remaining_budget() > 0.0, "the budget was never released back"


def _core_with_embedder_pointed_at(monkeypatch, server):
    """A core whose remote embedder talks to the test server, so the reads under test
    are real reads of a real socket rather than a stubbed function."""
    from jarvis.config import get_settings
    import jarvis.memory.embeddings as embeddings

    settings = get_settings()
    monkeypatch.setattr(settings, "llm_base_url", server.url.rsplit("/api", 1)[0])
    monkeypatch.setattr(settings, "llm_api_key", "trickler")
    embeddings._embedder = None
    from jarvis.api.app import get_core
    return get_core()
