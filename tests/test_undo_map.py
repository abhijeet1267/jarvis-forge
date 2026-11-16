"""Temporal Undo Map: a real undo for file effects, an honest "no" for the rest.

The property that matters is that an undo either restores the *actual* previous
bytes or refuses with a reason. A map that offered a button for a sent email, or
restored over somebody's newer edit, would be worse than no map -- it would
report success for work it silently destroyed or never did.
"""

from __future__ import annotations

import hashlib

import pytest

from jarvis.core.undo_map import (
    ALREADY_UNDONE,
    CONFLICTED,
    IRREVERSIBLE,
    REVERSIBLE,
    UNKNOWN,
    UndoError,
    UndoMap,
    UndoStore,
    classify,
)


@pytest.fixture()
def store(tmp_path):
    return UndoStore(root=tmp_path / "undo")


@pytest.fixture()
def undo_map(store):
    return UndoMap(store=store)


# -- what is reversible ------------------------------------------------------

def test_a_write_with_a_pre_image_is_reversible():
    state, why = classify("write_file", before_sha256="abc123")
    assert state == REVERSIBLE
    assert "previous bytes" in why


def test_a_sent_email_is_irreversible():
    state, why = classify("send_email")
    assert state == IRREVERSIBLE
    assert "cannot be un-sent" in why


def test_a_physical_action_is_irreversible():
    state, why = classify("unlock_car", risk="physical")
    assert state == IRREVERSIBLE
    assert "physical world" in why


def test_a_destructive_action_without_a_pre_image_is_irreversible():
    state, _ = classify("delete_file", risk="destructive")
    assert state == IRREVERSIBLE


def test_a_permanent_delete_with_a_pre_image_is_reversible():
    state, why = classify("delete_file", risk="destructive", before_sha256="deadbeef")
    assert state == REVERSIBLE
    assert "restored byte for byte" in why


def test_an_undo_entry_is_marked_as_a_reversal_not_an_unknown_effect():
    state, why = classify("undo")
    assert state == "undo"
    assert "reversal" in why


def test_an_unclassified_tool_is_unknown_not_optimistic():
    state, why = classify("some_new_tool")
    assert state == UNKNOWN
    assert "will not guess" in why


# -- capturing a real pre-image ----------------------------------------------

def test_capture_before_stores_the_bytes(tmp_path, store):
    target = tmp_path / "a.txt"
    target.write_text("original contents")
    digest = store.capture_before(str(target))
    assert digest == hashlib.sha256(b"original contents").hexdigest()
    assert store.get_blob(digest) == b"original contents"


def test_capture_before_a_missing_file_is_empty(store, tmp_path):
    assert store.capture_before(str(tmp_path / "nope.txt")) == ""


# -- the real undo -----------------------------------------------------------

def test_undo_restores_the_previous_bytes(tmp_path, undo_map):
    target = tmp_path / "note.txt"
    target.write_text("before")
    before = undo_map.store.capture_before(str(target))

    target.write_text("after")
    after = hashlib.sha256(b"after").hexdigest()
    entry = undo_map.store.record(tool="write_file", path=str(target),
                                  before_sha256=before, after_sha256=after)

    result = undo_map.undo(entry["entry_id"])
    assert target.read_text() == "before"
    assert "restored" in result["restored"]


def test_undo_refuses_after_the_file_changed(tmp_path, undo_map):
    """Undoing over somebody's newer edit would destroy work -- refused."""
    target = tmp_path / "note.txt"
    target.write_text("before")
    before = undo_map.store.capture_before(str(target))
    target.write_text("after")
    after = hashlib.sha256(b"after").hexdigest()
    entry = undo_map.store.record(tool="write_file", path=str(target),
                                  before_sha256=before, after_sha256=after)

    # Somebody edits it again after our effect.
    target.write_text("edited by the person")

    with pytest.raises(UndoError) as caught:
        undo_map.undo(entry["entry_id"])
    assert "changed since" in str(caught.value)
    assert target.read_text() == "edited by the person"


def test_undo_refuses_an_irreversible_entry(undo_map):
    entry = undo_map.store.record(tool="send_email", path="",
                                  undo_kind="none")
    with pytest.raises(UndoError) as caught:
        undo_map.undo(entry["entry_id"])
    assert "cannot be un-sent" in str(caught.value)


def test_undo_removes_a_created_file(tmp_path, undo_map):
    target = tmp_path / "created.txt"
    target.write_text("new file")
    entry = undo_map.store.record(tool="create_folder", path=str(target),
                                  undo_kind="remove")
    undo_map.undo(entry["entry_id"])
    assert not target.exists()


