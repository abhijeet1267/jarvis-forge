"""Phase 56 -- asking for a permission, on purpose, and reporting what actually happened.

Phase 50 built the status card and deliberately left it read-only, because a status poll that
raises a system dialog is a consent attack: the page refreshes, macOS interrupts the user, and
nobody asked for that. This phase adds the other half -- a request the user starts -- and the
rule that makes it safe: **the answer comes from re-reading the state afterwards, never from
what the request itself reported.**

That is not academic. macOS's own request APIs are inconsistent about what their completion
means:

* ``AVCaptureDevice.requestAccess`` / ``SFSpeechRecognizer.requestAuthorization`` hand back a
  bool for "the user decided yes", and the authoritative answer is still the status query.
* ``AXIsProcessTrustedWithOptions`` with the prompt option grants nothing -- it opens System
  Settings and returns the *previous* state. Reporting its return value as the outcome would
  say "granted" about a dialog the user has not finished.
* ``CGRequestScreenCaptureAccess`` can return true while the running process still needs a
  relaunch before the grant applies to it.

Capabilities a helper cannot ask about -- automation (the only probe *is* the action),
notifications (the framework needs a bundle and a run loop this helper does not have), local
network (not gated at all) -- refuse, name the reason, and never run the binary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

GRANTED = "GRANTED"
DENIED = "DENIED"
MANUAL = "REQUIRES_MANUAL_SYSTEM_SETTINGS"
UNKNOWN = "UNKNOWN"
NOT_REQUIRED = "NOT_REQUIRED"

REQUESTABLE = {"microphone", "speech_recognition", "screen_recording", "accessibility"}
NOT_REQUESTABLE = {"automation", "notifications", "local_network"}


@pytest.fixture()
def service():
    from jarvis.config import get_settings
    from jarvis.permissions.service import PermissionStatusService

    return PermissionStatusService(settings=get_settings())


def _cap(service, name):
    return next(c for c in service.capabilities() if c.name == name)


def _dashboard(page: str) -> str:
    return (Path(__file__).resolve().parents[1] / "dashboard" / page).read_text(
        encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. every capability says whether it can be asked for, and why not if it cannot
# ---------------------------------------------------------------------------


def test_the_seven_capabilities_are_declared(service):
    assert {c.name for c in service.capabilities()} == {
        "microphone", "speech_recognition", "screen_recording", "accessibility",
        "automation", "notifications", "local_network"}


def test_the_requestable_set_is_exactly_what_macos_can_prompt_for(service):
    got = {c.name for c in service.capabilities() if c.requestable}
    assert got == REQUESTABLE, got


def test_what_cannot_be_requested_says_why(service):
    for name in NOT_REQUESTABLE:
        cap = _cap(service, name)
        assert cap.requestable is False, name
        assert cap.no_request_reason, f"{name} refuses without giving a reason"


def test_automation_cannot_be_asked_because_the_probe_is_the_action(service):
    """The only way to find out whether Apple Events are allowed is to send one. Asking would
    be doing, and doing is the feature's job, not a settings page's."""
    assert "event" in _cap(service, "automation").no_request_reason.lower()


def test_accessibility_prompts_but_the_prompt_grants_nothing(service):
    cap = _cap(service, "accessibility")
    assert cap.requestable is True
    assert cap.prompt_only is True, (
        "the accessibility prompt opens System Settings; calling that a grant is a lie")


def test_local_network_is_not_gated_at_all(service):
    cap = _cap(service, "local_network")
    assert cap.requestable is False
    assert "not" in cap.no_request_reason.lower()


# ---------------------------------------------------------------------------
# 2. the request answers from the re-read, never from the request
# ---------------------------------------------------------------------------


def test_a_request_that_claims_success_is_still_answered_from_the_state(service, monkeypatch):
    """The whole phase in one test: a request API returning True is a report about a dialog,
    not about the permission."""
    monkeypatch.setattr(service, "_run_request",
                        lambda key: {"ok": True, "requested": True, "helper_says": GRANTED})
    monkeypatch.setattr(service, "_read_state", lambda cap: DENIED)
    out = service.request("microphone")
    assert out["state"] == DENIED, out
    assert out["granted"] is False, out
    assert "is granted" not in out["message"].lower(), out["message"]


def test_a_request_that_is_answered_yes_reports_yes(service, monkeypatch):
    monkeypatch.setattr(service, "_run_request", lambda key: {"ok": True, "requested": True})
    monkeypatch.setattr(service, "_read_state", lambda cap: GRANTED)
    out = service.request("microphone")
    assert out["state"] == GRANTED
    assert out["granted"] is True
    assert "granted" in out["message"].lower()


def test_a_prompt_only_request_says_the_dialog_opened_and_no_more(service, monkeypatch):
    monkeypatch.setattr(service, "_run_request", lambda key: {"ok": True, "requested": True})
    monkeypatch.setattr(service, "_read_state", lambda cap: MANUAL)
    out = service.request("accessibility")
    assert out["state"] == MANUAL, out
    assert out["granted"] is False
    assert "settings" in out["message"].lower(), out["message"]


def test_the_request_names_the_subject_it_asked_for(service, monkeypatch):
    """Phase 50's rule, applied to the new path: a state with no subject is not a fact."""
    monkeypatch.setattr(service, "_run_request", lambda key: {"ok": True, "requested": True})
    monkeypatch.setattr(service, "_read_state", lambda cap: GRANTED)
    out = service.request("microphone")
    assert out.get("subject"), out


