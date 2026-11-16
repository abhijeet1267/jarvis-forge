"""The mission runner: gate, execute, then verify with something independent.

These tests exist to pin the distinction the whole feature rests on -- a step
that ran and reported success is not a mission that achieved its outcome. The
runner must reach COMPLETED only when an observer that does not trust the step
has checked the world.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from jarvis.core.mission_runner import MissionRunner, Verifiers
from jarvis.core.missions import ConditionStatus, MissionState


@pytest.fixture()
def service():
    from jarvis.core.missions import MissionService
    from jarvis.db.database import get_db

    return MissionService(get_db())


@pytest.fixture()
def workspace(tmp_path):
    return tmp_path


def _allow_all(step, mission):
    return {"allowed": True, "reason": "test gate"}


def _deny(step, mission):
    return {"allowed": False, "reason": "the path is outside the mission scope"}


class _Ok:
    ok = True
    message = "wrote the file"


class _Bad:
    ok = False
    message = "permission denied by the tool"


def _mission(service, *, conditions, steps):
    return service.create("test request", conditions=conditions, steps=steps)


# -- the honest path --------------------------------------------------------

@pytest.mark.asyncio
async def test_a_verified_mission_completes_and_its_evidence_names_what_was_seen(
        service, workspace):
    """A real write, then an independent read-back that confirms it."""
    target = workspace / "README.md"

    def write(step, mission):
        target.write_text("# Project\n\nUse the --fast flag.\n", encoding="utf-8")
        return _Ok()

    mission = _mission(
        service,
        conditions=[{"statement": "the README documents --fast",
                     "verifier": "file content"}],
        steps=[{"title": "update the README", "tool": "write_file"}],
    )

    runner = MissionRunner(service, execute_step=write, permission=_allow_all)
    done = await runner.run(
        mission.mission_id,
        verifiers={"C1": Verifiers.file_contains(str(target), "--fast", "C1")},
    )

    assert done.state == MissionState.COMPLETED.value
    assert done.verdict() == "verified"
    condition = done.conditions[0]
    assert condition.status == ConditionStatus.VERIFIED.value
    # The observation names the artefact and a hash, not "the write succeeded".
    assert "sha256:" in condition.observation


@pytest.mark.asyncio
async def test_a_step_that_reports_success_but_did_not_do_the_work_is_caught(
        service, workspace):
    """The headline case: the tool says OK, the world says otherwise."""
    target = workspace / "README.md"
    target.write_text("# Project\n", encoding="utf-8")

    def pretend(step, mission):
        return _Ok()  # claims it wrote the flag; does not touch the file

    mission = _mission(
        service,
        conditions=[{"statement": "the README documents --fast",
                     "verifier": "file content"}],
        steps=[{"title": "update the README", "tool": "write_file"}],
    )

    runner = MissionRunner(service, execute_step=pretend, permission=_allow_all)
    result = await runner.run(
        mission.mission_id,
        verifiers={"C1": Verifiers.file_contains(str(target), "--fast", "C1")},
    )

    assert result.state == MissionState.PARTIALLY_COMPLETED.value
    assert result.conditions[0].status == ConditionStatus.FAILED.value
    assert result.verdict() == "failed"
    assert result.verdict() != "verified", (
        "a successful tool message must never be able to complete a mission")


@pytest.mark.asyncio
async def test_a_condition_with_no_verifier_stays_unknown_not_verified(service):
    def work(step, mission):
        return _Ok()

    mission = _mission(
        service,
        conditions=[{"statement": "something happened", "verifier": "?"}],
        steps=[{"title": "do it", "tool": "read_file"}],
    )

    runner = MissionRunner(service, execute_step=work, permission=_allow_all)
    result = await runner.run(mission.mission_id)  # no verifiers supplied

    assert result.conditions[0].status == ConditionStatus.UNKNOWN.value
    assert result.state == MissionState.PARTIALLY_COMPLETED.value


# -- refusals ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_refused_step_blocks_the_mission_and_changes_nothing(service, workspace):
    target = workspace / "README.md"

    def should_not_run(step, mission):
        target.write_text("should not exist", encoding="utf-8")
        return _Ok()

    mission = _mission(
        service,
        conditions=[{"statement": "no write happened", "verifier": "file absent"}],
        steps=[{"title": "write outside scope", "tool": "write_file"}],
    )

    runner = MissionRunner(service, execute_step=should_not_run, permission=_deny)
    result = await runner.run(mission.mission_id)

    assert result.state == MissionState.BLOCKED.value
    assert not target.exists(), "a refused step must not have executed"
    assert "outside the mission scope" in result.reason


@pytest.mark.asyncio
async def test_a_runner_without_a_permission_engine_refuses_to_run(service):
    """No gate is not permission. Absence must fail closed."""
    def work(step, mission):
        return _Ok()

    mission = _mission(
        service,
        conditions=[{"statement": "x", "verifier": "y"}],
        steps=[{"title": "do it", "tool": "read_file"}],
    )

    runner = MissionRunner(service, execute_step=work)  # no permission
    result = await runner.run(mission.mission_id)

    assert result.state == MissionState.BLOCKED.value
    assert "no permission engine" in result.reason


@pytest.mark.asyncio
async def test_a_failed_step_fails_the_mission_with_the_tool_message(service):
    def fail(step, mission):
        return _Bad()

    mission = _mission(
        service,
        conditions=[{"statement": "x", "verifier": "y"}],
        steps=[{"title": "do it", "tool": "write_file"}],
    )

    runner = MissionRunner(service, execute_step=fail, permission=_allow_all)
    result = await runner.run(mission.mission_id)

    assert result.state == MissionState.FAILED.value
    assert "permission denied by the tool" in result.reason


# -- resume / idempotency ---------------------------------------------------

@pytest.mark.asyncio
async def test_a_completed_step_is_not_run_again_on_resume(service):
    """Resuming must not replay a side effect that already happened."""
    calls = []

    def count(step, mission):
        calls.append(step.step_id)
        return _Ok()

    mission = _mission(
        service,
        conditions=[{"statement": "x", "verifier": "y"}],
        steps=[{"title": "first", "tool": "read_file"},
               {"title": "second", "tool": "read_file"}],
    )
    # Pretend the first step already finished before the interruption.
    service.mark_step(mission.mission_id, "S1", status="done", detail="done earlier")

    runner = MissionRunner(service, execute_step=count, permission=_allow_all)
    await runner.run(mission.mission_id)

    assert calls == ["S2"], f"resume replayed completed work: {calls}"


# -- the verifiers themselves ----------------------------------------------

def test_file_contains_reports_verified_and_failed_from_real_bytes(workspace):
    target = workspace / "note.txt"
    target.write_text("hello world", encoding="utf-8")

    from jarvis.core.missions import Mission

    empty_mission = Mission(mission_id="M", request="", outcome="")
    ok = Verifiers.file_contains(str(target), "hello", "C1")(empty_mission)
    missing = Verifiers.file_contains(str(target), "goodbye", "C1")(empty_mission)

    assert ok.status == ConditionStatus.VERIFIED
    assert missing.status == ConditionStatus.FAILED


def test_a_missing_file_is_failed_not_unknown(workspace):
    from jarvis.core.missions import Mission

    result = Verifiers.file_contains(str(workspace / "nope.txt"), "x", "C1")(
        Mission(mission_id="M", request="", outcome=""))
    assert result.status == ConditionStatus.FAILED


def test_file_hash_matches_the_real_digest(workspace):
    target = workspace / "data.bin"
    target.write_bytes(b"\x00\x01\x02")
    digest = hashlib.sha256(b"\x00\x01\x02").hexdigest()

    from jarvis.core.missions import Mission

    mission = Mission(mission_id="M", request="", outcome="")
    good = Verifiers.file_hash(str(target), digest, "C1")(mission)
    wrong = Verifiers.file_hash(str(target), "0" * 64, "C1")(mission)

    assert good.status == ConditionStatus.VERIFIED
    assert wrong.status == ConditionStatus.FAILED


def test_a_verifier_that_cannot_observe_reports_unknown_never_verified():
    from jarvis.core.missions import Mission

    result = Verifiers.always_unknown("this system cannot observe it", "C1")(
        Mission(mission_id="M", request="", outcome=""))
    assert result.status == ConditionStatus.UNKNOWN
    assert result.status != ConditionStatus.VERIFIED

# -- the gate's decision vocabulary ---------------------------------------
#
# Two bugs found by running a mission live rather than only through the unit
# seams. Both were silent: the mission reported "blocked" or a bare "failed",
# which looks like policy or the tool refusing, not like this code.

def test_the_gate_decision_vocabulary_is_uppercase():
    """`PolicyDecision`'s values are EXECUTE/CONFIRM/BLOCK, not lowercase.

    The route adapter compared against "execute", so every allowed step read as
    denied. Pin the vocabulary the engine actually uses, so the comparison has a
    failing test if it drifts again.
    """
    from jarvis.core.models import PolicyDecision

    assert PolicyDecision.EXECUTE.value == "EXECUTE"
    assert PolicyDecision.CONFIRM.value == "CONFIRM"
    assert PolicyDecision.BLOCK.value == "BLOCK"


@pytest.mark.asyncio
async def test_a_failed_step_reports_the_tool_reason_not_a_bare_failed(service):
    """`ActionResult` carries the reason in `error`; the runner must read it."""
    class _Failed:
        ok = False
        message = ""
        error = "File is too large to read inline (349.8 KB)."

    def fail(step, mission):
        return _Failed()

    mission = _mission(
        service,
        conditions=[{"statement": "x", "verifier": "y"}],
        steps=[{"title": "read it", "tool": "read_file"}],
    )

    runner = MissionRunner(service, execute_step=fail, permission=_allow_all)
    result = await runner.run(mission.mission_id)

    assert result.state == MissionState.FAILED.value
    assert "too large" in result.reason, (
        "the tool's own explanation must reach the mission, not the word 'failed'")


def test_a_failed_audit_write_does_not_produce_a_verified_claim(service, workspace):
    """Evidence is what `verified` rests on; no evidence means no verified claim.

    A verifier can succeed and the audit write still fail (a full disk, a locked
    database). Recording the condition as verified then would leave a claim whose
    evidence reference points at nothing -- exactly the "looks like proof" state
    the contract exists to prevent.
    """
    import asyncio

    target = workspace / "f.txt"
    target.write_text("--fast\n", encoding="utf-8")

    class _BrokenAudit:
        def record(self, **kwargs):
            raise RuntimeError("the audit database is unavailable")

    def work(step, mission):
        class _Ok:
            ok = True
            message = "done"
        return _Ok()

    mission = _mission(
        service,
        conditions=[{"statement": "the file mentions --fast", "verifier": "file content"}],
        steps=[{"title": "touch", "tool": "read_file"}],
    )

    runner = MissionRunner(service, execute_step=work, permission=_allow_all,
                           audit=_BrokenAudit())
    result = asyncio.run(runner.run(
        mission.mission_id,
        verifiers={"C1": Verifiers.file_contains(str(target), "--fast", "C1")},
    ))

    assert result.conditions[0].status == ConditionStatus.UNKNOWN.value
    assert result.verdict() != "verified"
    assert "could not be recorded" in result.conditions[0].observation


# -- CONFIRM is an approval, not a permission ------------------------------

def _confirm_gate(step, mission):
    return {"allowed": False, "decision": "CONFIRM",
            "reason": "writing a file needs a person to approve it",
            "confirmation_id": "CONF-123"}


@pytest.mark.asyncio
async def test_confirm_parks_the_mission_awaiting_approval(service, workspace):
    """The runner must not treat CONFIRM as if it were allowed."""
    target = workspace / "f.txt"
    calls = []

    def work(step, mission):
        calls.append(step.step_id)
        target.write_text("written", encoding="utf-8")

        class _Ok:
            ok = True
            message = "wrote"
        return _Ok()

    mission = _mission(
        service,
        conditions=[{"statement": "the file exists", "verifier": "file"}],
        steps=[{"title": "write it", "tool": "write_file"}],
    )

    runner = MissionRunner(service, execute_step=work, permission=_confirm_gate)
    result = await runner.run(mission.mission_id)

    assert result.state == MissionState.AWAITING_APPROVAL.value
    assert calls == [], "a step awaiting approval must not have executed"
    assert not target.exists()
    assert "CONF-123" in result.reason


@pytest.mark.asyncio
async def test_re_running_without_approval_leaves_it_parked(service, workspace):
    """Nothing resumes itself; an approval has to be supplied."""
    mission = _mission(
        service,
        conditions=[{"statement": "x", "verifier": "y"}],
        steps=[{"title": "write it", "tool": "write_file"}],
    )
    runner = MissionRunner(service, execute_step=lambda s, m: None,
                           permission=_confirm_gate)
    await runner.run(mission.mission_id)

    again = await runner.run(mission.mission_id)          # no approval
    assert again.state == MissionState.AWAITING_APPROVAL.value


@pytest.mark.asyncio
async def test_a_supplied_approval_lets_the_mission_continue(service, workspace):
    target = workspace / "f.txt"

    def work(step, mission):
        target.write_text("--yes", encoding="utf-8")

        class _Ok:
            ok = True
            message = "wrote"
        return _Ok()

    mission = _mission(
        service,
        conditions=[{"statement": "the file mentions --yes", "verifier": "file content"}],
        steps=[{"title": "write it", "tool": "write_file"}],
    )
    runner = MissionRunner(service, execute_step=work, permission=_confirm_gate)
    parked = await runner.run(mission.mission_id)
    assert parked.state == MissionState.AWAITING_APPROVAL.value

    resumed = await runner.run(
        mission.mission_id,
        verifiers={"C1": Verifiers.file_contains(str(target), "--yes", "C1")},
        approval={"confirmation_id": "CONF-123"})
    assert resumed.state in (MissionState.COMPLETED.value,
                             MissionState.PARTIALLY_COMPLETED.value)
    assert target.exists()


# -- lease enforcement -----------------------------------------------------

@pytest.fixture()
def leases():
    from jarvis.security.leases import LeaseService
    from jarvis.db.database import get_db

    return LeaseService(get_db())


def _lease_for(leases, mission, *, scope, capability="write_file", uses=1):
    return leases.issue(mission_id=mission.mission_id, session_id="S1",
                        capability=capability, scope=scope, ttl_seconds=60,
                        max_uses=uses)


@pytest.mark.asyncio
async def test_a_step_without_a_lease_is_refused_under_lease_enforcement(
        service, leases, workspace):
    """An unleased step has no authorization to point at."""
    target = workspace / "f.txt"

    def work(step, mission):
        target.write_text("ran", encoding="utf-8")

        class _Ok:
            ok = True
            message = "wrote"
        return _Ok()

    mission = _mission(service,
                       conditions=[{"statement": "x", "verifier": "y"}],
                       steps=[{"title": "write", "tool": "write_file",
                               "params": {"path": str(target)}}])

    runner = MissionRunner(service, execute_step=work, permission=_allow_all,
                           leases=leases)
    result = await runner.run(mission.mission_id)

    assert result.state == MissionState.BLOCKED.value
    assert "no lease to spend" in result.reason
    assert not target.exists()


@pytest.mark.asyncio
async def test_a_valid_lease_lets_the_step_run_and_is_spent(service, leases, workspace):
    target = workspace / "f.txt"

    def work(step, mission):
        target.write_text("--ok", encoding="utf-8")

        class _Ok:
            ok = True
            message = "wrote"
        return _Ok()

    mission = _mission(service,
                       conditions=[{"statement": "the file says --ok",
                                    "verifier": "file content"}],
                       steps=[{"title": "write", "tool": "write_file",
                               "params": {"path": str(target)}}])
    lease = _lease_for(leases, mission, scope=[str(workspace)])

    runner = MissionRunner(service, execute_step=work, permission=_allow_all,
                           leases=leases)
    result = await runner.run(
        mission.mission_id,
        verifiers={"C1": Verifiers.file_contains(str(target), "--ok", "C1")},
        approval={"lease_id": lease.lease_id, "session_id": "S1"})

    assert result.state == MissionState.COMPLETED.value
    assert leases.get(lease.lease_id).uses == 1


@pytest.mark.asyncio
async def test_a_revoked_lease_blocks_the_step(service, leases, workspace):
    """Demo C's shape: approval existed, permission was withdrawn before commit."""
    target = workspace / "f.txt"

    def work(step, mission):
        target.write_text("should not run", encoding="utf-8")

        class _Ok:
            ok = True
            message = "wrote"
        return _Ok()

    mission = _mission(service,
                       conditions=[{"statement": "x", "verifier": "y"}],
                       steps=[{"title": "write", "tool": "write_file",
                               "params": {"path": str(target)}}])
    lease = _lease_for(leases, mission, scope=[str(workspace)])
    leases.revoke(lease.lease_id, reason="the operator withdrew it")

    runner = MissionRunner(service, execute_step=work, permission=_allow_all,
                           leases=leases)
    result = await runner.run(mission.mission_id,
                              approval={"lease_id": lease.lease_id, "session_id": "S1"})

    assert result.state == MissionState.BLOCKED.value
    assert "revoked" in result.reason
    assert not target.exists()


