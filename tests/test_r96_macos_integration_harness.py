"""The macOS app's integration paths, exercised against the REAL code.

XCTest cannot run in this environment (Command Line Tools only — `no such module
'XCTest'`), so the app's own test target has never executed here. These two
checks compile the REAL app source files (BackendManager, APIClient,
ConversationService, Conversation, APIModels) alongside a small driver and run
them against a real local HTTP backend on an ephemeral port, via the internal
`JARVIS_BACKEND_PORT` seam (production default stays 8765 — untouched). No mock
fetcher, no stub client: the real URLSession path and the real state machine are
what run.

1. **Connection lifecycle** (`IntegrationHarness/run_lifecycle_check.sh`) —
   connect -> running, backend down -> truthful error, recovery -> running and
   refreshed, repeated connect does NOT multiply the poll loop, 401 is reported
   as an authorization error (not "unreachable"), and shutdown clears the poll
   task.

2. **Command Center end-to-end** (`IntegrationHarness/run_command_center_check.sh`)
   — a model answer is shown and attributed, the session id is adopted for a
   follow-up, and provider failure / auth failure / malformed response each
   become an honest visible error — never a silent fake success.

A build is required (swiftc) and a Python interpreter for the stub server; where
either is missing the check skips rather than failing, with the reason named.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "macos" / "Vantage" / "IntegrationHarness"

_swiftc = shutil.which("swiftc")
_python = shutil.which("python3")

requires_toolchain = pytest.mark.skipif(
    _swiftc is None or _python is None,
    reason="swiftc or python3 not available for the macOS integration harness",
)


def _run(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(HARNESS / script)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=300,
    )


@requires_toolchain
def test_backend_manager_connection_lifecycle():
    """connect/down/recovery/no-duplicate-loop/auth-distinction/shutdown."""
    result = _run("run_lifecycle_check.sh")
    assert result.returncode == 0, (
        "the BackendManager lifecycle check failed:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}")
    assert "ALL CHECKS PASSED" in result.stdout, result.stdout


@requires_toolchain
def test_command_center_end_to_end():
    """model answer shown; session adopted; provider/auth/malformed handled honestly."""
    result = _run("run_command_center_check.sh")
    assert result.returncode == 0, (
        "the Command Center end-to-end check failed:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}")
    assert "ALL CHECKS PASSED" in result.stdout, result.stdout


def test_the_seam_defaults_to_production_8765():
    """The JARVIS_BACKEND_PORT seam must not change the production default.

    An isolated test points the seam elsewhere; production must still target
    8765. This pins both source files that read it.
    """
    for rel in ("Sources/Services/BackendManager.swift", "Sources/APIClient/APIClient.swift"):
        source = (REPO / "macos" / "Vantage" / rel).read_text(encoding="utf-8")
        assert 'JARVIS_BACKEND_PORT' in source, rel
        assert '"8765"' in source, f"{rel}: the production default 8765 is missing"

def test_anycodable_exposes_nested_objects():
    """A nested object survives AnyCodable, so "next up" is not blank.

    `AnyCodable.init` stores a nested object as `[String: Any]`; an accessor
    that cast to `[String: AnyCodable]` always returned nil, and the native
    planner's "next up" line rendered as an empty state while tasks existed.
    """
    import json
    src = (REPO / "macos" / "Vantage" / "Sources" / "APIClient" / "APIModels.swift").read_text()
    ext = src[src.index("extension AnyCodable {"):]
    assert "value as? [String: Any]" in ext, (
        "the dictionary accessor must read the [String: Any] shape AnyCodable stores")
    assert "value as? [String: AnyCodable]" not in ext, (
        "casting to [String: AnyCodable] does not match the stored shape")
    # And the planner view reads the nested next-action the server sends.
    view = (REPO / "macos" / "Vantage" / "Sources" / "Views" / "PlannerView.swift").read_text()
    assert 'raw["task"]' in view, view[:200]


def test_the_native_planner_has_the_same_sections_as_the_web_planner():
    """Feature parity is pinned in source, since the natives can't be unit-run.

    The web planner gained goal planning and a "needs attention" section after
    the native view was written, so the native one lagged. Both surfaces read the
    same routes; this guard keeps them from drifting apart again.
    """
    view = (REPO / "macos" / "Vantage" / "Sources" / "Views" / "PlannerView.swift").read_text()
    for needle, why in (
        ("planGoal", "goal planning (POST /planner/plan-goal)"),
        ("attention", "the needs-attention section (/attention)"),
        ("attentionSection", "the attention section is actually rendered"),
        ("goalComposer", "the goal box is actually rendered"),
    ):
        assert needle in view, f"the native planner is missing {why}"
    client = (REPO / "macos" / "Vantage" / "Sources" / "APIClient" / "APIClient.swift").read_text()
    assert '"/planner/plan-goal"' in client, "the client does not call plan-goal"
    assert '"/attention"' in client, "the client does not call /attention"
    models = (REPO / "macos" / "Vantage" / "Sources" / "APIClient" / "APIModels.swift").read_text()
    assert "struct AttentionItem" in models, "no AttentionItem model"


def test_the_native_work_view_matches_the_web_work_page():
    """The web gained /work before the native app did; keep them in step."""
    view = (REPO / "macos" / "Vantage" / "Sources" / "Views" / "WorkView.swift").read_text()
    for needle, why in (
        ("workspaces()", "listing workspaces"),
        ("openWorkspace(", "opening a workspace"),
        ("inspectWorkspace(", "inspecting a workspace"),
        ("workflowJobs()", "listing workflow jobs"),
        ("workflowJob(", "loading a job's steps"),
        ("workflowAction(", "pause/cancel"),
    ):
        assert needle in view, f"the native Work view is missing {why}"
    content = (REPO / "macos" / "Vantage" / "Sources" / "Views" / "ContentView.swift").read_text()
    assert "WorkView()" in content and ".work" in content, "Work is not reachable from the sidebar"


def test_the_native_people_view_covers_contacts_and_posts():
    """Call preparation and post drafts are structured surfaces, so they need a view.

    App/document launching is already reachable through the Command Center (it
    sends arbitrary text to `/command`), so a duplicate Operator screen would be
    redundant — but contacts, call briefs and post drafts are not sentences, and
    had no native surface at all.
    """
    view = (REPO / "macos" / "Vantage" / "Sources" / "Views" / "PeopleView.swift").read_text()
    for needle, why in (
        ("contacts()", "listing contacts"),
        ("addContact(", "adding a contact"),
        ("prepareCall(", "preparing a call"),
        ("posts()", "listing post drafts"),
        ("createPost(", "saving a draft"),
        ("deletePost(", "deleting a draft"),
    ):
        assert needle in view, f"the native People view is missing {why}"
    # The publishing boundary is shown, not hidden.
    assert "publishing" in view, "the view must state the publishing boundary"
    content = (REPO / "macos" / "Vantage" / "Sources" / "Views" / "ContentView.swift").read_text()
    assert "PeopleView()" in content and ".people" in content, "People is not reachable"


def test_home_surfaces_attention_and_never_fakes_all_clear():
    """Home must show problems first, and a failed check must not read as "clear"."""
    home = (REPO / "macos" / "Vantage" / "Sources" / "Views" / "HomeView.swift").read_text()
    assert "api.attention()" in home, "Home does not read /attention"
    assert 'sectionErrors["attention"]' in home, (
        "a failed attention fetch must be reported, not treated as 'nothing wrong'")
    assert "Needs attention" in home


def test_the_native_planner_scheduler_matches_the_web_controls():
    """The web scheduler gained Edit + History; the native view must keep up."""
    view = (REPO / "macos" / "Vantage" / "Sources" / "Views" / "PlannerView.swift").read_text()
    client = (REPO / "macos" / "Vantage" / "Sources" / "APIClient" / "APIClient.swift").read_text()
    for needle, why in (
        ("editSchedule", "editing a job"),
        ("scheduleHistory", "reading a job's runs"),
        ("loadHistory", "showing history in the view"),
        ("beginEdit", "the edit control"),
    ):
        assert needle in view, f"the native planner is missing {why}"
    assert "func editSchedule" in client and "func scheduleHistory" in client, (
        "the client does not call the edit/history routes")
    assert 'func patch<' in client, "the client has no PATCH helper"
    models = (REPO / "macos" / "Vantage" / "Sources" / "APIClient" / "APIModels.swift").read_text()
    assert "struct ScheduleRun" in models
