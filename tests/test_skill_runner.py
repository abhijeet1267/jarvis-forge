"""Running a stored skill: authorized, executed as a mission, verified, measured.

The interesting property is that a skill is a *plan*, not a capability. These
tests pin that a skill goes through the same gate and verifiers as any other
mission, and that a run with no authorization or no independent check cannot
report success.
"""

from __future__ import annotations

import pytest

from jarvis.core.skill_runner import SkillRunError, run_skill
from jarvis.core.skills import SkillManifest, SkillStore


@pytest.fixture()
def store(tmp_path):
    return SkillStore(path=tmp_path / "skills.json")


@pytest.fixture()
def missions():
    from jarvis.core.missions import MissionService
    from jarvis.db.database import get_db

    return MissionService(get_db())


def _allow(step, mission):
    return {"allowed": True, "reason": "inside the skill's declared scope"}


def _manifest(tmp_path, **overrides):
    target = tmp_path / "out.txt"
    data = {
        "skill_id": "write-note",
        "name": "Write a note",
        "description": "writes a short note",
        "steps": [{"tool": "write_file", "params": {"path": str(target)}}],
        "required_capabilities": ["write_file"],
        "required_scope": [str(tmp_path)],
        "verification": [{"condition_id": "C1", "kind": "file_contains",
                          "path": str(target), "needle": "note"}],
        "supported_platforms": ["macos"],
        "risk": 1,
    }
    data.update(overrides)
    return SkillManifest.from_dict(data), target


# -- the honest path --------------------------------------------------------

@pytest.mark.asyncio
async def test_a_skill_runs_as_a_real_mission_and_is_verified(store, missions, tmp_path):
    manifest, target = _manifest(tmp_path)
    store.save(manifest)

    def write(step, mission):
        target.write_text("this is a note\n", encoding="utf-8")

        class _Ok:
            ok = True
            message = "wrote the note"
        return _Ok()

    result = await run_skill(
        "write-note", store=store, mission_service=missions,
        execute_step=write, permission=_allow,
        available_capabilities=["write_file"], allowed_scope=[str(tmp_path)])

    assert result["state"] == "completed"
    assert result["verdict"] == "verified"
    assert target.read_text(encoding="utf-8") == "this is a note\n"
    # The run is recorded, so reliability reflects something real.
    assert result["reliability"]["verified_successes"] == 1


@pytest.mark.asyncio
async def test_a_skill_that_claims_success_but_did_nothing_is_not_verified(
        store, missions, tmp_path):
    """The verifier reads the file; the step's message is not evidence."""
    manifest, target = _manifest(tmp_path)
    store.save(manifest)

    def pretend(step, mission):
        class _Ok:
            ok = True
            message = "wrote the note"     # it did not
        return _Ok()

    result = await run_skill(
        "write-note", store=store, mission_service=missions,
        execute_step=pretend, permission=_allow,
        available_capabilities=["write_file"], allowed_scope=[str(tmp_path)])

    assert result["verdict"] == "failed"
    assert result["state"] != "completed"
    assert result["reliability"]["failures"] == 1
    assert not target.exists()


# -- refusals ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_disabled_skill_does_not_run(store, missions, tmp_path):
    manifest, _ = _manifest(tmp_path)
    store.save(manifest)
    store.disable("write-note")

    with pytest.raises(SkillRunError) as excinfo:
        await run_skill("write-note", store=store, mission_service=missions,
                        execute_step=lambda s, m: None, permission=_allow,
                        available_capabilities=["write_file"],
                        allowed_scope=[str(tmp_path)])
    assert "not runnable" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_skill_needs_a_gate_and_an_executor(store, missions, tmp_path):
    """A skill runner with no gate would be an automation path around policy."""
    manifest, _ = _manifest(tmp_path)
    store.save(manifest)

    with pytest.raises(SkillRunError) as excinfo:
        await run_skill("write-note", store=store, mission_service=missions)
    assert "permission gate" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_capability_the_caller_does_not_have_is_refused(store, missions, tmp_path):
    manifest, _ = _manifest(tmp_path)
    store.save(manifest)

    with pytest.raises(SkillRunError) as excinfo:
        await run_skill("write-note", store=store, mission_service=missions,
                        execute_step=lambda s, m: None, permission=_allow,
                        available_capabilities=[], allowed_scope=[str(tmp_path)])
    assert "write_file" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_scope_the_caller_does_not_cover_is_refused(store, missions, tmp_path):
    manifest, _ = _manifest(tmp_path)
    store.save(manifest)

    with pytest.raises(SkillRunError) as excinfo:
        await run_skill("write-note", store=store, mission_service=missions,
                        execute_step=lambda s, m: None, permission=_allow,
                        available_capabilities=["write_file"],
                        allowed_scope=["/somewhere/else"])
    assert "scope" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_an_unsupported_platform_is_refused(store, missions, tmp_path):
    manifest, _ = _manifest(tmp_path, supported_platforms=["linux"])
    store.save(manifest)

    with pytest.raises(SkillRunError) as excinfo:
        await run_skill("write-note", store=store, mission_service=missions,
                        execute_step=lambda s, m: None, permission=_allow,
                        available_capabilities=["write_file"],
                        allowed_scope=[str(tmp_path)])
    assert "supports" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_precondition_the_system_cannot_evaluate_is_not_assumed_met(
        store, missions, tmp_path):
    """Assuming an unchecked precondition is inventing safety."""
    manifest, _ = _manifest(tmp_path, preconditions=["the database is healthy"])
    store.save(manifest)

    with pytest.raises(SkillRunError) as excinfo:
        await run_skill("write-note", store=store, mission_service=missions,
                        execute_step=lambda s, m: None, permission=_allow,
                        available_capabilities=["write_file"],
                        allowed_scope=[str(tmp_path)])
    assert "cannot be evaluated" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_path_precondition_is_actually_checked(store, missions, tmp_path):
    missing = tmp_path / "not-there"
    manifest, _ = _manifest(tmp_path, preconditions=[f"path exists: {missing}"])
    store.save(manifest)

    with pytest.raises(SkillRunError) as excinfo:
        await run_skill("write-note", store=store, mission_service=missions,
                        execute_step=lambda s, m: None, permission=_allow,
                        available_capabilities=["write_file"],
                        allowed_scope=[str(tmp_path)])
    assert "does not exist" in str(excinfo.value)