@pytest.mark.asyncio
async def test_a_lease_for_another_directory_blocks_the_step(service, leases, workspace):
    target = workspace / "f.txt"
    other = workspace.parent / "elsewhere"

    def work(step, mission):
        target.write_text("nope", encoding="utf-8")

        class _Ok:
            ok = True
            message = "wrote"
        return _Ok()

    mission = _mission(service,
                       conditions=[{"statement": "x", "verifier": "y"}],
                       steps=[{"title": "write", "tool": "write_file",
                               "params": {"path": str(target)}}])
    lease = _lease_for(leases, mission, scope=[str(other)])

    runner = MissionRunner(service, execute_step=work, permission=_allow_all,
                           leases=leases)
    result = await runner.run(mission.mission_id,
                              approval={"lease_id": lease.lease_id, "session_id": "S1"})

    assert result.state == MissionState.BLOCKED.value
    assert "outside the lease's scope" in result.reason
    assert not target.exists()


# -- parallel execution ----------------------------------------------------

@pytest.mark.asyncio
async def test_independent_steps_run_concurrently(service, workspace):
    """Three independent steps should all start before any of them finishes."""
    import asyncio

    started = []
    finished = []

    async def slow(step, mission):
        started.append(step.step_id)
        await asyncio.sleep(0.05)
        finished.append(step.step_id)

        class _Ok:
            ok = True
            message = "ran"
        return _Ok()

    def allow(step, mission):
        return {"allowed": True, "reason": "ok"}

    mission = service.create(
        "three independent jobs",
        conditions=[{"statement": "all ran", "verifier": "step ledger"}],
        steps=[{"title": "a", "tool": "read_file", "params": {"path": str(workspace / "a")}},
               {"title": "b", "tool": "read_file", "params": {"path": str(workspace / "b")}},
               {"title": "c", "tool": "read_file", "params": {"path": str(workspace / "c")}}],
    )
    runner = MissionRunner(service, execute_step=slow, permission=allow)
    await runner.run_parallel(mission.mission_id, max_concurrent=4)

    # All three started before the first finished -- that is real concurrency.
    assert len(started) == 3
    assert started[:3] == ["S1", "S2", "S3"] or set(started) == {"S1", "S2", "S3"}