def test_an_unknown_capability_is_refused(service):
    with pytest.raises(ValueError):
        service.request("telepathy")


def test_a_capability_that_cannot_be_asked_never_runs_the_helper(service, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "_run_request",
                        lambda key: calls.append(key) or {"ok": True, "requested": False})
    with pytest.raises(ValueError):
        service.request("automation")
    assert calls == [], "a refused request still ran the native helper"


def test_a_local_network_request_is_refused_as_not_gated(service):
    with pytest.raises(ValueError):
        service.request("local_network")


def test_a_request_that_could_not_run_is_not_reported_as_a_refusal(service, monkeypatch):
    """If the helper did not run, the honest answer is "could not ask" -- not "you were
    refused", which would send someone to System Settings to fix a non-problem."""
    monkeypatch.setattr(service, "_run_request",
                        lambda key: {"ok": False, "reason": "the helper did not run"})
    out = service.request("microphone")
    assert out["ok"] is False, out
    assert out["state"] == UNKNOWN, out
    assert "could not ask" in out["message"].lower(), out["message"]


def test_an_aborted_helper_reports_why_rather_than_a_bare_failure(service, monkeypatch):
    """Measured on this machine: the Speech framework aborts the helper when it is not an app
    bundle, so the reason has to reach the person at the keyboard -- "it failed" would be a
    dead end, and the real cause is that the ask has to come from inside Vantage.app."""
    import subprocess as sp

    real = sp.run

    def aborted(cmd, *a, **k):
        # The helper is called as [binary, "request", key] -- matching on the wrong index
        # here let the first version of this test run the real binary instead of the stub.
        if len(cmd) > 1 and cmd[1] == "request":
            return sp.CompletedProcess(cmd, returncode=-6, stdout="",
                                       stderr="Speech framework: bundle proxy unavailable")
        return real(cmd, *a, **k)

    monkeypatch.setattr("jarvis.permissions.service.subprocess.run", aborted)
    asked = service._run_request("speech_recognition")
    assert asked["ok"] is False, asked
    assert "bundle proxy" in asked["reason"], asked


def test_a_refusal_stays_a_refusal_after_the_request(service, monkeypatch):
    """macOS remembers "Don't allow" and stops asking. The state must not soften into
    not_determined, which would imply another prompt is coming."""
    monkeypatch.setattr(service, "_run_request", lambda key: {"ok": True, "requested": True})
    monkeypatch.setattr(service, "_read_state", lambda cap: DENIED)
    out = service.request("speech_recognition")
    assert out["state"] == DENIED
    assert "denied" in out["message"].lower() or "refused" in out["message"].lower(), out["message"]


# ---------------------------------------------------------------------------
# 3. the read surface says which rows can be asked for
# ---------------------------------------------------------------------------


def test_the_status_rows_carry_the_requestability(service, monkeypatch):
    monkeypatch.setattr(service, "read_helper", lambda: None)
    rows = service.status()
    assert len(rows) == 7
    by = {r["capability"]: r for r in rows}
    assert by["microphone"]["requestable"] is True
    assert by["automation"]["requestable"] is False
    assert by["automation"]["no_request_reason"]
    assert by["accessibility"]["prompt_only"] is True


