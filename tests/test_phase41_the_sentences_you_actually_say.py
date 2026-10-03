"""Phase 41a/41b: the sentences a person actually says.

Asked for "control on the system, an AI like Iron Man", the test was not the ambition but
the eight sentences behind it. Run through the planner on this build, measured:

    open github.com                -> read_file   path=~/.../github.com     (a domain, as a file)
    open the github site in chrome -> open_app    (the site is dropped)
    how much storage is left       -> not_understood, and no tool exists
    check storage                  -> not_understood
    find my resume and open it     -> not_understood, though search_files and open_app both exist
    open my project                -> DECLINED: "Project continuation needs the design and file
                                      agents, which are not in this build yet."

That last answer is the interesting one, because it is not true. The same build registers
`design` and `laptop` and `coding` agents, and Phase 39 already opens a folder in the editor
and proves it from the editor's own record. So a sentence the system could act on was
refused with an excuse about components it has -- and the excuse was written into the planner
as a string, where nothing checks it.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from jarvis.coding import workspace as workspace_module
from jarvis.core.models import PlannedAction, RiskLevel
from jarvis.tools import apps as apps_module

REPO = Path(__file__).resolve().parents[1]


def _ctx(core):
    from jarvis.tools.base import ToolContext

    return ToolContext(device_id="laptop_main", session_id="p41")


@pytest.fixture(autouse=True)
def _clean_workspaces():
    """Isolate the process-wide workspace registry for the duration of one test.

    This tool's whole answer depends on how many projects are open, so "whatever the
    rest of the session happened to leave open" is not a premise a test may rest on.
    """
    registry = workspace_module.get_workspace_registry()
    before = [(w.root, w.name) for w in registry.all()]
    for w in list(registry.all()):
        registry.close(w.workspace_id)
    yield registry
    for w in list(registry.all()):
        registry.close(w.workspace_id)
    for root, name in before:
        registry.open(str(root), name=name)


def _open(core, registry, tmp_path, name):
    folder = tmp_path / name
    folder.mkdir()
    registry.open(str(folder), name=name)
    return folder


# ---------------------------------------------------------------------------
# 41a — the refusal was false, and the capability behind it is real
# ---------------------------------------------------------------------------


def test_the_build_has_the_agents_the_refusal_says_it_lacks(core):
    """The sentence is the claim; the registry is the evidence."""
    planner = (REPO / "jarvis" / "core" / "planner.py").read_text()
    assert "not in this build yet" not in planner, (
        "the planner still declines with a claim about its own build")
    for agent in ("design", "laptop", "coding"):
        assert agent in core.agents, f"{agent} is registered; the refusal said it was not"


def test_the_one_open_project_opens_in_the_editor(core, _clean_workspaces, tmp_path,
                                                  monkeypatch):
    registry = _clean_workspaces
    folder = _open(core, registry, tmp_path, "ironman-41")

    seen = {}

    async def fake_run(argv, **kw):
        seen["argv"] = argv
        return 0, "", ""

    monkeypatch.setattr(apps_module, "run_command", fake_run)
    monkeypatch.setattr(apps_module, "editor_folder_opened",
                        lambda *a, **k: True)

    result = asyncio.run(apps_module.open_project_folder(
        _ctx(core), project=""))
    assert result.ok is True, result.error
    assert str(folder) in " ".join(seen["argv"]), seen["argv"]
    assert folder.name in result.message


def test_no_open_project_says_so_rather_than_inventing_a_folder(core, _clean_workspaces):
    result = asyncio.run(apps_module.open_project_folder(_ctx(core), project=""))
    assert result.ok is False
    assert "open_workspace" in result.error, (
        "the answer has to say how a project gets into the set I can open")


def test_two_open_projects_are_asked_about_by_name(core, _clean_workspaces, tmp_path):
    registry = _clean_workspaces
    _open(core, registry, tmp_path, "mars-41a")
    _open(core, registry, tmp_path, "venus-41b")

    result = asyncio.run(apps_module.open_project_folder(_ctx(core), project=""))
    assert result.ok is False
    assert "mars-41a" in result.error and "venus-41b" in result.error, result.error
    assert result.error.rstrip().endswith("?"), "a question, not a refusal"


def test_naming_the_project_picks_it_from_the_open_ones(core, _clean_workspaces, tmp_path,
                                                        monkeypatch):
    registry = _clean_workspaces
    _open(core, registry, tmp_path, "apollo-41")
    wanted = _open(core, registry, tmp_path, "artemis-41")

    seen = {}

    async def fake_run(argv, **kw):
        seen["argv"] = argv
        return 0, "", ""

    monkeypatch.setattr(apps_module, "run_command", fake_run)
    monkeypatch.setattr(apps_module, "editor_folder_opened", lambda *a, **k: True)

    result = asyncio.run(apps_module.open_project_folder(
        _ctx(core), project="artemis"))
    assert result.ok is True, result.error
    assert str(wanted) in " ".join(seen["argv"]), seen["argv"]


def test_the_editor_is_one_that_is_actually_installed(core, _clean_workspaces, tmp_path,
                                                     monkeypatch):
    """No installed editor means a sentence naming the machine's own facts, not a guess."""
    registry = _clean_workspaces
    _open(core, registry, tmp_path, "quiet-41")
    monkeypatch.setattr(apps_module, "installed_editors", lambda *a, **k: [])

    result = asyncio.run(apps_module.open_project_folder(_ctx(core), project=""))
    assert result.ok is False
    assert "editor" in result.error.lower()