@pytest.mark.asyncio
async def test_a_dependent_step_waits_in_the_parallel_runner(service, workspace):
    order = []

    async def record(step, mission):
        order.append(step.step_id)

        class _Ok:
            ok = True
            message = "ran"
        return _Ok()

    def allow(step, mission):
        return {"allowed": True, "reason": "ok"}

    mission = service.create(
        "dependent work",
        conditions=[{"statement": "both ran", "verifier": "ledger"}],
        steps=[{"title": "first", "tool": "read_file",
                "params": {"path": str(workspace / "one")}},
               {"title": "second", "tool": "read_file",
                "params": {"path": str(workspace / "two")},
                "depends_on": ["S1"]}],
    )
    runner = MissionRunner(service, execute_step=record, permission=allow)
    await runner.run_parallel(mission.mission_id)

    assert order == ["S1", "S2"], "the dependent step must run after its prerequisite"


@pytest.mark.asyncio
async def test_two_writers_to_one_file_do_not_run_together(service, workspace):
    target = workspace / "shared.txt"
    concurrent = {"now": 0, "max": 0}

    async def write(step, mission):
        import asyncio

        concurrent["now"] += 1
        concurrent["max"] = max(concurrent["max"], concurrent["now"])
        await asyncio.sleep(0.03)
        concurrent["now"] -= 1

        class _Ok:
            ok = True
            message = "wrote"
        return _Ok()

    def allow(step, mission):
        return {"allowed": True, "reason": "ok"}

    mission = service.create(
        "two writes to one file",
        conditions=[{"statement": "both ran", "verifier": "ledger"}],
        steps=[{"title": "one", "tool": "write_file", "params": {"path": str(target)}},
               {"title": "two", "tool": "write_file", "params": {"path": str(target)}}],
    )
    runner = MissionRunner(service, execute_step=write, permission=allow)
    await runner.run_parallel(mission.mission_id)

    assert concurrent["max"] == 1, "two writers to one file ran at the same time"