def test_undo_refuses_a_second_time(undo_map, tmp_path):
    target = tmp_path / "x.txt"
    target.write_text("a")
    before = undo_map.store.capture_before(str(target))
    target.write_text("b")
    entry = undo_map.store.record(tool="write_file", path=str(target),
                                  before_sha256=before,
                                  after_sha256=hashlib.sha256(b"b").hexdigest())
    undo_map.undo(entry["entry_id"])
    with pytest.raises(UndoError) as caught:
        undo_map.undo(entry["entry_id"])
    assert "already undone" in str(caught.value)


def test_undo_is_itself_recorded(tmp_path, undo_map):
    target = tmp_path / "x.txt"
    target.write_text("a")
    before = undo_map.store.capture_before(str(target))
    target.write_text("b")
    entry = undo_map.store.record(tool="write_file", path=str(target),
                                  before_sha256=before,
                                  after_sha256=hashlib.sha256(b"b").hexdigest())
    result = undo_map.undo(entry["entry_id"])
    undo_entry = undo_map.store.get(result["undo_entry"])
    assert undo_entry is not None
    assert undo_entry["tool"] == "undo"


def test_undo_refuses_when_the_pre_image_is_missing(tmp_path, undo_map):
    target = tmp_path / "x.txt"
    target.write_text("a")
    before = undo_map.store.capture_before(str(target))
    # Drop the blob to simulate a pruned store.
    (undo_map.store.blob_dir / before).unlink()
    target.write_text("b")
    entry = undo_map.store.record(tool="write_file", path=str(target),
                                  before_sha256=before,
                                  after_sha256=hashlib.sha256(b"b").hexdigest())
    with pytest.raises(UndoError) as caught:
        undo_map.undo(entry["entry_id"])
    assert "missing from the undo store" in str(caught.value)


# -- the timeline says the truth ---------------------------------------------

def test_the_timeline_marks_a_conflicted_entry(tmp_path, undo_map):
    target = tmp_path / "x.txt"
    target.write_text("a")
    before = undo_map.store.capture_before(str(target))
    target.write_text("b")
    after = hashlib.sha256(b"b").hexdigest()
    undo_map.store.record(tool="write_file", path=str(target),
                          before_sha256=before, after_sha256=after)
    target.write_text("changed again")
    rows = undo_map.timeline()
    assert rows[0]["state"] == CONFLICTED


def test_the_timeline_marks_an_undone_entry(tmp_path, undo_map):
    target = tmp_path / "y.txt"
    target.write_text("a")
    before = undo_map.store.capture_before(str(target))
    target.write_text("b")
    entry = undo_map.store.record(tool="write_file", path=str(target),
                                  before_sha256=before,
                                  after_sha256=hashlib.sha256(b"b").hexdigest())
    undo_map.undo(entry["entry_id"])
    rows = {r["entry_id"]: r for r in undo_map.timeline()}
    assert rows[entry["entry_id"]]["state"] == ALREADY_UNDONE


def test_the_summary_states_the_rule(undo_map):
    body = undo_map.summary()
    assert "reversible only when the bytes to restore exist" in body["note"]


# -- through the tool funnel -------------------------------------------------

def test_a_real_write_file_records_an_undoable_entry(monkeypatch, tmp_path):
    """The map is fed by `Agent.execute`, the single funnel, so a real
    `write_file` produces an entry whose undo restores the old bytes."""
    import asyncio

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    import jarvis.core.undo_map as mod

    mod._MAP = None
    target = tmp_path / "doc.txt"
    target.write_text("original")

    from jarvis.core.models import PlannedAction
    from jarvis.core.undo_map import get_undo_map
    from jarvis.registry.tools import ToolRegistry
    from jarvis.tools.base import ToolContext
    from jarvis.tools.files import SPECS as FILE_SPECS

    registry = ToolRegistry()
    for spec, handler in FILE_SPECS:
        registry.register(spec, handler)

    from jarvis.agents.base import build_agents

    agents = build_agents(registry)
    agent = agents["laptop"]
    action = PlannedAction(tool="write_file", device="laptop_main",
                           params={"path": str(target), "content": "replaced",
                                   "mode": "overwrite"})
    asyncio.run(agent.execute(action, ToolContext(device_id="laptop_main")))

    assert target.read_text() == "replaced"
    rows = get_undo_map().timeline()
    mine = [r for r in rows if r["tool"] == "write_file" and str(target) in r["path"]]
    assert mine, rows
    entry = mine[0]
    assert entry["state"] == REVERSIBLE

    get_undo_map().undo(entry["entry_id"])
    assert target.read_text() == "original"


# -- over the wire ------------------------------------------------------------

def test_the_timeline_is_served_over_http():
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}
    body = client.get("/undo", headers=headers).json()
    assert "entries" in body
    assert "counts" in body


def test_undoing_an_unknown_entry_is_a_409():
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}
    response = client.post("/undo/UND-does-not-exist", headers=headers)
    assert response.status_code == 409