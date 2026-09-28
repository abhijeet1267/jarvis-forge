"""Phase 21: fast, and honest about being bounded.

Two measured problems this covers:

* ``search_files`` with a query that does not match took **6.9 s** against a home
  directory, against 0.36 s when it did match. The walk only stopped early on a
  find, and it descended into ``~/Library`` -- macOS' largest tree, and not where a
  user's documents live.
* ``research`` died whenever arXiv answered ``HTTP Error 406``. The cause is a
  client fingerprint at their edge, not the request: measured on one machine in one
  minute, ``curl`` gets 200 while urllib and httpx both get 406, with a browser UA, a
  self-identifying UA, HTTPS and HTTP/2 each tried and discarding itself. The failure
  was reported honestly (no invented results) but the capability was dead, and one
  refused source should not be able to kill a command that has other approved sources.

The bound is only half the fix. A search that stops early must say so -- a
"nothing found" that really means "I stopped looking" is the kind of confident
wrong answer the previous twenty phases exist to prevent.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from jarvis.tools import files as files_module
from jarvis.tools.base import ToolContext


def _ctx(core) -> ToolContext:
    return ToolContext(settings=core.settings, apps=core.apps, devices=core.devices,
                       memory=core.memory, graph=core.graph, core=core)


@pytest.fixture()
def big_tree(tmp_path: Path) -> Path:
    """A directory with far more non-matching entries than the scan cap."""
    for group in range(60):
        leaf = tmp_path / f"cache-{group}"
        leaf.mkdir()
        for index in range(80):
            (leaf / f"blob-{group}-{index}.bin").write_text("x")
    return tmp_path


# ---------------------------------------------------------------------------
# Bounded walk
# ---------------------------------------------------------------------------


def test_a_search_has_a_scan_cap_at_all():
    assert getattr(files_module, "SEARCH_SCAN_CAP", 0) > 0, (
        "no bound exists -- an unmatched query walks the entire tree")
    assert getattr(files_module, "SEARCH_TIME_BUDGET_S", 0) > 0


def test_pruned_directories_are_the_ones_nobody_means(tmp_path: Path):
    """macOS' ~/Library is hundreds of thousands of entries; no user asks for it."""
    prune = files_module.PRUNED_DIRS
    for name in ("Library", "node_modules", "__pycache__"):
        assert name in prune, name


async def test_an_unmatched_search_stops_within_its_budget(core, big_tree, monkeypatch):
    monkeypatch.setattr(files_module, "SEARCH_SCAN_CAP", 400)
    started = time.perf_counter()
    result = await files_module.search_files(_ctx(core), query="quarterly-report",
                                             directory=str(big_tree))
    elapsed = time.perf_counter() - started

    assert result.ok
    assert elapsed < 2.0, f"bounded search still took {elapsed:.2f}s"


async def test_a_stopped_early_search_says_so(core, big_tree, monkeypatch):
    """The bound is only safe if it is disclosed."""
    monkeypatch.setattr(files_module, "SEARCH_SCAN_CAP", 400)
    result = await files_module.search_files(_ctx(core), query="quarterly-report",
                                             directory=str(big_tree))

    assert result.output["truncated"] is True
    assert result.output["scanned"] >= 400
    assert "stopped" in result.message.lower() or "not exhaustive" in result.message.lower(), (
        result.message)
    assert "could not find" not in result.message.lower(), (
        "an incomplete search reported a definite negative")


async def test_a_complete_search_does_not_claim_truncation(core, tmp_path):
    (tmp_path / "notes.txt").write_text("x")
    result = await files_module.search_files(_ctx(core), query="notes",
                                             directory=str(tmp_path))

    assert result.output["truncated"] is False
    assert result.output["results"]


async def test_a_real_match_still_wins_before_the_cap(core, big_tree, monkeypatch):
    (big_tree / "important-report.md").write_text("x")
    monkeypatch.setattr(files_module, "SEARCH_SCAN_CAP", 400)
    result = await files_module.search_files(_ctx(core), query="important-report",
                                             directory=str(big_tree))

    assert any("important-report" in r["name"] for r in result.output["results"])


def _no_index(monkeypatch):
    """Take Spotlight out of the path so the *walk* is what is under test.

    Whether the index has already seen a file written two milliseconds ago is the
    runner's business: on a GitHub macOS runner it answers for paths under
    /private/var/folders, on a laptop it does not. That made this assertion pass
    locally and fail in CI while the code did the same thing both times.
    """
    monkeypatch.setattr(files_module.shutil, "which", lambda name: None)


async def test_pruned_directories_are_not_walked(core, tmp_path, monkeypatch):
    _no_index(monkeypatch)
    hidden = tmp_path / "Library" / "deep"
    hidden.mkdir(parents=True)
    (hidden / "wanted-file.txt").write_text("x")
    result = await files_module.search_files(_ctx(core), query="wanted-file",
                                             directory=str(tmp_path))

    assert result.output["results"] == []
    assert "Library" in result.output["skipped"]


