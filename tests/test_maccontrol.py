"""Mac Control: refuse unknown apps, and never call a launch "running" unseen.

The two properties that matter are both about honesty: an unresolvable phrase
must not become a blind launch, and a successful `open` must not be reported as
"the application is running" until a process was actually observed.
"""

from __future__ import annotations

import jarvis.core.maccontrol as mc


def test_supported_applications_come_from_the_registry():
    apps = mc.supported_applications()
    assert apps, "the registry should offer at least one application"
    assert all({"key", "name", "launch_target"} <= set(a) for a in apps)


def test_an_unknown_application_is_refused_without_launching(monkeypatch):
    """A typo must not become an `open` of whatever happened to match."""
    launched = []
    monkeypatch.setattr(mc.subprocess, "run",
                        lambda *a, **k: launched.append(a) or _ok())

    result = mc.open_application("definitely-not-an-app-xyz")

    assert result["ok"] is False
    assert result["launched"] is False
    assert "does not match" in result["detail"]
    assert launched == [], "nothing may be launched for an unresolved phrase"


def test_a_launch_is_not_reported_running_when_no_process_is_observed(monkeypatch):
    """`open` returning 0 means open asked; it does not mean the app started."""
    monkeypatch.setattr(mc.subprocess, "run", lambda *a, **k: _ok())
    monkeypatch.setattr(mc, "_is_running", lambda name: False)
    monkeypatch.setattr(mc, "LAUNCH_TIMEOUT", 0.2)

    result = mc.open_application("safari")

    assert result["launched"] is True, "the command was accepted"
    assert result["ok"] is False, "but nothing was observed running"
    assert result["observed_running"] is False
    assert "not observed running" in result["detail"]


def test_a_launch_that_is_observed_running_reports_success(monkeypatch):
    monkeypatch.setattr(mc.subprocess, "run", lambda *a, **k: _ok())
    monkeypatch.setattr(mc, "_is_running", lambda name: True)

    result = mc.open_application("safari")

    assert result["ok"] is True
    assert result["observed_running"] is True


def test_a_failed_launch_command_is_reported_not_hidden(monkeypatch):
    class _Failed:
        returncode = 1
        stdout = ""
        stderr = "Unable to find application named 'Nope'"

    monkeypatch.setattr(mc.subprocess, "run", lambda *a, **k: _Failed())

    result = mc.open_application("safari")
    assert result["ok"] is False
    assert "Unable to find" in result["detail"]


def test_status_reports_whether_the_running_list_is_real(monkeypatch):
    """An empty list must say it is empty because it could not look."""
    monkeypatch.setattr(mc, "running_applications", lambda: [])

    payload = mc.status()
    assert payload["can_list_running"] is False
    assert "not granted" in payload["note"]


class _ok:
    returncode = 0
    stdout = ""
    stderr = ""