def test_installed_means_present_on_this_machine_not_listed_in_a_file(tmp_path, monkeypatch):
    """`installed_editors` reads a directory, so a fake Applications tree proves it.

    The alternative is a test that passes because the machine happens to have VS Code --
    which is the shape of a claim that goes quietly false on any other machine.
    """
    from jarvis.registry.apps import DEFAULT_APPS
    from jarvis.tools import agents as agents_module

    fake = tmp_path / "Applications"
    (fake / "Cursor.app").mkdir(parents=True)
    monkeypatch.setattr(agents_module, "BUNDLE_ROOTS", (str(fake),))

    class _Apps:
        def get(self, key):
            return next((a for a in DEFAULT_APPS if a.key == key), None)

    found = apps_module.installed_editors(_Apps(), platform="macos")
    assert [s.key for s in found] == ["cursor"], found

    (tmp_path / "Applications" / "Visual Studio Code.app").mkdir()
    found = apps_module.installed_editors(_Apps(), platform="macos")
    assert [s.key for s in found] == ["vscode", "cursor"], (
        "the preference order is a claim about which editor a folder is opened in")


def test_an_editor_with_no_app_on_disk_is_not_offered(tmp_path, monkeypatch):
    from jarvis.tools import agents as agents_module

    monkeypatch.setattr(agents_module, "BUNDLE_ROOTS", (str(tmp_path / "empty"),))
    from jarvis.registry.apps import DEFAULT_APPS

    class _Apps:
        def get(self, key):
            return next((a for a in DEFAULT_APPS if a.key == key), None)

    assert apps_module.installed_editors(_Apps(), platform="macos") == []


def test_the_sentence_reaches_the_tool_instead_of_a_note(core):
    plan = core.planner.parse("open my project")
    actions = [a for a in plan.actions]
    assert actions, "the sentence still ends in a note rather than an action"
    assert actions[0].tool == "open_project_folder", actions
    assert getattr(plan, "reply_kind", "") != "declined"


def test_the_new_tool_declares_its_own_risk(core):
    spec = core.tools.get("open_project_folder")
    assert spec is not None, "registered with the rest of the laptop tools"
    assert spec.risk is RiskLevel.LOW
    assert spec.requires_confirmation is False


# ---------------------------------------------------------------------------
# 41b — storage: asked for twice, answered with nothing, and no tool at all
# ---------------------------------------------------------------------------


def test_a_storage_tool_exists(core):
    spec = core.tools.get("check_storage")
    assert spec is not None, "'check storage' had no tool behind it at all"
    assert spec.risk is RiskLevel.READ, "reading a volume is not an actuation"


def test_the_storage_answer_comes_from_the_volume_not_from_a_guess(core, monkeypatch):
    from jarvis.tools import storage as storage_module

    async def fake_run(argv, **kw):
        assert argv[0] == "df", argv
        return 0, (
            "Filesystem     1K-blocks      Used Available Use% Mounted on\n"
            "/dev/disk3s3s1  494384692 12338412  43762372  22%   /\n"), ""

    monkeypatch.setattr(storage_module, "run_command", fake_run)
    result = asyncio.run(storage_module.check_storage(_ctx(core)))
    assert result.ok is True, result.error
    data = result.output
    assert data["used_percent"] == 22, data
    assert round(data["free_gb"], 1) == 41.7, data       # 43,762,372 KiB
    assert round(data["total_gb"], 1) == 471.5, data
    assert "/dev/disk3s3s1" in result.message, result.message


def test_a_storage_question_is_understood(core):
    for sentence in ("check storage", "how much storage is left",
                     "is my disk full", "show free space"):
        plan = core.planner.parse(sentence)
        assert [a.tool for a in plan.actions] == ["check_storage"], (sentence, plan.actions)


def test_the_storage_answer_admits_when_there_is_no_answer(core, monkeypatch):
    from jarvis.tools import storage as storage_module

    async def fake_run(argv, **kw):
        return 1, "", "df: no such tool"

    monkeypatch.setattr(storage_module, "run_command", fake_run)
    result = asyncio.run(storage_module.check_storage(_ctx(core)))
    assert result.ok is False, "a failed read is not a number"