def test_reading_the_status_still_never_prompts(service, monkeypatch):
    """The card is polled. A poll that raises a dialog is the thing Phase 50 refused to build,
    and adding a request path must not have smuggled it into the read path."""
    seen = []
    monkeypatch.setattr(service, "_run_request",
                        lambda key: seen.append(key) or {"requested": True})
    service.status()
    assert seen == []


# ---------------------------------------------------------------------------
# 4. the HTTP surface
# ---------------------------------------------------------------------------


def _api():
    import importlib

    return importlib.import_module("jarvis.api.app")


def _client(base="http://127.0.0.1:8765", headers=None, peer="127.0.0.1"):
    """``peer`` is the socket address the server sees.

    TestClient defaults it to "testclient", which is neither loopback nor a real address -- so
    without setting it, every loopback-only rule reads the caller as remote and a test that
    asserts 403 is really testing the fixture.
    """
    from fastapi.testclient import TestClient

    from tests.conftest import AUTH_HEADERS

    return TestClient(_api().app, base_url=base, client=(peer, 50000),
                      headers=AUTH_HEADERS if headers is None else headers)


def test_the_request_route_needs_authorisation():
    with _client(headers={}) as anon:
        assert anon.post("/permissions/microphone/request").status_code == 401


def test_a_socket_that_is_not_the_laptop_is_refused():
    """The dialog appears in front of the machine, so the decision to raise it belongs to
    someone at the laptop -- regardless of how authorised the caller is."""
    with _client(peer="192.0.2.7") as client:
        res = client.post("/permissions/microphone/request")
        assert res.status_code == 403, res.text
        assert "laptop" in res.json()["detail"].lower(), res.text


def test_an_address_the_host_gate_accepts_is_still_refused_here():
    """A phone on the wifi passes the host gate, because its Host is an address this machine
    owns -- so the host gate alone does not cover this rule. The loopback check has to."""
    from fastapi.testclient import TestClient

    from tests.conftest import AUTH_HEADERS

    with TestClient(_api().app, base_url="http://127.0.0.1:8765",
                    client=("192.168.1.40", 44040), headers=AUTH_HEADERS) as client:
        res = client.post("/permissions/microphone/request")
        assert res.status_code == 403, res.text


def test_an_unknown_capability_is_404():
    with _client() as client:
        assert client.post("/permissions/telepathy/request").status_code == 404


def test_an_unaskable_capability_is_409_with_a_reason():
    with _client() as client:
        res = client.post("/permissions/automation/request")
        assert res.status_code == 409, res.text
        assert "event" in res.json()["detail"].lower(), res.text


def test_a_request_from_the_laptop_returns_the_state_that_was_read(service, monkeypatch):
    """End to end through the route, with the helper itself stubbed: what comes back must be
    the re-read state, not whatever the request claimed."""
    monkeypatch.setattr(service, "_run_request", lambda key: {"ok": True, "requested": True})
    monkeypatch.setattr(service, "_read_state", lambda cap: DENIED)
    import jarvis.permissions.service as svc

    monkeypatch.setattr(svc, "PermissionStatusService", lambda *a, **k: service)
    with _client() as client:
        body = client.post("/permissions/microphone/request").json()
    assert body["state"] == DENIED, body
    assert body["granted"] is False, body


# ---------------------------------------------------------------------------
# 5. the panel carries the control
# ---------------------------------------------------------------------------


def test_the_panel_offers_a_request_and_a_recheck():
    html = _dashboard("index.html")
    assert "/request" in html, "the panel has no request action"
    assert "Check again" in html, "the panel lost the re-read control"


def test_the_panel_does_not_promise_a_grant_it_cannot_give():
    html = _dashboard("index.html")
    assert "Open System Settings" in html, (
        "a prompt-only capability must not be labelled 'Grant access'")


# ---------------------------------------------------------------------------
# 6. regressions
# ---------------------------------------------------------------------------


def test_the_read_surface_still_answers_seven():
    from jarvis.permissions.service import describe_permissions

    payload = describe_permissions()
    assert len(payload["capabilities"]) == 7
    for row in payload["capabilities"]:
        assert row["status"], row


def test_no_gate_was_loosened():
    from jarvis.core.orchestrator import JarvisCore

    specs = JarvisCore().tools.all()
    assert sum(1 for s in specs if s.requires_confirmation) == 28
    assert len(specs) == len({s.name for s in specs})
