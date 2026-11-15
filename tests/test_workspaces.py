"""Saved workspaces: unique names, honest launches, reversible edits.

Every test here drives the real service over a real SQLite database. The
launch tests replace the two side-effecting seams (application launch and the
``open`` handoff) so the suite never starts a real application or loads a real
URL -- what is being pinned is the *reporting*, which is the part a person
reads and trusts.
"""

from __future__ import annotations

import subprocess

import pytest


@pytest.fixture()
def service():
    from jarvis.core.workspaces import WorkspaceService
    from jarvis.db.database import get_db

    return WorkspaceService(get_db())


def _unique(prefix: str) -> str:
    import uuid

    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _folder(tmp_path):
    directory = tmp_path / "project"
    directory.mkdir()
    return str(directory)


# -- names -------------------------------------------------------------------

def test_a_workspace_is_created_and_read_back(service, tmp_path):
    name = _unique("Writing")
    created = service.create(name, project_dir=_folder(tmp_path),
                             apps=["finder"], urls=["https://example.com"])
    assert created["name"] == name
    assert created["apps"] == ["finder"]
    assert service.get(created["workspace_id"])["urls"] == ["https://example.com"]


def test_a_name_collision_is_refused_case_insensitively(service):
    name = _unique("Focus")
    service.create(name)
    with pytest.raises(ValueError):
        service.create(name.upper())


def test_a_rename_that_collides_is_refused_and_leaves_the_row_alone(service):
    first = service.create(_unique("One"))
    second = service.create(_unique("Two"))
    with pytest.raises(ValueError):
        service.update(second["workspace_id"], name=first["name"])
    assert service.get(second["workspace_id"])["name"] != first["name"]


def test_a_workspace_without_a_name_is_refused(service):
    with pytest.raises(ValueError):
        service.create("   ")


def test_a_project_directory_that_does_not_exist_is_refused(service, tmp_path):
    with pytest.raises(ValueError):
        service.create(_unique("Ghost"), project_dir=str(tmp_path / "nope"))


# -- members -----------------------------------------------------------------

def test_members_are_trimmed_deduplicated_and_bounded(service):
    created = service.create(_unique("Members"),
                             apps=["finder", " finder ", "", "terminal"])
    assert created["apps"] == ["finder", "terminal"]

    with pytest.raises(ValueError):
        service.create(_unique("TooMany"), apps=[f"app{i}" for i in range(51)])


def test_editing_replaces_only_the_named_lists(service):
    workspace = service.create(_unique("Partial"), apps=["finder"],
                               urls=["https://example.com"])
    updated = service.update(workspace["workspace_id"], apps=["terminal"])
    assert updated["apps"] == ["terminal"]
    assert updated["urls"] == ["https://example.com"], "an untouched list must not clear"


# -- duplicate and delete ----------------------------------------------------

def test_a_duplicate_gets_its_own_name_and_copies_the_members(service):
    source = service.create(_unique("Source"), apps=["finder"])
    copy = service.duplicate(source["workspace_id"])
    assert copy["workspace_id"] != source["workspace_id"]
    assert copy["name"] != source["name"]
    assert copy["apps"] == ["finder"]


def test_deleting_reports_whether_a_row_was_there(service):
    workspace = service.create(_unique("Gone"))
    assert service.delete(workspace["workspace_id"]) is True
    assert service.delete(workspace["workspace_id"]) is False


# -- launch ------------------------------------------------------------------

def test_a_launch_reports_each_member_separately(service, tmp_path, monkeypatch):
    """One failing member must not turn into one opaque failure."""
    from jarvis.core import maccontrol, workspaces

    monkeypatch.setattr(maccontrol, "open_application",
                        lambda key, path="": {"ok": True, "launched": True,
                                              "observed_running": True,
                                              "detail": f"{key} is running"})

    def fake_run(argv, **kwargs):
        target = argv[-1]
        if "broken" in target:
            return subprocess.CompletedProcess(argv, 1, "", "no application knows it")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(workspaces.subprocess, "run", fake_run)

    workspace = service.create(_unique("Mixed"), apps=["finder"],
                               urls=["https://ok.example", "https://broken.example"])
    result = service.launch(workspace["workspace_id"])

    assert result["verified"] is False
    by_target = {r["target"]: r for r in result["results"]}
    assert by_target["finder"]["ok"] is True
    assert by_target["https://ok.example"]["ok"] is True
    assert by_target["https://broken.example"]["ok"] is False
    assert "1 did not" in result["summary"] or "did not" in result["summary"]


def test_a_fully_successful_launch_is_marked_verified(service, tmp_path, monkeypatch):
    from jarvis.core import maccontrol, workspaces

    monkeypatch.setattr(maccontrol, "open_application",
                        lambda key, path="": {"ok": True, "launched": True,
                                              "observed_running": True,
                                              "detail": f"{key} is running"})
    monkeypatch.setattr(workspaces.subprocess, "run",
                        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""))

    workspace = service.create(_unique("AllGood"), apps=["finder"])
    result = service.launch(workspace["workspace_id"])
    assert result["verified"] is True
    assert service.get(workspace["workspace_id"])["launch_count"] == 1


def test_a_non_http_url_is_refused_rather_than_handed_to_open(service, monkeypatch):
    from jarvis.core import workspaces

    calls = []
    monkeypatch.setattr(workspaces.subprocess, "run",
                        lambda argv, **kwargs: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0, "", ""))

    workspace = service.create(_unique("Scheme"), urls=["file:///etc/passwd"])
    result = service.launch(workspace["workspace_id"])
    assert result["results"][0]["ok"] is False
    assert calls == [], "a non-http URL must never reach open"


def test_a_missing_file_is_reported_without_calling_open(service, tmp_path, monkeypatch):
    from jarvis.core import workspaces

    calls = []
    monkeypatch.setattr(workspaces.subprocess, "run",
                        lambda argv, **kwargs: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0, "", ""))

    workspace = service.create(_unique("Missing"),
                               files=[str(tmp_path / "absent.txt")])
    result = service.launch(workspace["workspace_id"])
    assert result["results"][0]["ok"] is False
    assert "does not exist" in result["results"][0]["detail"]
    assert calls == []


def test_an_empty_workspace_is_not_verified(service):
    workspace = service.create(_unique("Empty"))
    result = service.launch(workspace["workspace_id"])
    assert result["results"] == []
    assert result["verified"] is False