"""Phase 23: a deletion that stops reporting more than it did.

Three defects were found in Phase 10, described precisely, and parked with
``xfail`` because the fix belonged to a file that phase did not own. Each names its
root cause in the marker reason, which is why they are actionable now rather than
mysterious:

* ``POST /memory/{id}/forget`` (``jarvis/api/app.py``) has its own handler and returns
  a hardcoded ``{'deleted': True}`` -- never the tool's honest report, so the HTTP
  surface claims completeness the tool refuses to claim.
* ``JarvisCore._remember_outcome()`` stores a session echo of *every* command with
  actions, including the recall used to check a deletion, so verifying a forget
  writes another copy of the thing just forgotten.
* ``RemoteEmbedder._cache`` is keyed by raw text with no eviction API, so a deleted
  fact stays resident in the process after every row holding it is gone.

Plus the fourth, carried from Phase 22 as 22-H: ``command_robot_motion`` *infers* a
robot when the user named none, which is the same silent-choice shape the home
gateway was just fixed for -- on physical hardware.

Written test-first; the four that describe existing defects fail before the code
they describe exists.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

import pytest
from jarvis.core.models import CommandRequest
from jarvis.core.orchestrator import JarvisCore
from jarvis.db.database import get_db
from jarvis.memory.embeddings import HashingEmbedder, RemoteEmbedder, get_embedder
from jarvis.memory.store import MemoryScope, MemoryStore
from jarvis.robotics.gateway import AMR_ACTIONS, ARM_ACTIONS, get_robot_gateway, reset_robot_gateway
from jarvis.tools.base import ToolContext
from jarvis.tools.memory_controls import forget_memory
from jarvis.tools.robotics import command_robot_motion


def _ctx(core: JarvisCore) -> ToolContext:
    return ToolContext(settings=core.settings, apps=core.apps, devices=core.devices,
                       memory=core.memory, graph=core.graph, core=core)


def _rows_holding(text: str, table: str = "memories") -> List[Dict[str, Any]]:
    db = get_db()
    columns = [c["name"] for c in db.query(f"PRAGMA table_info({table})")]
    where = " OR ".join(f"CAST({c} AS TEXT) LIKE ?" for c in columns)
    return [dict(r) for r in db.query(
        f"SELECT * FROM {table} WHERE {where}", tuple(f"%{text}%" for _ in columns))]


def _fact(tag: str) -> str:
    return f"my old car plate is {tag}{uuid.uuid4().hex[:6].upper()}"


async def _speak_remember(core: JarvisCore, fact: str) -> Dict[str, Any]:
    response = await core.handle(CommandRequest(text=f"remember that {fact}", source="test"))
    assert response.reply.startswith("Got it. I will remember:"), response.reply
    token = fact.rsplit(" ", 1)[1]
    rows = {r["id"]: r for r in _rows_holding(token, "memories")}
    named = [r for r in rows.values() if r["content"] == fact]
    assert len(named) == 1, list(rows.values())
    return {"fact": fact, "token": token, "target": named[0], "all": rows}


@pytest.fixture()
def api_client():
    """An authenticated client: the HTTP forget route is a second, separate path."""
    import os

    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    client.headers.update({"X-JARVIS-Token": os.environ["JARVIS_API_TOKEN"]})
    return client


@pytest.fixture(autouse=True)
def _clean_gateways():
    """Both gateways are process-wide; hand each test a fresh one and leave it fresh."""
    reset_robot_gateway(None)
    yield
    reset_robot_gateway(None)


def _supports(robot, action: str) -> bool:
    return action in (AMR_ACTIONS if robot.kind == "amr" else ARM_ACTIONS)


# ---------------------------------------------------------------------------
# 23-A -- the HTTP forget route has to stop answering with a claim
# ---------------------------------------------------------------------------


async def test_the_http_forget_route_reports_what_the_tool_reports(api_client, core):
    stored = await _speak_remember(core, _fact("HTTP-"))
    token = stored["token"]

    response = api_client.post(f"/memory/{stored['target']['id']}/forget")

    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] is True
    assert _rows_holding(token, "memories"), "precondition: an echo still carries the fact"
    assert body.get("complete") is False, (
        "the route answered 'deleted: true' and stopped there, while the same deletion "
        "through the tool reports the copies it did not remove")
    assert body.get("report"), "the user should see the same sentence the tool tells them"


async def test_the_http_forget_route_audits_what_it_actually_did(api_client, core):
    stored = await _speak_remember(core, _fact("AUDIT-"))
    memory_id = stored["target"]["id"]
    before = get_db().query("SELECT COUNT(*) AS n FROM action_logs")[0]["n"]

    api_client.post(f"/memory/{memory_id}/forget")

    rows = [dict(r) for r in get_db().query(
        "SELECT * FROM action_logs ORDER BY rowid DESC LIMIT 5")]
    row = [r for r in rows if str(r.get("target")) == memory_id]
    assert row, "a destructive HTTP operation must be in the ledger it shares with the tool path"
    assert row[0]["result"] != "success", (
        f"the ledger records {row[0]['result']!r} for a deletion that demonstrably left "
        "copies behind; the tool knows better and the route does not say so")
    assert get_db().query("SELECT COUNT(*) AS n FROM action_logs")[0]["n"] > before


async def test_forgetting_something_that_is_not_there_says_so(api_client):
    response = api_client.post("/memory/no-such-id/forget")

    assert response.status_code == 404, (
        f"a delete that matched no row answered {response.status_code} "
        f"{response.json()} -- a deletion that never happened")


# ---------------------------------------------------------------------------
# 23-B -- checking a deletion must not re-create the thing deleted
# ---------------------------------------------------------------------------


async def test_a_recall_does_not_store_another_activity_row(core):
    before = get_db().query("SELECT COUNT(*) AS n FROM memories")[0]["n"]

    response = await core.handle(
        CommandRequest(text="Jarvis, what do you remember about the garage door?", source="test"))

    assert any(a.tool == "search_memory" for a in response.actions), response.reply
    after = get_db().query("SELECT COUNT(*) AS n FROM memories")[0]["n"]
    assert after == before, (
        f"a command whose only effect was reading memory wrote {after - before} new "
        "memories -- looking at the activity log should not add to it")


async def test_a_command_that_changes_something_still_echoes(core):
    """The activity log is a feature; only memory reads are excluded."""
    before = get_db().query("SELECT COUNT(*) AS n FROM memories")[0]["n"]
    await core.handle(
        CommandRequest(text="Jarvis, remember that the spare key is in the drawer", source="test"))

    assert get_db().query("SELECT COUNT(*) AS n FROM memories")[0]["n"] > before


async def test_the_forgotten_fact_stays_gone_after_verifying_it(core):
    stored = await _speak_remember(core, _fact("STAY-"))
    token = stored["token"]
    await forget_memory(_ctx(core), memory_id=stored["target"]["id"])

    count_after_forget = len(_rows_holding(token, "memories"))
    await core.handle(CommandRequest(text=f"do you remember {token}?", source="test"))

    assert len(_rows_holding(token, "memories")) == count_after_forget, (
        "the recall used to check the deletion wrote another copy, so the fact is back")


# ---------------------------------------------------------------------------
# 23-C -- deletion has to reach the in-process cache too
# ---------------------------------------------------------------------------


def test_deleting_a_memory_drops_its_embedding_cache_entry():
    store = MemoryStore()
    fact = _fact("CACHE-")
    item = store.remember(fact, scope=MemoryScope.LONG_TERM, index=False)

    embedder = get_embedder()
    assert isinstance(embedder, HashingEmbedder), "the suite must not run a remote embedder"

    remote = RemoteEmbedder()
    remote._cache[fact] = [0.0]  # exactly what RemoteEmbedder.embed() leaves behind
    store.forget(item.id)

    assert not any(fact in str(key) for key in remote._cache), (
        "the row is gone but the text is still resident, in a cache with no eviction API")


def test_the_local_embedder_reports_that_it_holds_no_text():
    embedder = HashingEmbedder()
    assert hasattr(embedder, "forget_text"), (
        "a deletion path needs one eviction call to make for every embedder, including "
        "the one that keeps nothing")
    embedder.forget_text("anything")  # must be a no-op, not an AttributeError


# ---------------------------------------------------------------------------
# 23-D / 22-H -- an unnamed robot is a question, not a default
# ---------------------------------------------------------------------------


async def test_a_motion_two_robots_can_do_asks_which_one(core):
    """The inference was the defect, not the motion.

    ``home`` and ``stop`` are in both action sets, so with a fleet of two an
    unnamed "home" is a real choice -- and the old code answered it with
    ``gw.resolve("amr")``, sending a command to a machine the user never named.
    """
    gw = get_robot_gateway()
    shared = [a for a in AMR_ACTIONS if a in ARM_ACTIONS]
    assert shared, "the fleet needs a genuinely ambiguous action for this to mean anything"
    assert len(gw.robots) > 1, "one robot would leave nothing to ask about"

    result = await command_robot_motion(_ctx(core), robot="", action=shared[0])

    assert not result.ok, f"an ambiguous robot command executed anyway: {result.error}"
    assert "which" in result.error.lower(), result.error
    for name in (r.name for r in gw.robots):
        assert name.split()[0].lower() in result.error.lower(), (
            f"{name} is in the fleet but was not offered")


async def test_a_motion_only_one_robot_can_do_still_proceeds(core):
    """A choice the fleet cannot make is not a question worth asking."""
    gw = get_robot_gateway()
    amr_only = [a for a in AMR_ACTIONS if a not in ARM_ACTIONS][0]
    capable = [r for r in gw.robots if _supports(r, amr_only)]
    assert len(capable) == 1, (
        f"{amr_only!r} is doable by {len(capable)} robots, so it is not the unambiguous "
        "case this test claims")

    result = await command_robot_motion(_ctx(core), robot="", action=amr_only)

    assert "which robot" not in (result.error or result.message).lower(), result.error


