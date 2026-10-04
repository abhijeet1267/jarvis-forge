"""Phase 39a: the folder you named is the folder the editor opens.

Reported by the user: "it open vscode etc but in vscode its not opening folder". Measured:

```
jarvis/tools/apps.py:35      run_command(["open", "-a", target])   -> no folder argument exists
jarvis/core/planner.py:1156  \b(open|launch|start|run)\s+(.+)      -> resolves the APP and
                                                                      throws the rest away
jarvis/tools/coding.py:49    open_workspace(path)                  -> registers a directory in
                                                                      Vantage; never touches
                                                                      an editor
`code` on PATH                                                     -> MISSING
/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code -> 1.140.0, present
```

So the app opened exactly what macOS hands a bare `open -a`: whatever was last focused. The
folder was never sent. Verified against the real editor: pointing its bundled CLI at a directory
made VS Code write `workspaceStorage/<hash>/workspace.json` containing that directory, which is
the observation this phase reports success from -- an exit code of 0 says a command ran, not that
a folder opened.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from jarvis.core.models import CommandRequest, PlannedAction, PolicyDecision


def _ctx(core):
    from jarvis.tools.base import ToolContext

    return ToolContext(device_id="laptop_main", session_id="p39")


def _calls(monkeypatch, module, code=0, out="", err=""):
    """Record what the tool actually launches, and answer as if it worked."""
    seen = []

    async def fake_run(argv, **kw):
        seen.append(list(argv))
        return code, out, err

    monkeypatch.setattr(module, "run_command", fake_run)
    return seen


# ---------------------------------------------------------------------------
# 1. the registry must describe a command that accepts a folder
# ---------------------------------------------------------------------------


def test_vscode_declares_a_command_that_can_take_a_folder():
    from jarvis.registry.apps import DEFAULT_APPS

    vscode = next(a for a in DEFAULT_APPS if a.key == "vscode")
    cli = getattr(vscode, "cli", None)
    assert cli, "an editor the user opens folders in must declare a command line"
    assert "macos" in cli


def test_the_declared_command_resolves_on_this_machine():
    """A declared command nobody can find is how the folder gets silently dropped again."""
    from jarvis.registry import apps as apps_registry
    from jarvis.tools import apps as apps_module

    vscode = next(a for a in apps_registry.DEFAULT_APPS if a.key == "vscode")
    found = apps_module.resolve_editor_command(vscode, platform="macos")
    if found is None:
        pytest.skip("no VS Code install on this machine")
    assert Path(found).exists(), found


# ---------------------------------------------------------------------------
# 2. the tool carries the folder, and refuses to invent one
# ---------------------------------------------------------------------------


def test_opening_an_app_with_a_folder_sends_the_folder(core, monkeypatch, tmp_path):
    from jarvis.tools import apps as apps_module

    workspace = tmp_path / "my-project"
    workspace.mkdir()
    seen = _calls(monkeypatch, apps_module)
    monkeypatch.setattr(apps_module, "editor_folder_opened", lambda *a, **k: True)

    result = asyncio.run(apps_module.open_app(_ctx(core), app="vscode", path=str(workspace)))
    assert result.ok is True, result.error or result.message
    assert seen, "nothing was launched"
    assert str(workspace) in seen[-1], seen[-1]


def test_a_folder_that_does_not_exist_launches_nothing(core, monkeypatch, tmp_path):
    from jarvis.tools import apps as apps_module

    seen = _calls(monkeypatch, apps_module)
    missing = tmp_path / "not-here"
    result = asyncio.run(apps_module.open_app(
        _ctx(core), app="vscode", path=str(missing)))
    assert result.ok is False, result.output
    assert "not-here" in (result.error or result.message)
    assert seen == [], "an invented folder must not reach the editor"


def test_a_file_is_not_a_folder(core, monkeypatch, tmp_path):
    from jarvis.tools import apps as apps_module

    seen = _calls(monkeypatch, apps_module)
    a_file = tmp_path / "notes.md"
    a_file.write_text("x\n")
    result = asyncio.run(apps_module.open_app(_ctx(core), app="vscode", path=str(a_file)))
    assert result.ok is False
    assert "directory" in (result.error or result.message).lower(), result.message
    assert seen == []


def test_a_folder_named_rather_than_pathed_resolves_against_open_workspaces(
        core, monkeypatch, tmp_path):
    """"open vscode with agent-test" names a workspace, not a path. The registry knows
    which directories are real, so the tool should use that rather than ask."""
    from jarvis.tools import apps as apps_module
    from jarvis.tools import coding as coding_module

    workspace = tmp_path / "agent-test"
    workspace.mkdir()
    opened = asyncio.run(coding_module.open_workspace(_ctx(core), path=str(workspace)))
    assert opened.ok is True, opened.message

    seen = _calls(monkeypatch, apps_module)
    monkeypatch.setattr(apps_module, "editor_folder_opened", lambda *a, **k: True)
    result = asyncio.run(apps_module.open_app(_ctx(core), app="vscode", path="agent-test"))
    assert result.ok is True, result.error or result.message
    assert str(workspace) in seen[-1], seen[-1]


def test_an_unrecognised_folder_offers_the_ones_that_exist(core, monkeypatch):
    """Phase 33's rule: a question the system asks must carry the answers it holds."""
    from jarvis.tools import apps as apps_module

    _calls(monkeypatch, apps_module)
    result = asyncio.run(apps_module.open_app(
        _ctx(core), app="vscode", path="definitely-not-a-workspace-xyz"))
    assert result.ok is False
    message = (result.error or result.message)
    assert "open_workspace" in message.lower() or "no open workspace" in message.lower(), message


