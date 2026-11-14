"""The workflow templates the API advertises must actually exist and run.

`GET /workflows` published eight template names while nothing ever created them:
the engine had zero workflows and zero steps, so every
`POST /workflows/{name}/execute` raised `Unknown workflow` and answered 500.
Advertising a name that cannot run is the disconnected-surface defect.

These tests pin the fix: templates and their steps are seeded, a real template
executes against the real handlers, and a step handler that cannot do what it
claims reports failure rather than success.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from jarvis.api.app import app
    from tests.conftest import AUTH_HEADERS

    c = TestClient(app, base_url="http://127.0.0.1:8765")
    c.headers.update(AUTH_HEADERS)
    return c


def test_every_advertised_template_exists_with_steps(client):
    from jarvis.db.database import get_db
    from jarvis.workflows.engine import get_default_workflow_engine

    listed = client.get("/workflows").json()["workflows"]
    assert len(listed) == 8, listed

    get_default_workflow_engine()  # seeds on first use
    engine = get_default_workflow_engine()
    db = get_db()
    for entry in listed:
        workflow_id = entry["workflow_id"]
        assert workflow_id in engine.workflows, (
            f"{workflow_id} is advertised but the engine does not know it")
        count = db.query(
            "SELECT COUNT(*) AS c FROM workflow_steps WHERE workflow_id = ?",
            (workflow_id,))[0]["c"]
        assert count >= 1, f"{workflow_id} has no steps to run"


def test_a_template_executes_instead_of_answering_500(client):
    """The published path runs. 'unknown_intent' would be dishonest here."""
    from jarvis.workflows.engine import get_default_workflow_engine

    get_default_workflow_engine()
    response = client.post("/workflows/SYSTEM_STATUS/execute",
                           json={"session_id": "wf-test", "user_request": "run SYSTEM_STATUS"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["workflow_id"] == "SYSTEM_STATUS", data
    assert data["run_id"], data
    assert data["status"] in ("completed", "running"), data


def test_seeding_is_idempotent_and_keeps_existing_steps(client):
    """Re-seeding refreshes definitions without duplicating them.

    It deliberately rewrites them: a template whose definition was wrong would
    otherwise stay broken on an install that had already seeded.
    """
    from jarvis.db.database import get_db
    from jarvis.workflows.engine import get_default_workflow_engine, seed_default_workflows

    engine = get_default_workflow_engine()
    db = get_db()
    before = db.query("SELECT COUNT(*) AS c FROM workflow_steps")[0]["c"]
    seed_default_workflows(engine)
    seed_default_workflows(engine)
    after = db.query("SELECT COUNT(*) AS c FROM workflow_steps")[0]["c"]
    assert after == before, "re-seeding must not duplicate steps"
    assert before >= 8, before


def test_a_step_whose_dependency_is_absent_reports_failure():
    """A template step naming a tool that is not in the handler registry fails
    loudly instead of being scored a success."""
    from jarvis.workflows.engine import WorkflowStep, get_default_workflow_engine

    engine = get_default_workflow_engine()
    step = WorkflowStep(step_id="s1", workflow_id="X", order=0,
                        tool_name="no_such_tool", arguments={})
    ok, _result, error = engine._execute_step(step, {})
    assert ok is False
    assert "Unknown tool" in (error or "")


# ---------------------------------------------------------------------------
# Handler honesty: success must follow what actually happened.
# ---------------------------------------------------------------------------

def test_open_application_reports_failure_for_a_missing_app(monkeypatch):
    """`open` exits non-zero for an app that does not exist. The handler used to
    return success: True regardless, so the workflow claimed it had opened it."""
    import subprocess

    from jarvis.workflows.handlers import ApplicationHandler

    class _Failed:
        returncode = 1
        stderr = 'Unable to find application named "NoSuchApp_xyz"'
        stdout = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Failed())
    result = ApplicationHandler.open_application("NoSuchApp_xyz")

    assert result["success"] is False, result
    assert "NoSuchApp_xyz" in result["message"] or "Failed" in result["message"], result


def test_list_applications_reports_success_so_the_step_is_not_scored_failed(monkeypatch):
    """The engine reads result['success']; a listing that worked but omitted the
    key was scored as a failed step."""
    import subprocess

    from jarvis.workflows.handlers import ApplicationHandler

    class _Ok:
        returncode = 0
        stdout = "Finder\nTerminal\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Ok())
    result = ApplicationHandler.list_applications()

    assert result["success"] is True, result
    assert result["count"] == 2, result


def test_list_applications_reports_failure_when_the_read_fails(monkeypatch):
    import subprocess

    from jarvis.workflows.handlers import ApplicationHandler

    class _Failed:
        returncode = 1
        stdout = ""
        stderr = "not authorised to send Apple events"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Failed())
    result = ApplicationHandler.list_applications()

    assert result["success"] is False, result
    assert "authorised" in result["error"], result


def test_search_files_distinguishes_no_matches_from_could_not_look(monkeypatch):
    """An empty result is only trustworthy when the search actually completed.

    `find` exits 1 both for "no matches" and for a path or permission error, so
    the exit code alone cannot decide. Reporting the error case as a successful
    empty search is the same "empty is not the same as unreadable" mistake this
    project keeps fixing -- so here the partial result is a failed read that
    still returns whatever it managed to see.
    """
    import subprocess

    from jarvis.workflows.handlers import FileControlHandler

    class _Failed:
        returncode = 1
        stdout = ""
        stderr = "find: /x: Operation not permitted"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Failed())
    result = FileControlHandler.search_files("*.nothing", path="/tmp")

    assert result["success"] is False, result
    assert "Operation not permitted" in result["error"], result

    class _Clean:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Clean())
    result = FileControlHandler.search_files("*.nothing", path="/tmp")

    assert result["success"] is True, result
    assert result["count"] == 0, result

# ---------------------------------------------------------------------------
# Found by running all eight templates against a live server, not by reading code.
# ---------------------------------------------------------------------------

def test_every_template_runs_when_named_directly(client, monkeypatch):
    """The endpoint is given the template name; re-deriving it from prose lost
    it, so END_WORKDAY and DEVELOPMENT_START answered "Please specify which
    workflow to run" while SYSTEM_STATUS worked by luck of a substring match.

    Every handler is stubbed here on purpose. This test is about routing — that
    the template named in the URL is the one that runs — and leaving the real
    handlers in place makes RUN_TESTS launch the entire suite from inside the
    suite, which is both slow and genuinely recursive.
    """
    from jarvis.workflows import engine as engine_mod

    monkeypatch.setattr(engine_mod, "DEFAULT_HANDLERS",
                        {name: (lambda args: {"success": True, "stub": True})
                         for name in engine_mod.DEFAULT_HANDLERS})

    engine_mod.get_default_workflow_engine()
    for template_id in ("OPEN_WORKSPACE", "DEVELOPMENT_START", "RUN_TESTS",
                        "CHECK_BUILD", "CLEAN_DOWNLOADS", "PREPARE_MEETING",
                        "END_WORKDAY", "SYSTEM_STATUS"):
        data = client.post(f"/workflows/{template_id}/execute",
                           json={"session_id": "all-templates"}).json()
        assert data["run_id"], (template_id, data)
        assert data["workflow_id"] == template_id, (template_id, data)
        assert "specify which workflow" not in str(data.get("error") or ""), (template_id, data)


def test_a_failed_step_says_why_instead_of_none(client):
    """`_execute_step` returned error=None for a handler that failed without an
    `error` key, so runs were recorded as "Step 0 failed: None"."""
    from jarvis.workflows.engine import WorkflowStep, get_default_workflow_engine

    engine = get_default_workflow_engine()
    step = WorkflowStep(step_id="s1", workflow_id="X", order=0,
                        tool_name="execute_shell",
                        arguments={"command": "git status --short", "working_dir": "/nope"})
    ok, _result, error = engine._execute_step(step, {})
    assert ok is False
    assert error and error != "None", error


def test_search_files_expands_a_tilde_path(monkeypatch):
    """`~` was handed to `find` verbatim, so CLEAN_DOWNLOADS searched for a
    directory literally named '~/Downloads'."""
    import subprocess

    from jarvis.workflows.handlers import FileControlHandler

    seen = {}

    class _Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(cmd, *a, **k):
        seen["cmd"] = cmd
        return _Ok()

    monkeypatch.setattr(subprocess, "run", _run)
    monkeypatch.setattr("pathlib.Path.exists", lambda self: True)

    FileControlHandler.search_files("*", path="~/Downloads")

    assert "~" not in seen["cmd"][1], seen["cmd"]


def test_search_files_does_not_call_a_missing_folder_an_empty_one():
    """A path that does not exist is a failed search, not 'no matches'."""
    from jarvis.workflows.handlers import FileControlHandler

    result = FileControlHandler.search_files("*", path="/definitely/not/here/xyz")

    assert result["success"] is False, result
    assert "no such directory" in result["error"], result

def test_the_step_timeout_reaches_the_shell_handler():
    """`timeout_seconds` was stored on the step and then dropped in three places:
    it was nested under `arguments` at seed time, the `execute_shell` registry
    lambda never forwarded it, and nothing passed it from the step. A 30 s default
    silently cut off a test-suite run that needs minutes."""
    import time

    from jarvis.workflows.handlers import DEFAULT_HANDLERS

    started = time.time()
    result = DEFAULT_HANDLERS["execute_shell"](
        {"command": 'python3 -c "import time; time.sleep(5)"', "timeout": 30})
    assert result["success"] is True, result
    assert time.time() - started >= 4, "the command should have been allowed to finish"

    started = time.time()
    cut = DEFAULT_HANDLERS["execute_shell"](
        {"command": 'python3 -c "import time; time.sleep(30)"', "timeout": 1})
    assert cut["success"] is False, cut
    assert cut["timed_out"] is True, cut
    assert time.time() - started < 15, "the short timeout must actually abort the command"


def test_the_run_tests_template_declares_a_suite_sized_timeout():
    """The definition itself must carry the timeout, not rely on a default."""
    from jarvis.workflows.engine import TEMPLATE_STEPS

    (step,) = TEMPLATE_STEPS["RUN_TESTS"]
    assert step.get("timeout_seconds", 0) >= 600, step
    assert "timeout_seconds" not in step.get("arguments", {}), (
        "a column-level field left inside arguments is the bug that hid it")


def test_a_timed_out_command_leaves_no_orphan_running():
    """`subprocess.run` kills only the shell. A timed-out RUN_TESTS step left a
    full pytest run alive, reparented to launchd, and the next attempt stacked
    another one on top of it. The handler now runs the command in its own
    process group and signals the group on timeout."""
    import subprocess
    import time

    from jarvis.workflows.handlers import DEFAULT_HANDLERS

    marker = "vantage_orphan_probe_4711"
    started = time.time()
    result = DEFAULT_HANDLERS["execute_shell"](
        {"command": f'python3 -c "import time; time.sleep(60)  # {marker}"',
         "timeout": 1})
    assert result["timed_out"] is True, result
    assert time.time() - started < 30, "the timeout must abort promptly"

    time.sleep(2)
    survivors = subprocess.run(["pgrep", "-f", marker],
                               capture_output=True, text=True)
    assert survivors.stdout.strip() == "", (
        f"a timed-out command is still running: {survivors.stdout.strip()}")


def test_quit_reports_an_absent_app_as_nothing_to_do(monkeypatch):
    """END_WORKDAY failed on a machine where the app was not running, and
    `was_running` was hardcoded True without being checked."""
    from jarvis.workflows.handlers import ApplicationHandler

    monkeypatch.setattr(ApplicationHandler, "_is_running", staticmethod(lambda name: False))
    result = ApplicationHandler.quit_application("Cursor")

    assert result["success"] is True, result
    assert result["was_running"] is False, result
    assert "not running" in result["message"], result


def test_a_helper_with_the_name_in_its_path_is_not_the_application(monkeypatch):
    """`_process_exists` substring-matched the whole path, so the macOS
    ``CursorUIViewService`` helper made Cursor look installed and running."""
    import subprocess

    from jarvis.workflows.handlers import ApplicationHandler

    class _Ps:
        returncode = 0
        stderr = ""
        stdout = (
            "/System/Library/PrivateFrameworks/TextInputUIMacHelper.framework"
            "/Versions/A/XPCServices/CursorUIViewService.xpc/Contents/MacOS/CursorUIViewService\n"
            "/System/Library/CoreServices/Finder.app/Contents/MacOS/Finder\n"
        )

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Ps())

    assert ApplicationHandler._process_exists("Cursor") is False, (
        "a service whose path contains the name is not the application")
    assert ApplicationHandler._process_exists("Finder") is True
    assert ApplicationHandler._process_exists("CursorUIViewService") is True


def test_an_uninstalled_app_is_reported_as_not_running(monkeypatch):
    """The osascript question must ask for an exact name, not a substring."""
    import subprocess

    from jarvis.workflows.handlers import ApplicationHandler

    seen = {}

    class _No:
        returncode = 0
        stdout = "false\n"
        stderr = ""

    def _run(cmd, *a, **k):
        if cmd and cmd[0] == "osascript":
            seen["script"] = cmd[-1]
        return _No()

    monkeypatch.setattr(subprocess, "run", _run)
    assert ApplicationHandler._is_running("Cursor") is False
    assert " whose name is " in seen["script"], seen["script"]