def test_file_identical_compares_real_bytes(workspace):
    """A copy condition: the destination holds exactly the source's contents."""
    from jarvis.core.missions import Mission

    source = workspace / "source.txt"
    source.write_text("report body", encoding="utf-8")
    copy = workspace / "copy.txt"
    copy.write_bytes(source.read_bytes())

    mission = Mission(mission_id="M", request="", outcome="")
    same = Verifiers.file_identical(str(copy), str(source), "C1")(mission)
    assert same.status == ConditionStatus.VERIFIED

    copy.write_text("report body, edited", encoding="utf-8")
    differs = Verifiers.file_identical(str(copy), str(source), "C1")(mission)
    assert differs.status == ConditionStatus.FAILED

def test_a_missing_destination_is_failed_and_a_missing_source_unknown(workspace):
    """The two absences mean different things and must not be conflated.

    A missing destination means the copy did not happen -- the condition it
    claimed is false. A missing *source* means the comparison can no longer be
    made at all, so the honest report is "nobody can tell", not "it failed".
    """
    from jarvis.core.missions import Mission

    mission = Mission(mission_id="M", request="", outcome="")
    source = workspace / "source.txt"
    source.write_text("x", encoding="utf-8")

    assert Verifiers.file_identical(str(workspace / "gone.txt"), str(source), "C1")(
        mission).status == ConditionStatus.FAILED

    copy = workspace / "copy.txt"
    copy.write_text("x", encoding="utf-8")
    assert Verifiers.file_identical(str(copy), str(workspace / "gone.txt"), "C1")(
        mission).status == ConditionStatus.UNKNOWN


