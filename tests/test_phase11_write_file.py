"""Phase 11: write_file — the first tool built against the real-execution contract.

The catalogue could copy, move and delete files but could not create or modify
one, so no real end-to-end "do something to my computer" scenario was possible.
These tests assert the contract the brief requires: a write is only reported
verified when the filesystem says so afterwards.
"""

from __future__ import annotations

import asyncio

import pytest
from jarvis.core.models import PlannedAction, PolicyDecision, Verification
from jarvis.tools.base import ToolContext
from jarvis.tools.files import write_file


@pytest.fixture()
def ctx(core) -> ToolContext:
    return ToolContext(settings=core.settings, apps=core.apps, devices=core.devices,
                       memory=core.memory, graph=core.graph, core=core)


def _sandbox(tmp_area):
    box = tmp_area / "JARVIS_REAL_TEST"
    box.mkdir(exist_ok=True)
    (box / "nested").mkdir(exist_ok=True)
    return box


# ---------------------------------------------------------------- create

async def test_create_reports_verified_and_file_is_really_there(ctx, tmp_area):
    box = _sandbox(tmp_area)
    target = box / "hello.txt"

    result = await write_file(ctx, path=str(target), content="JARVIS REAL TEST\n")

    assert result.ok
    assert result.verification is Verification.SUCCESS
    assert result.verified is True
    assert target.read_text() == "JARVIS REAL TEST\n"   # independent read
    assert result.output["created"] is True
    assert "Created hello.txt" in result.message


async def test_append_extends_and_verifies(ctx, tmp_area):
    box = _sandbox(tmp_area)
    target = box / "hello.txt"
    target.write_text("JARVIS REAL TEST\n")

    result = await write_file(ctx, path=str(target),
                              content="REAL EXECUTION VERIFIED\n", mode="append")

    assert result.verified is True
    assert target.read_text() == "JARVIS REAL TEST\nREAL EXECUTION VERIFIED\n"
    assert result.output["created"] is False
    assert "Appended" in result.message


async def test_overwrite_replaces_and_reports_updated(ctx, tmp_area):
    box = _sandbox(tmp_area)
    target = box / "hello.txt"
    target.write_text("old contents\n")

    result = await write_file(ctx, path=str(target), content="new\n")

    assert result.verified is True
    assert target.read_text() == "new\n"
    assert "Updated" in result.message
    assert result.output["created"] is False


# ---------------------------------------------------------------- failure paths

async def test_missing_path_is_a_failure_not_a_silent_success(ctx, tmp_area):
    result = await write_file(ctx, content="x")
    assert result.ok is False
    assert result.verification is Verification.FAILED


async def test_unknown_mode_refused(ctx, tmp_area):
    box = _sandbox(tmp_area)
    result = await write_file(ctx, path=str(box / "a.txt"), content="x", mode="implode")
    assert result.ok is False
    assert "overwrite" in result.error and "append" in result.error
    assert not (box / "a.txt").exists()


async def test_creating_inside_a_protected_location_is_refused(ctx):
    from pathlib import Path

    target = Path.home() / ".ssh" / "authorized_keys_phase11_probe"
    result = await write_file(ctx, path=str(target), content="x")

    assert result.ok is False
    assert "protected" in result.error.lower()
    assert not target.exists(), "a protected path was written to anyway"


async def test_refuses_to_write_over_a_directory(ctx, tmp_area):
    box = _sandbox(tmp_area)
    result = await write_file(ctx, path=str(box / "nested"), content="x")
    assert result.ok is False
    assert "folder" in result.summary.lower()


async def test_append_to_a_missing_file_creates_it(ctx, tmp_area):
    box = _sandbox(tmp_area)
    target = box / "new.txt"
    result = await write_file(ctx, path=str(target), content="first\n", mode="append")
    assert result.verified is True
    assert target.read_text() == "first\n"


# ------------------------------------------------- verification can be withheld

async def test_unreadable_result_is_reported_unverified_not_success(
    ctx, tmp_area, monkeypatch
):
    """If the disk cannot be read back, the tool must not claim success.

    This is the exact defect class Phase 9 found throughout the system: a call
    returning without error was recorded as the effect having happened.
    """
    box = _sandbox(tmp_area)
    target = box / "sneaky.txt"
    calls = {"n": 0}

    def broken_read(self):
        calls["n"] += 1
        raise OSError("simulated I/O error")

    monkeypatch.setattr(type(target), "read_bytes", broken_read)
    result = await write_file(ctx, path=str(target), content="x")
    monkeypatch.undo()

    assert calls["n"] >= 1, "the tool never tried to read the file back"
    assert target.exists(), "the write itself should still have happened"
    assert result.verified is False, "a write that could not be checked was called verified"
    assert result.verification is Verification.UNKNOWN
    assert "could not read it back" in result.message


# ------------------------------------------------------ wired into the engine

def test_tool_is_registered_with_the_expected_policy(core):
    spec = core.tools.get("write_file")
    assert spec is not None
    assert spec.requires_confirmation is True, "writing to disk must be confirm-gated"
    assert int(spec.risk) == 2
    verdict = core.policy.evaluate(PlannedAction(
        step_id=1, tool="write_file",
        params={"path": "/tmp/jarvis-p11-x.txt", "content": "x"}, device="laptop_main"))
    assert verdict.decision is PolicyDecision.CONFIRM


def test_laptop_agent_owns_it(core):
    agent = next(a for a in core.agents.values() if a.name == "laptop")
    assert agent.owns("write_file")
