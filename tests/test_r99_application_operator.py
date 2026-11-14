"""Feature 2 — the application operator: open apps, documents, sites, projects.

The launch tools already existed for apps/URLs/project folders. What was missing
was a *document* opened in a named app — and the sentence for it was reaching
``read_file`` instead (so "open the readme in TextEdit" read the file's text
rather than opening it).

Every launch keeps the same rule the project already holds: ``open`` exiting 0
means the launch was *requested*, not that something appeared. A named app is
reported verified only when it is observed; the default handler, which cannot be
observed from here, is reported unverified — never claimed.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(scope="module")
def ctx():
    from jarvis.tools.base import ToolContext
    return ToolContext()


def test_a_document_opened_in_a_named_app_routes_to_open_document(core):
    for text in ("open the readme in TextEdit",
                 "open report.pdf in Preview",
                 "open notes.txt with TextEdit"):
        plan = core.planner.parse(text, {})
        assert [a.tool for a in plan.actions] == ["open_document"], (text, plan.actions)


def test_reading_a_file_is_still_a_read(core):
    """"read the readme" must not be mistaken for "open it in an app"."""
    plan = core.planner.parse("read the readme", {})
    assert [a.tool for a in plan.actions] == ["read_file"], plan.actions


def test_opening_an_app_or_site_is_unchanged(core):
    assert [a.tool for a in core.planner.parse("open finder", {}).actions] == ["open_application"]
    assert [a.tool for a in core.planner.parse("open https://github.com", {}).actions] == ["open_url"]
    plan = core.planner.parse("open my project in cursor", {})
    assert [a.tool for a in plan.actions] == ["open_application"], plan.actions


def test_open_document_refuses_a_missing_file(ctx):
    from jarvis.tools.apps import open_document

    result = asyncio.run(open_document(ctx, path="/no/such/file.pdf", app="Preview"))
    assert result.ok is False
    assert "could not find" in result.error.lower(), result.error


def test_open_document_refuses_a_folder(ctx, tmp_path):
    from jarvis.tools.apps import open_document

    result = asyncio.run(open_document(ctx, path=str(tmp_path), app="TextEdit"))
    assert result.ok is False
    assert "folder" in result.error.lower(), result.error


def test_open_document_asks_for_a_path(ctx):
    from jarvis.tools.apps import open_document

    result = asyncio.run(open_document(ctx, path=""))
    assert result.ok is False
    assert "which document" in result.error.lower(), result.error


def test_open_document_never_claims_a_launch_it_cannot_observe(ctx, tmp_path):
    """The default-handler launch is reported unverified, not as success."""
    from jarvis.tools.apps import open_document

    doc = tmp_path / "note.txt"
    doc.write_text("hello\n")
    result = asyncio.run(open_document(ctx, path=str(doc)))
    # Either it launched (unverified, because no app was observed) or the platform
    # refused; neither may be reported as a verified success.
    from jarvis.core.models import Verification
    assert result.verification is not Verification.SUCCESS or not result.ok