# -- reliability honesty ----------------------------------------------------

@pytest.mark.asyncio
async def test_an_unknown_check_kind_reports_unknown_not_verified(store, missions, tmp_path):
    target = tmp_path / "out.txt"
    manifest, _ = _manifest(
        tmp_path,
        verification=[{"condition_id": "C1", "kind": "telepathy",
                       "path": str(target)}])
    store.save(manifest)

    def write(step, mission):
        target.write_text("note\n", encoding="utf-8")

        class _Ok:
            ok = True
            message = "wrote"
        return _Ok()

    result = await run_skill(
        "write-note", store=store, mission_service=missions,
        execute_step=write, permission=_allow,
        available_capabilities=["write_file"], allowed_scope=[str(tmp_path)])

    assert result["verdict"] != "verified"
    assert result["state"] != "completed"


def test_a_skill_never_run_reports_no_success_rate(store, tmp_path):
    manifest, _ = _manifest(tmp_path)
    store.save(manifest)
    reliability = store.reliability("write-note")
    assert reliability["success_rate"] is None

# -- a skill run has an identity that approval resumes ---------------------

@pytest.mark.asyncio
async def test_a_parked_run_resumes_the_same_mission_after_approval(store, missions, tmp_path):
    """The defect this fixes: approval must reach the run that needed it.

    Originally every invocation created a new mission, so approving the parked
    one could never resume it -- the run needing authorisation and the run that
    executed were different missions. Passing `run_id` back continues the same
    mission.
    """
    manifest, target = _manifest(tmp_path)
    store.save(manifest)

    def needs_confirm(step, mission):
        return {"allowed": False, "decision": "CONFIRM",
                "reason": "writing a file needs approval",
                "confirmation_id": "CONF-SKILL"}

    def write(step, mission):
        target.write_text("this is a note\n", encoding="utf-8")

        class _Ok:
            ok = True
            message = "wrote"
        return _Ok()

    parked = await run_skill(
        "write-note", store=store, mission_service=missions,
        execute_step=write, permission=needs_confirm,
        available_capabilities=["write_file"], allowed_scope=[str(tmp_path)])

    assert parked["state"] == "awaiting_approval"
    run_id = parked["run_id"]
    assert run_id, "a parked run must expose an identity to resume"

    def allow(step, mission):
        return {"allowed": False, "decision": "CONFIRM",
                "reason": "writing a file needs approval",
                "confirmation_id": "CONF-SKILL"}

    resumed = await run_skill(
        "write-note", store=store, mission_service=missions,
        execute_step=write, permission=allow,
        approval={"confirmation_id": "CONF-SKILL"},
        run_id=run_id,
        available_capabilities=["write_file"], allowed_scope=[str(tmp_path)])

    assert resumed["mission_id"] == parked["mission_id"], (
        "approval resumed a different mission than the one that was parked")
    assert resumed["state"] == "completed"
    assert resumed["verdict"] == "verified"
    assert target.read_text(encoding="utf-8") == "this is a note\n"


@pytest.mark.asyncio
async def test_a_wrong_approval_does_not_resume_the_parked_run(store, missions, tmp_path):
    manifest, target = _manifest(tmp_path)
    store.save(manifest)

    def needs_confirm(step, mission):
        return {"allowed": False, "decision": "CONFIRM",
                "reason": "needs approval", "confirmation_id": "CONF-RIGHT"}

    def write(step, mission):
        target.write_text("note\n", encoding="utf-8")

        class _Ok:
            ok = True
            message = "wrote"
        return _Ok()

    parked = await run_skill(
        "write-note", store=store, mission_service=missions,
        execute_step=write, permission=needs_confirm,
        available_capabilities=["write_file"], allowed_scope=[str(tmp_path)])

    resumed = await run_skill(
        "write-note", store=store, mission_service=missions,
        execute_step=write, permission=needs_confirm,
        approval={"confirmation_id": "CONF-WRONG"},
        run_id=parked["run_id"],
        available_capabilities=["write_file"], allowed_scope=[str(tmp_path)])

    assert resumed["state"] == "awaiting_approval", (
        "an approval for a different confirmation must not resume this run")
    assert not target.exists()