# -- the mission's own intent is a second refusal ----------------------------

@pytest.mark.asyncio
async def test_a_step_outside_the_recorded_intent_is_refused_before_it_runs(
        service, workspace):
    """The permission engine may allow a tool; the intent asks whether the
    person asked for it. Both must pass, and the intent refusal names the rule.
    """
    from jarvis.security.intentshield import IntentScope

    scope = IntentScope(
        mission_id="MIS-INTENT", goal="write inside the workspace",
        allowed_capabilities={"write_file"},
        allowed_resource_prefixes=[str(workspace)], risk_budget=1)

    ran = []

    async def executor(step, mission):
        ran.append(step.step_id)
        return _Ok()

    mission = service.create(
        "write inside the workspace",
        steps=[{"step_id": "S1", "title": "outside", "tool": "write_file",
                "params": {"path": "/etc/not-allowed", "content": "x"}}],
        intent=scope.to_json())

    runner = MissionRunner(service, execute_step=executor, permission=_allow_all)
    result = await runner.run(mission.mission_id)

    assert result.state == MissionState.BLOCKED.value, result.reason
    assert ran == [], "a step the intent refuses was executed anyway"
    assert "intent" in result.reason


@pytest.mark.asyncio
async def test_a_step_inside_the_intent_still_passes(service, workspace):
    """The intent check must not refuse everything -- the guard has to have a
    path that allows, or it would prove nothing."""
    from jarvis.security.intentshield import IntentScope

    target = workspace / "inside.txt"
    scope = IntentScope(
        mission_id="MIS-INTENT", goal="write inside the workspace",
        allowed_capabilities={"write_file"},
        allowed_resource_prefixes=[str(workspace)], risk_budget=1)

    async def executor(step, mission):
        Path(step.params["path"]).write_text("x", encoding="utf-8")
        return _Ok()

    mission = service.create(
        "write inside the workspace",
        conditions=[{"condition_id": "C1", "statement": "the file exists",
                     "verifier": '{"kind": "file_contains", "path": "%s", '
                                 '"needle": "x"}' % target}],
        steps=[{"step_id": "S1", "title": "write", "tool": "write_file",
                "params": {"path": str(target), "content": "x"}}],
        intent=scope.to_json())

    runner = MissionRunner(service, execute_step=executor, permission=_allow_all)
    result = await runner.run(mission.mission_id)

    assert result.state in (MissionState.COMPLETED.value,
                            MissionState.PARTIALLY_COMPLETED.value), result.reason
    assert target.exists()