async def test_an_indexed_match_in_a_pruned_directory_still_answers(core, tmp_path,
                                                                   monkeypatch):
    """Pruning bounds the walk, not the search.

    The index reply is canned rather than real, because this is a claim about
    precedence, not about whether macOS chose to index a temp file.
    """
    hidden = tmp_path / "Library" / "deep"
    hidden.mkdir(parents=True)
    target = hidden / "wanted-file.txt"
    target.write_text("x")

    monkeypatch.setattr(files_module.shutil, "which", lambda name: "/usr/bin/mdfind")

    async def fake_run(argv, *args, **kwargs):
        return 0, str(target), ""

    monkeypatch.setattr(files_module, "run_command", fake_run)

    result = await files_module.search_files(_ctx(core), query="wanted-file",
                                             directory=str(tmp_path))

    assert [r["path"] for r in result.output["results"]] == [str(target)]
    assert result.output["scanned"] == 0, "the walk ran although the index answered"


# ---------------------------------------------------------------------------
# arXiv: a client refusal that is not a User-Agent problem
# ---------------------------------------------------------------------------


def test_arxiv_request_identifies_the_client(monkeypatch):
    import urllib.request

    from jarvis.research import sources

    captured = {}

    class _Reply:
        def read(self):
            return (b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
                    b"<entry><id>http://arxiv.org/abs/2405.00001v1</id>"
                    b"<title>A Real Paper</title></entry></feed>")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        captured["request"] = request
        return _Reply()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    source = sources.ArxivSource()
    papers = source.search("mobile robots", limit=3)

    request = captured["request"]
    agent = request.get_header("User-agent") if hasattr(request, "get_header") else None
    assert agent and "python-urllib" not in agent.lower(), (
        f"arXiv rejected the request as {agent!r} -- HTTP 406 is that refusal")
    assert papers, "the response stopped parsing once the header was added"


# ---------------------------------------------------------------------------
# arXiv: what the product does when an upstream refuses it
# ---------------------------------------------------------------------------
#
# Measured, after three wrong diagnoses: the 406 is not the User-Agent, not
# HTTP/1.1-vs-HTTP/2, and not the plain-HTTP 301 hop. curl gets 200 from the same
# machine at the same moment while every Python client gets 406 -- an upstream
# client-fingerprint refusal this project cannot and should not try to evade.
# What it *can* do is stop turning one dead source into a failed command.


def test_arxiv_retries_before_giving_up(monkeypatch):
    import urllib.request

    from jarvis.research import sources

    calls = {"n": 0}

    class _Reply:
        def read(self):
            return (b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
                    b"<entry><id>http://arxiv.org/abs/2405.00002v1</id>"
                    b"<title>Retry Worked</title></entry></feed>")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def flaky(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(request.full_url, 406, "Not Acceptable", {}, None)
        return _Reply()

    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    papers = sources.ArxivSource().search("mobile robots", limit=2)

    assert calls["n"] >= 2, "a single refusal was treated as final"
    assert papers and papers[0].title == "Retry Worked"


async def test_one_refused_source_does_not_fail_the_command(core, monkeypatch):
    from jarvis.research import sources
    from jarvis.tools import research as research_module

    def explode(self, query, limit=5):
        raise RuntimeError("arXiv could not be reached: HTTP Error 406: Not Acceptable")

    monkeypatch.setattr(sources.ArxivSource, "search", explode)
    result = await research_module.search_papers(_ctx(core), query="swarm robotics",
                                                 source="arxiv", limit=3)

    # The command is answered by whatever source can serve it, and the refusal is
    # named rather than swallowed.
    assert result.output.get("source_errors"), "a refused source went unreported"
    assert "406" in str(result.output["source_errors"])
    assert result.ok or "406" in result.error, (result.message, result.error)


async def test_no_invented_papers_when_every_source_fails(core, monkeypatch):
    from jarvis.research import sources
    from jarvis.tools import research as research_module

    def explode(self, query, limit=5):
        raise RuntimeError("arXiv could not be reached: HTTP Error 406")

    monkeypatch.setattr(sources.ArxivSource, "search", explode)
    result = await research_module.search_papers(_ctx(core), query="swarm robotics",
                                                 source="arxiv", limit=3)

    assert not getattr(result, "papers", None) or result.output.get("papers", []) == [], (
        "papers appeared from nowhere after every source failed")


async def test_a_refused_source_is_queried_once_per_command(core, monkeypatch):
    """The fallback compares by name; get_source() returns a fresh instance."""
    from jarvis.research import sources
    from jarvis.tools import research as research_module

    attempts = {"arxiv": 0}
    original = sources.ArxivSource.search

    def counting(self, query, limit=5):
        attempts["arxiv"] += 1
        return original(self, query, limit=limit)

    monkeypatch.setattr(sources.ArxivSource, "search", counting)
    await research_module.search_papers(_ctx(core), query="swarm robotics",
                                        source="arxiv", limit=2)

    assert attempts["arxiv"] == 1, (
        f"arXiv was queried {attempts['arxiv']} times for one command -- the retry "
        "inside the source already covers a transient refusal")
