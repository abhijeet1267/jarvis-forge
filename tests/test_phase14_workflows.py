"""Phase 14: an autonomous workflow must tell the truth about its steps.

The goal loop the plan describes (Goal -> Plan -> Execute -> Observe -> Verify ->
Recover -> Report) already existed in the orchestrator, but two things made it
unreachable and dishonest:

* ``RulePlanner`` structurally produced at most one action per sentence, so the
  multi-step branch never ran.
* Measured directly before the fix: a synthetic two-step plan that halted at its
  second step left the job reporting ``running`` forever, with the waiting step
  ``pending`` -- and after the approval expired it stayed that way permanently.

These tests bind to real backend calls. Nothing here asserts a state the server
did not produce, and a step that never ran is never allowed to read as done.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jarvis.api.app import app
from jarvis.config import get_settings
from jarvis.core.models import ActionStatus, CommandRequest, Plan, PlannedAction
from jarvis.core.tasks import job_status_for

PAGE = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"
TEST_TOKEN = os.environ["JARVIS_API_TOKEN"]
AUTH_HEADERS = {"X-JARVIS-Token": TEST_TOKEN}

node = shutil.which("node")


@pytest.fixture()
def workspace(tmp_area, monkeypatch) -> Path:
    monkeypatch.setattr(get_settings(), "file_workspace", str(tmp_area))
    return tmp_area


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:8765", headers=AUTH_HEADERS)


@pytest.fixture(scope="module")
def anon_client() -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:8765")


# ------------------------------------------------------------------------
# §2 — a stated sequence becomes a real multi-step plan
# ------------------------------------------------------------------------

MID_GOAL = (
    "what is the time and then create a file called p14-mid.txt containing: mid "
    "and then what is my battery"
)


def test_composed_goal_produces_multiple_actions(core):
    plan = core.planner.parse(MID_GOAL)
    assert len(plan.actions) == 3, [a.tool for a in plan.actions]
    assert [a.tool for a in plan.actions] == ["get_time", "write_file", "get_battery_status"]
    assert [a.step_id for a in plan.actions] == [1, 2, 3]
    assert plan.intent == "workflow"


def test_every_composed_action_is_a_tool_the_registry_has(core):
    for sentence in (
        "what is the time, then what is my battery",
        "open Visual Studio Code and then take a screenshot",
        MID_GOAL,
    ):
        for action in core.planner.parse(sentence).actions:
            assert core.tools.has(action.tool), action.tool


def test_unroutable_clause_is_not_invented_into_a_step(core):
    """Half a sequence JARVIS cannot do must not silently become nothing.

    The whole sentence falls back to ordinary parsing, so the user gets the
    single answer the old planner gave rather than a plan with a step quietly
    dropped from it.
    """
    plan = core.planner.parse("what is the time and then frobnicate the warp core")
    assert [a.tool for a in plan.actions] == ["get_time"]


@pytest.mark.parametrize(
    "sentence, expected",
    [
        # "then" inside the content is content, not a sequence marker.
        ("Create a file called p14-a.txt containing: first line then second line",
         "first line then second line\n"),
        ("Create a file called p14-b.txt containing: alpha and then beta",
         "alpha and then beta\n"),
    ],
)
def test_sequence_marker_inside_content_stays_content(core, workspace, sentence, expected):
    plan = core.planner.parse(sentence)
    assert len(plan.actions) == 1, [a.tool for a in plan.actions]
    assert plan.actions[0].params["content"] == expected


def test_composition_does_not_reinterpret_arguments(core, workspace):
    """Each step carries exactly the params the same clause produces alone."""
    combined = core.planner.parse(MID_GOAL).actions
    for action, clause in zip(combined, (
        "what is the time",
        "create a file called p14-mid.txt containing: mid",
        "what is my battery",
    )):
        solo = core.planner.parse(clause).actions
        assert solo and action.params == solo[0].params, clause


# ------------------------------------------------------------------------
# §3 — the job's state must match what actually happened
# ------------------------------------------------------------------------


async def test_halted_workflow_is_not_left_running(core, workspace):
    sentence = (
        "what is the time and then create a file called p14-halt.txt "
        "containing: never written"
    )
    response = await core.handle(CommandRequest(text=sentence, source="test"))
    assert response.needs_confirmation
    assert response.job_id, "the response does not point at its own job"

    job = core.tasks.get(response.job_id)
    assert job["status"] == "awaiting_confirmation", (
        "a job that stopped waiting for approval was still reported as running"
    )
    assert [(s["seq"], s["status"]) for s in job["steps"]] == [
        (0, "succeeded"), (1, "awaiting_confirmation"),
    ]
    assert not (workspace / "p14-halt.txt").exists(), "an unapproved step wrote to disk"


async def test_approving_one_step_does_not_claim_the_workflow_finished(core, workspace):
    response = await core.handle(CommandRequest(text=MID_GOAL, source="test"))
    assert response.needs_confirmation
    after = await core.resolve_confirmation(
        response.confirmation_id, True, session_id=response.session_id)

    job = core.tasks.get(after.job_id)
    assert job["status"] == "incomplete", job["status"]
    assert [(s["seq"], s["status"]) for s in job["steps"]] == [
        (0, "succeeded"), (1, "succeeded"), (2, "pending"),
    ]
    # The battery step never ran, and the reply says so rather than trailing off.
    assert "not run" in after.reply
    assert (workspace / "p14-mid.txt").read_text() == "mid\n"


async def test_denied_step_leaves_the_workflow_failed_not_done(core, workspace):
    response = await core.handle(CommandRequest(text=MID_GOAL, source="test"))
    denied = await core.resolve_confirmation(
        response.confirmation_id, False, session_id=response.session_id)

    job = core.tasks.get(denied.job_id)
    assert job["status"] == "failed"
    assert job["steps"][1]["status"] == "blocked"
    assert not (workspace / "p14-mid.txt").exists()


async def test_blocked_step_terminalizes_the_job(core, workspace):
    """Engine-level: a refusal the planner itself cannot produce.

    The write-file route anchors names inside the workspace, so an absolute
    protected path can only reach the pipeline as a constructed action. What is
    under test here is the job's state after a BLOCK verdict, not the route.
    """
    goal = "phase14 engine-level blocked step"
    hostile = Plan(goal=goal, intent="workflow", actions=[
        PlannedAction(step_id=1, tool="get_time", params={}, device="laptop_main"),
        PlannedAction(step_id=2, tool="write_file",
                      params={"path": "/etc/phase14-hostile.conf", "content": "x"},
                      device="laptop_main"),
    ])
    original = core.planner.parse
    core.planner.parse = lambda text, context=None: hostile
    try:
        response = await core.handle(CommandRequest(text=goal, source="test"))
    finally:
        # Session-scoped core: an unrestored override would leak into unrelated
        # tests, which is the Phase 12 harness defect.
        core.planner.parse = original

    assert response.decision is not None
    job = core.tasks.get(response.job_id)
    assert job["status"] == "failed", job["status"]
    assert job["steps"][1]["status"] == "blocked"
    assert not Path("/etc/phase14-hostile.conf").exists()


def test_aggregation_never_calls_an_unfinished_sequence_done():
    # The defect each of these lines encodes, as one table.
    assert job_status_for(["succeeded", "pending"]) == "incomplete"
    assert job_status_for(["succeeded", "awaiting_confirmation", "pending"]) == "awaiting_confirmation"
    assert job_status_for(["succeeded", "blocked"]) == "failed"
    assert job_status_for(["succeeded", "unverified"]) == "unverified"
    assert job_status_for(["succeeded", "succeeded"]) == "done"
    assert job_status_for([]) == "running"


def test_duplicate_step_names_do_not_move_each_other(core, tmp_area):
    """Same tool twice in one plan: step identity is positional, not by name."""
    job = core.tasks.create("phase14 duplicate steps", ["get_time", "get_time"])
    try:
        core.tasks.update_step(job.job_id, "get_time", ActionStatus.SUCCEEDED,
                               "first only", seq=0)
        steps = core.tasks.get(job.job_id)["steps"]
        assert [s["status"] for s in steps] == ["succeeded", "pending"]
    finally:
        core.tasks.cancel(job.job_id)


# ------------------------------------------------------------------------
# §4 — the API and the page
# ------------------------------------------------------------------------


def test_command_response_points_at_its_job_and_the_api_serves_its_steps(client, monkeypatch):
    area = Path(tempfile.mkdtemp(prefix="p14-api-"))
    monkeypatch.setattr(get_settings(), "file_workspace", str(area))
    sent = client.post("/command", json={"text": MID_GOAL, "source": "test"})
    assert sent.status_code == 200
    body = sent.json()
    assert body["job_id"]

    job = client.get(f"/tasks/{body['job_id']}")
    assert job.status_code == 200
    steps = job.json()["steps"]
    assert [s["seq"] for s in steps] == [0, 1, 2]
    assert [s["name"] for s in steps] == ["get_time", "write_file", "get_battery_status"]


def test_unknown_job_is_not_reported_as_found(client):
    assert client.get("/tasks/JOB-does-not-exist").status_code == 404


def test_anonymous_client_cannot_read_workflow_state(anon_client):
    assert anon_client.get("/tasks").status_code in (401, 403)
    assert anon_client.get("/tasks/JOB-anything").status_code in (401, 403)


def test_client_supplied_job_id_is_ignored(client):
    """The dashboard renders a job id; it must not be able to choose one."""
    response = client.post("/command", json={
        "text": "what is the time", "job_id": "JOB-forged", "source": "test"})
    assert response.status_code == 200
    assert response.json()["job_id"] == "", "a caller-supplied job id reached the response"


@pytest.fixture(scope="module")
def page_code(tmp_path_factory):
    """The step-mapping code actually shipped in dashboard/index.html."""
    if not node:
        pytest.skip("node not available to run the page's own code")
    body = re.search(r"<script>(.*?)</script>", PAGE.read_text(), re.S).group(1)
    extracted = (body[body.index("const STATE_TONE"):body.index("function applyHealth")]
                 .replace("const STATE_TONE", "var STATE_TONE", 1)
                 .replace("const STEP_STATE", "var STEP_STATE", 1)
                 .replace("const JOB_STATE", "var JOB_STATE", 1))
    for name in ("STEP_STATE", "JOB_STATE", "stateTone", "pipelineState"):
        assert name in extracted, f"{name} is not in the page"

    root = tmp_path_factory.mktemp("p14")
    lib = root / "page.js"

    def _run(expr):
        lib.write_text("var get = async () => { throw new Error('no fetch in test'); };\n"
                       "var escapeHtml = v => String(v ?? '');\n"
                       + "eval(" + json.dumps(extracted) + ");\n"
                       + "console.log(JSON.stringify(" + expr + "));\n")
        proc = subprocess.run([node, str(lib)], capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr[:400]
        return json.loads(proc.stdout.strip())

    return _run


@pytest.mark.parametrize("step_status, expected", [
    ("succeeded", "VERIFIED"),
    ("unverified", "UNKNOWN"),
    ("failed", "FAILED"),
    ("blocked", "BLOCKED"),
    ("awaiting_confirmation", "AWAITING CONFIRMATION"),
    ("pending", "NOT RUN"),
])
def test_page_maps_each_step_state(page_code, step_status, expected):
    assert page_code(f"STEP_STATE[{json.dumps(step_status)}]") == expected


def test_page_has_no_word_for_a_finished_job_that_is_not_true(page_code):
    # `incomplete` must not surface as a completion.
    assert page_code("JOB_STATE['incomplete']") == "WORKFLOW PARTLY DONE"
    assert "COMPLETE" not in page_code("JOB_STATE['incomplete']")
    assert page_code("JOB_STATE['done']") == "WORKFLOW COMPLETE"


def test_succeeded_step_is_the_only_one_that_renders_verified(page_code):
    """A pending step in a partly-done workflow never reads as done."""
    assert page_code("stateTone(STEP_STATE['pending'])") == "muted"
    assert page_code("stateTone(JOB_STATE['incomplete'])") == "warn"


def test_page_wires_the_workflow_render_call():
    source = PAGE.read_text()
    assert 'id="steps"' in source
    assert "renderWorkflow(data.job_id)" in source, "the job state is never displayed"
    assert 'get("/tasks/"' in source, "steps are not read from the backend"