@pytest.mark.asyncio
async def test_a_steered_plan_is_refused_when_it_leaves_the_intent(service, workspace):
    """Steering must not be a way around the intent: a revision is checked at
    run time, against the same recorded intent, not only in the preview."""
    from jarvis.security.intentshield import IntentScope

    scope = IntentScope(
        mission_id="MIS-INTENT", goal="write inside the workspace",
        allowed_capabilities={"write_file"},
        allowed_resource_prefixes=[str(workspace)], risk_budget=1)
    mission = service.create(
        "write inside the workspace",
        steps=[{"step_id": "S1", "title": "inside", "tool": "write_file",
                "params": {"path": str(workspace / "a.txt"), "content": "a"}}],
        intent=scope.to_json())

    service.apply_revision(
        mission.mission_id,
        steps=[{"step_id": "S9", "title": "escape", "tool": "write_file",
                "params": {"path": "/etc/escaped.txt", "content": "x"}}],
        reason="an escape attempt")

    ran = []

    async def executor(step, mission):
        ran.append(step.step_id)
        return _Ok()

    runner = MissionRunner(service, execute_step=executor, permission=_allow_all)
    result = await runner.run(mission.mission_id)

    assert result.state == MissionState.BLOCKED.value, result.reason
    assert ran == []
    assert "intent" in result.reason

