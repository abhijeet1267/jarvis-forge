"""Demo A: a real mission that edits a disposable project, then proves the edit.

This is the end-to-end path the directive calls Demo A, exercised against a
temporary directory so no real user file is touched:

    request -> mission contract -> plan -> gate -> execute -> independent verify

The point is the last step. The mission completes only because a verifier
re-read the file from disk and found the change; the same run with the write
removed reports PARTIALLY_COMPLETED and a verdict of ``failed``. Both halves are
asserted here, so the demo cannot pass by accident.

Run:  .venv/bin/python -m pytest tests/test_demo_a_file_mission.py -q
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from jarvis.core.mission_runner import MissionRunner, Verifiers
from jarvis.core.missions import MissionState


@pytest.fixture()
def service():
    from jarvis.core.missions import MissionService
    from jarvis.db.database import get_db

    return MissionService(get_db())


def _sample_project(root: Path) -> Path:
    """A disposable project whose README is genuinely out of date."""
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    readme = root / "README.md"
    readme.write_text("# Sample\n\nRun it.\n", encoding="utf-8")
    return readme


@pytest.mark.asyncio
async def test_demo_a_edits_a_real_file_and_proves_it(service, tmp_path):
    readme = _sample_project(tmp_path)
    before_digest = hashlib.sha256(readme.read_bytes()).hexdigest()

    def update_readme(step, mission):
        """The step's job: document the flag, the way a real tool would."""
        readme.write_text("# Sample\n\nRun it with --fast.\n", encoding="utf-8")

        class _Done:
            ok = True
            message = "updated README.md"
        return _Done()

    def allow(step, mission):
        return {"allowed": True, "reason": "inside the mission's file scope"}

    mission = service.create(
        "update the sample project's documentation",
        scope={"paths": [str(tmp_path)]},
        conditions=[{"statement": "README documents the --fast flag",
                     "verifier": "file content"}],
        steps=[{"title": "update README.md", "tool": "write_file",
                "params": {"path": str(readme)}}],
    )

    runner = MissionRunner(service, execute_step=update_readme, permission=allow)
    result = await runner.run(
        mission.mission_id,
        verifiers={"C1": Verifiers.file_contains(str(readme), "--fast", "C1")},
    )

    # The mission completed, and for the right reason.
    assert result.state == MissionState.COMPLETED.value
    assert result.verdict() == "verified"

    # The world actually changed.
    after_digest = hashlib.sha256(readme.read_bytes()).hexdigest()
    assert after_digest != before_digest, "the file was not really written"
    assert "--fast" in readme.read_text(encoding="utf-8")

    # The evidence names what was observed, not what the step claimed.
    observation = result.conditions[0].observation
    assert "README.md" in observation or "Sample" in observation
    assert "sha256:" in observation, (
        "verification must describe the artefact it read, not the step's message")


@pytest.mark.asyncio
async def test_demo_a_without_the_write_is_reported_as_failed(service, tmp_path):
    """The control: same mission, the step lies, verification catches it.

    Without this half, a passing test above would not prove the verifier was
    doing anything.
    """
    readme = _sample_project(tmp_path)

    def do_nothing(step, mission):
        class _Done:
            ok = True
            message = "updated README.md"   # it did not
        return _Done()

    def allow(step, mission):
        return {"allowed": True, "reason": "allowed"}

    mission = service.create(
        "update the sample project's documentation",
        conditions=[{"statement": "README documents the --fast flag",
                     "verifier": "file content"}],
        steps=[{"title": "update README.md", "tool": "write_file"}],
    )

    runner = MissionRunner(service, execute_step=do_nothing, permission=allow)
    result = await runner.run(
        mission.mission_id,
        verifiers={"C1": Verifiers.file_contains(str(readme), "--fast", "C1")},
    )

    assert result.state == MissionState.PARTIALLY_COMPLETED.value
    assert result.verdict() == "failed"
    assert result.conditions[0].status == "failed"


@pytest.mark.asyncio
async def test_demo_a_out_of_scope_write_is_refused_and_touches_nothing(
        service, tmp_path):
    """Demo E's shape, on the same path: refusal changes no file."""
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "keep.txt"
    victim.write_text("do not touch", encoding="utf-8")

    def should_not_run(step, mission):
        victim.write_text("clobbered", encoding="utf-8")

        class _Done:
            ok = True
            message = "wrote"
        return _Done()

    def refuse(step, mission):
        return {"allowed": False,
                "reason": f"{victim} is outside the mission scope"}

    mission = service.create(
        "edit a file outside the scope",
        conditions=[{"statement": "the file is unchanged", "verifier": "file content"}],
        steps=[{"title": "write outside", "tool": "write_file"}],
    )

    runner = MissionRunner(service, execute_step=should_not_run, permission=refuse)
    result = await runner.run(mission.mission_id)

    assert result.state == MissionState.BLOCKED.value
    assert victim.read_text(encoding="utf-8") == "do not touch"
    assert "outside the mission scope" in result.reason