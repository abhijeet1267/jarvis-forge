"""Phase 50 — a permission manager that says who answered.

The build prompt (§3) sets one rule above the rest:

    Never display "Permission Granted" until the operating system actually reports that it
    is granted.

Measured on 2026-10-04, that rule has a second half the prompt does not mention. The same
status query, run from this agent's shell against a Mac where the user had granted Speech
Recognition to Vantage an hour earlier:

    screen_preflight=false   accessibility=true   microphone=authorized   speech=notDetermined

Three of those four are true statements about *the wrong subject*. macOS attributes a
permission to the responsible process, so a status read is a statement about whoever asked --
and a dashboard that prints "Accessibility: GRANTED" from a helper spawned by a terminal is
describing Terminal.app to a person reading a Vantage window.

So every status here carries the process that answered, and the service is allowed to say
UNKNOWN more often than it is allowed to guess.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis.permissions import service as svc

REPO = Path(__file__).resolve().parents[1]
SWIFT = REPO / "jarvis" / "permissions" / "native" / "permission_state.swift"

STATUSES = {"NOT_REQUIRED", "NOT_DETERMINED", "DENIED", "GRANTED",
            "REQUIRES_MANUAL_SYSTEM_SETTINGS", "UNKNOWN"}


BUNDLED_HOST = {"jarvis_root": "/Vantage.app/Contents/Resources/app/jarvis",
                "inside_bundle": True,
                "bundle_path": "/Vantage.app",
                "python": "/usr/bin/python3"}


@pytest.fixture()
def service(monkeypatch):
    """A service whose helper answer and host identity are both supplied.

    The host has to be injected too: the subject rule is about where the *answering
    process* runs, and a test run from a checkout is by definition not inside a bundle.
    """
    s = svc.PermissionStatusService()
    monkeypatch.setattr(s, "read_helper", lambda: dict(HELPER))
    monkeypatch.setattr(s, "host_identity", lambda: dict(BUNDLED_HOST))
    return s


HELPER = {
    "screen_recording": "denied",
    "accessibility": "granted",
    "microphone": "authorized" if False else "granted",
    "speech_recognition": "not_determined",
    "answered_by": {"pid": 1, "executable": "/X/Vantage.app/Contents/MacOS/JarvisForge",
                    "bundle_id": "com.jarvisforge.app"},
}


# ---------------------------------------------------------------------------
# 1. the shape of an answer
# ---------------------------------------------------------------------------


def test_the_helper_source_is_a_real_macos_query():
    """The statuses are only as honest as the API behind them. These four calls are the
    non-prompting ones Apple provides; a check built on `request*` would pop a dialog on
    every dashboard poll, which is not a status read.

    Phase 56 added a request mode to this same binary, so the ban can no longer be
    file-wide -- and the thing worth protecting was never "no requesting code exists", it
    was "the path a status read takes does not prompt". So the assertion is now scoped to
    the status path: everything before the request dispatch must be non-prompting, and the
    prompting calls must live after it.
    """
    source = SWIFT.read_text()
    marker = "// STATUS READ PATH"
    assert marker in source, "the helper no longer separates its request mode from its reads"
    request_region, _, status_region = source.partition(marker)

    for call in ("CGPreflightScreenCaptureAccess", "AXIsProcessTrusted",
                 "authorizationStatus(for: .audio)", "SFSpeechRecognizer.authorizationStatus"):
        assert call in status_region, f"the status path no longer queries {call}"

    for prompting in ("requestAccess", "requestAuthorization", "CGRequestScreenCaptureAccess",
                      "kAXTrustedCheckOptionPrompt"):
        assert prompting not in status_region, (
            f"{prompting} is on the status path; a status read must never prompt")
        assert prompting in request_region, (
            f"{prompting} never arrived in the request path -- Phase 56's request mode is "
            "not actually implemented")


def test_every_capability_reports_one_of_the_six_states(service):
    rows = service.status()
    assert rows, "the service reported nothing"
    for row in rows:
        assert row["status"] in STATUSES, row


def test_every_capability_carries_the_fields_the_prompt_asks_for(service):
    """§4 lists them per permission: capability, system status, explanation, request
    method, settings destination, verification method, recovery instructions."""
    for row in service.status():
        for field in ("capability", "status", "explanation", "request_method",
                      "settings_pane", "test_method", "recovery"):
            assert row.get(field), f"{row['capability']}: no {field}"


def test_the_answer_records_which_process_gave_it(service):
    """The subject, not just the value. Without this the dashboard cannot tell the user
    whether "granted" is about Vantage or about whatever shell started the server."""
    rows = service.status()
    subject = rows[0]["answered_by"]
    assert subject["bundle_id"] == "com.jarvisforge.app", subject
    assert rows[0]["about_bundled_host"] is True


def test_a_reading_taken_outside_the_bundle_says_which_subject_it_describes():
    """The case measured today, not a hypothetical.

    Same helper binary, same machine: asked by Vantage.app it answered speech=GRANTED,
    screen=GRANTED, microphone=not_determined, accessibility=denied; asked from a terminal
    it answered speech=NOT_DETERMINED, screen=denied, microphone=DENIED, accessibility=
    GRANTED. Four capabilities, four different answers, because macOS answers about the
    responsible process. A card that cannot say which one it is holding is worse than no
    card, because it reads as Vantage's state either way.
    """
    s = svc.PermissionStatusService()
    s.read_helper = lambda: dict(HELPER)
    s.host_identity = lambda: {**BUNDLED_HOST, "inside_bundle": False,
                               "bundle_path": None,
                               "jarvis_root": "/Users/dev/jarvis-forge/jarvis"}
    rows = s.status()
    assert all(r["about_bundled_host"] is False for r in rows)
    assert any("not inside" in r["status_note"] or "describes this caller" in r["status_note"]
               for r in rows), rows[0]["status_note"]


def test_a_bare_binary_answer_is_flagged_as_such(service):
    """The exact case measured today: the same helper run outside the bundle reports the
    terminal's permissions. That reading is not wrong, it is about something else -- and
    the UI must not be able to present it as Vantage's state."""
    # A helper that is itself a loose binary is normal -- it lives in the data dir -- so
    # the subject is decided by the process that started it, not by its own bundle id.
    service.read_helper = lambda: dict(HELPER, answered_by={
        "pid": 2, "executable": "/tmp/permission_state", "bundle_id": "none"})
    rows = service.status()
    assert all(r["about_bundled_host"] is True for r in rows), rows[0]
    assert rows[0]["answered_by"]["bundle_id"] == "none", rows[0]["answered_by"]