@pytest.mark.asyncio
async def test_a_refused_step_is_written_to_the_audit_chain(service, workspace):
    """A denial that lives only on the mission row is invisible to the log.

    Passports count refusals from the audit chain, and a person reviewing "what
    did it try" reads the same log. So the refusal has to reach it, not just the
    mission's own `reason` string.
    """
    from jarvis.security.audit import AuditLog
    from jarvis.security.intentshield import IntentScope

    scope = IntentScope(
        mission_id="MIS-AUDIT", goal="write inside the workspace",
        allowed_capabilities={"write_file"},
        allowed_resource_prefixes=[str(workspace)], risk_budget=1)
    mission = service.create(
        "write only inside the workspace",
        steps=[{"step_id": "S1", "title": "inside", "tool": "write_file",
                "params": {"path": str(workspace / "a.txt"), "content": "a"}}],
        intent=scope.to_json())
    service.apply_revision(
        mission.mission_id,
        steps=[{"step_id": "S9", "title": "escape", "tool": "write_file",
                "params": {"path": "/etc/escaped.txt", "content": "x"}}],
        reason="an escape attempt")

    audit = AuditLog()
    runner = MissionRunner(service, execute_step=lambda s, m: _Ok(),
                           permission=_allow_all, audit=audit)
    result = await runner.run(mission.mission_id)
    assert result.state == MissionState.BLOCKED.value

    rows = [r for r in audit.recent(200)
            if str(r.get("result") or "") == "denied"
            and mission.mission_id in str(r.get("intent") or "")]
    assert rows, "the refusal was not written to the audit chain"
    detail = rows[0].get("detail") or {}
    assert detail.get("refused_by") == "intent"
    assert detail.get("step_id") == "S9"