# ---------------------------------------------------------------------------
# 3. success means observed, not "the command returned"
# ---------------------------------------------------------------------------


def test_a_launch_that_cannot_be_observed_is_reported_unverified(core, monkeypatch, tmp_path):
    from jarvis.tools import apps as apps_module

    workspace = tmp_path / "project"
    workspace.mkdir()
    _calls(monkeypatch, apps_module)
    monkeypatch.setattr(apps_module, "editor_folder_opened", lambda *a, **k: False)
    result = asyncio.run(apps_module.open_app(_ctx(core), app="vscode", path=str(workspace)))
    assert result.ok is False or result.verification.value != "success", \
        "an unobserved open must not be recorded as a success"
    assert "could not confirm" in (result.message or "").lower(), result.message


def test_the_observation_reads_the_editors_own_record(tmp_path, monkeypatch):
    """Not a mock: the function that decides "did it open" is pointed at a storage
    directory laid out the way VS Code lays it out."""
    from jarvis.tools import apps as apps_module

    storage = tmp_path / "workspaceStorage" / "abc123"
    storage.mkdir(parents=True)
    target = tmp_path / "the-folder"
    target.mkdir()
    (storage / "workspace.json").write_text(
        '{"folder": "file://%s"}' % str(target), encoding="utf-8")
    assert apps_module.editor_folder_opened(target, storage_root=storage.parent) is True
    assert apps_module.editor_folder_opened(
        tmp_path / "other-folder", storage_root=storage.parent) is False


# ---------------------------------------------------------------------------
# 4. the sentence must keep the folder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", [
    "open vscode with {path}",
    "open vscode in {path}",
    "open folder {path} in vscode",
    "open {path} in vscode",
])
def test_a_sentence_that_names_a_folder_reaches_the_tool(core, tmp_path, phrase):
    workspace = tmp_path / "named-project"
    workspace.mkdir()
    plan = core.planner.parse(phrase.format(path=str(workspace)))
    actions = [a for a in plan.actions if a.tool == "open_application"]
    assert actions, f"{phrase!r} planned {[(a.tool, a.params) for a in plan.actions]}"
    assert actions[0].params.get("path") == str(workspace), actions[0].params


def test_a_sentence_naming_only_an_app_still_just_opens_the_app(core):
    plan = core.planner.parse("open vscode")
    actions = [a for a in plan.actions if a.tool == "open_application"]
    assert actions, [(a.tool, a.params) for a in plan.actions]
    assert not actions[0].params.get("path"), "no folder was named; none may be invented"


# ---------------------------------------------------------------------------
# 5. the guard already sees every string value; prove it sees this one
# ---------------------------------------------------------------------------


def test_a_protected_folder_is_refused_before_it_is_launched(core):
    verdict = core.policy.evaluate(PlannedAction(
        tool="open_application", params={"app": "vscode", "path": "/System/Volumes"},
        device="laptop_main"))
    assert verdict.decision is PolicyDecision.BLOCK, verdict.reason


def test_a_folder_whose_name_contains_project_is_not_a_declined_thing(core, tmp_path):
    """The bug this phase was chasing, in the user's own words.

    The project rule claimed any sentence holding "open" and "project", so a folder
    actually named `agent-test-project` could never be opened -- the editor was told to
    launch and the sentence was declined instead.
    """
    folder = tmp_path / "agent-test-project"
    folder.mkdir()
    plan = core.planner.parse(f"open vscode with {folder}")
    assert plan.reply_kind != "declined", plan.direct_reply
    actions = [a for a in plan.actions if a.tool == "open_application"]
    assert actions and actions[0].params.get("path") == str(folder), plan.model_dump()


def test_a_real_project_continuation_still_reaches_the_project_rule(core):
    """The fix must not swallow the sentence it was carved out of."""
    plan = core.planner.parse("continue my website project")
    assert plan.reply_kind == "declined" or plan.intent == "continue_project", \
        plan.model_dump()


# ---------------------------------------------------------------------------
# 6. found live, not in the suite: launching an app must not wait for it to quit
# ---------------------------------------------------------------------------


def test_a_launcher_that_holds_its_output_pipe_does_not_time_out():
    """The real VS Code open failed in the live check with "command timed out after
    20.0s" while all 17 unit tests passed -- because they faked `run_command`.

    A GUI launcher keeps stdout open for as long as the application runs, so a caller
    that captures and waits is waiting for the app to quit, not for the launch.
    """
    import asyncio
    import time

    from jarvis.tools.base import run_command

    started = time.monotonic()
    code, _out, err = asyncio.run(run_command(["sleep", "30"], detach=True, timeout=5.0))
    elapsed = time.monotonic() - started
    assert code == 0, err
    assert elapsed < 2.0, f"the launch waited {elapsed:.1f}s for a process to exit"


def test_a_detached_launch_still_reports_a_missing_program():
    import asyncio

    from jarvis.tools.base import run_command

    code, _out, err = asyncio.run(run_command(["definitely-not-a-real-binary-xyz"],
                                              detach=True))
    assert code != 0
    assert "definitely-not-a-real-binary-xyz" in err


def test_open_app_launches_without_waiting_for_the_editor(core, monkeypatch, tmp_path):
    """The tool must use that path, not the capturing one."""
    import inspect

    from jarvis.tools import apps as apps_module

    source = inspect.getsource(apps_module.open_app)
    assert "detach=True" in source, "open_app still waits for the editor to exit"