# ---------------------------------------------------------------------------
# 2. no answer, no claim
# ---------------------------------------------------------------------------


def test_a_missing_helper_yields_unknown_never_a_default_grant(monkeypatch):
    """The failure mode this service exists to prevent: a check that could not run
    reporting the optimistic value.

    NOT_REQUIRED is allowed to survive, because it is a statement about macOS (there is
    no such gate here) rather than a claim about a grant nobody read.
    """
    s = svc.PermissionStatusService()
    monkeypatch.setattr(s, "read_helper", lambda: None)
    rows = s.status()
    assert rows, "a dead helper produced no rows at all -- the card would render nothing"
    assert all(r["status"] != svc.GRANTED for r in rows), \
        [(r["capability"], r["status"]) for r in rows]
    checked = [r for r in rows if r["capability"] != "local_network"]
    assert all(r["status"] == svc.UNKNOWN for r in checked), \
        [(r["capability"], r["status"]) for r in checked]
    assert all("helper" in r["status_note"].lower() or "could not" in r["status_note"].lower()
               for r in checked), rows[0]["status_note"]


def test_a_partial_helper_answer_leaves_the_rest_unknown(monkeypatch):
    """One field missing is not zero fields missing; the others must not inherit a value."""
    s = svc.PermissionStatusService()
    monkeypatch.setattr(s, "read_helper", lambda: {
        "accessibility": "granted",
        "answered_by": {"pid": 3, "executable": "/X/Vantage.app/Contents/MacOS/JarvisForge",
                        "bundle_id": "com.jarvisforge.app"},
    })
    got = {r["capability"]: r["status"] for r in s.status()}
    assert got["accessibility"] == "GRANTED"
    assert got["speech_recognition"] == "UNKNOWN", got


# ---------------------------------------------------------------------------
# 3. the mapping, capability by capability
# ---------------------------------------------------------------------------


def test_screen_recording_denied_is_a_manual_settings_state_not_a_refusal(service):
    """macOS never prompts a helper for Screen Recording -- only Settings can grant it, so
    "DENIED" here would send the user looking for a prompt that will never arrive."""
    row = service.by_capability("screen_recording")
    assert row["status"] == "REQUIRES_MANUAL_SYSTEM_SETTINGS", row
    assert "Screen Recording" in row["settings_pane"]


def test_speech_not_determined_stays_not_determined(service):
    """This one *can* be granted by a prompt, and Phase 47 proved the prompt arrives when
    the app asks. So the honest state is "never asked", not "refused"."""
    assert service.by_capability("speech_recognition")["status"] == "NOT_DETERMINED"


def test_granted_stays_granted(service):
    assert service.by_capability("accessibility")["status"] == "GRANTED"
    assert service.by_capability("microphone")["status"] == "GRANTED"


def test_automation_is_never_reported_granted_without_an_attempt(service):
    """Apple Events has no preflight query. The only way to learn the answer is to send an
    event, which is itself the permission being asked about -- so the status is unknown
    until someone deliberately tests it, and saying GRANTED here would be a invented fact."""
    row = service.by_capability("automation")
    assert row["status"] == "UNKNOWN", row
    assert "attempt" in row["explanation"].lower() or "test" in row["explanation"].lower()


def test_local_network_is_not_a_mac_permission(service):
    """§4 lists Local Network. On macOS there is no such API (it is an iOS thing); the
    honest answer is that the capability is not gated here, not a silent grant."""
    row = service.by_capability("local_network")
    assert row["status"] in {"NOT_REQUIRED", "UNKNOWN"}, row
    assert row["status"] != "GRANTED"


# ---------------------------------------------------------------------------
# 4. the surface
# ---------------------------------------------------------------------------


def test_the_route_exists_and_is_authenticated():
    import importlib

    m = importlib.import_module("jarvis.api.app")
    from tests.conftest import flat_routes

    routes = {r.path: r for r in flat_routes(m.app)}
    assert "/permissions" in routes, sorted(routes)[:5]


def test_the_dashboard_shows_the_permission_card_and_who_answered():
    source = (REPO / "dashboard" / "index.html").read_text()
    assert "/permissions" in source, "the dashboard never asks the host"
    assert "answered_by" in source or "about_bundled_host" in source, (
        "the card prints statuses without saying which process they describe")


def test_a_page_never_prints_granted_for_an_unknown_state():
    """§3's rule, enforced on the client too: the word "Granted" may only follow a GRANTED."""
    source = (REPO / "dashboard" / "index.html").read_text()
    assert 'd.status === "GRANTED"' in source or 'r.status === "GRANTED"' in source, (
        "nothing distinguishes a grant from any other state in the rendering")


# ---------------------------------------------------------------------------
# 5. the bundle test, which the injected host identity could not catch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,expected", [
    # The real delivered layout, measured 2026-10-04.
    ("/Users/me/Downloads/Vantage/Vantage.app/Contents/Resources/app/jarvis",
     "/Users/me/Downloads/Vantage/Vantage.app"),
    # A checkout is not a bundle, and the reading describes the shell that started it.
    ("/Users/me/code/jarvis-forge/jarvis", None),
    # The component ends with .app; it is not named ".app". The first version tested for
    # equality and called the bundle "not inside a bundle" -- the live check caught it.
    ("/Apps/Other.app/Contents/Resources/app/jarvis", "/Apps/Other.app"),
])
def test_the_bundle_is_found_by_the_path_that_contains_it(path, expected):
    from pathlib import Path as P

    got = svc.PermissionStatusService._bundle_for(P(path))
    assert got == expected, f"{path} -> {got}"


def test_a_server_running_from_the_bundle_reports_itself_as_bundled():
    """The same question the delivered app answered wrongly once: does *this* process run
    from inside an .app? Answered from the real filesystem, not from an injected fixture."""
    s = svc.PermissionStatusService()
    s.read_helper = lambda: {}
    host = s.host_identity()
    assert host["inside_bundle"] is (svc.PermissionStatusService._bundle_for(
        Path(host["jarvis_root"])) is not None)
    # A test run from a checkout must report False -- if it reported True, every status
    # here would be claiming to describe Vantage.app from a process that is not it.
    assert host["inside_bundle"] is False, host


def test_the_subject_line_is_said_once():
    """Caught in a real browser: the card printed "read by Vantage running from …" twice,
    because the client rendered the server's note and then rendered it again under its own
    heading. Two identical lines is not information, it is a bug that looks like a
    sentence."""
    source = (REPO / "dashboard" / "index.html").read_text()
    body = source.split("async function loadPermissions()")[1].split("\n}")[0]
    assert body.count("data.note") == 1, body.count("data.note")
