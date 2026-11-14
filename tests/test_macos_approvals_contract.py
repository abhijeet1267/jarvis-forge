"""The macOS app can approve a pending action — and can read one.

The Approvals screen has existed since the app's early days, and it was
**broken in a way that made it useless**. Its Swift model declared

    id, action, description, device_id, session_id, created_at, expires_at

and the backend's `/confirmations` sends

    confirmation_id, request_id, session_id, tool, params, device, risk,
    preview, state, created_at, resolved_at, requested_by_session,
    requested_surface, approved_by_session, approved_surface

**Not one of the model's field names matches.** `id` and `action` and
`description` are never sent, so decoding a real confirmation failed and the
screen showed an error instead of the approvals. The card also showed
`confirmation.action` — a field that does not exist — so even if it had
decoded, every card would have read "Unknown action".

These tests pin the contract from the *server* side: the shape the app
decodes is the shape the backend actually sends, checked against a real
response, so a renamed field breaks here rather than in a signed app.

The Swift source is checked too — that the app sends the approver's session,
that it shows the server's own preview rather than reconstructing one, and
that the Command Center offers the approval where the action was requested.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _swift(*parts: str) -> str:
    return (REPO.joinpath("macos", "Vantage", "Sources", *parts)).read_text(
        encoding="utf-8")


# ---------------------------------------------------------------------------
# The contract, server side
# ---------------------------------------------------------------------------
def test_the_confirmations_endpoint_sends_the_fields_the_app_decodes():
    """Every field the app's model declares is one the server actually sends.

    This is the check that would have caught the original break: the model
    declared `id`, `action`, `description`, `device_id` and `expires_at`,
    none of which the server has ever sent.
    """
    from fastapi.testclient import TestClient

    from jarvis.api.app import app

    client = TestClient(app, base_url="http://127.0.0.1:8765")
    from tests.conftest import TEST_TOKEN

    response = client.get("/confirmations",
                          headers={"X-JARVIS-Token": TEST_TOKEN})
    assert response.status_code == 200, response.text
    rows = response.json()
    assert isinstance(rows, list), rows

    # The shape of a row, from the app's own model. Parsed out of the Swift
    # source so the two cannot drift.
    model = _swift("APIClient", "APIModels.swift")
    block = re.search(
        r"struct Confirmation: Codable, Identifiable \{(.*?)\n    \}", model, re.S)
    assert block, "the Confirmation model is gone from APIModels.swift"
    declared = set(re.findall(r"let (\w+):", block.group(1)))
    declared.discard("id")  # a computed property, not a stored field

    if not rows:
        # Nothing pending: assert the shape from the backend's own mapper
        # instead of from a live row.
        from jarvis.security.policy import PermissionEngine
        from jarvis.registry.tools import ToolRegistry
        from jarvis.tools import build_default_registry

        engine = PermissionEngine(build_default_registry(), None, None)
        produced = engine._row_to_confirmation({
            "confirmation_id": "CONF-x", "request_id": "", "session_id": "s",
            "tool": "browser_save", "params": "{}", "device": "laptop_main",
            "risk": 2, "preview": "p", "state": "pending",
            "created_at": "2026-01-01T00:00:00", "resolved_at": None,
            "requested_by_session": "s", "requested_surface": "dashboard",
            "approved_by_session": "", "approved_surface": "",
        })
        produced_keys = set(produced.keys())
    else:
        produced_keys = set(rows[0].keys())

    missing = declared - produced_keys
    assert not missing, (
        f"the app's Confirmation model declares fields the server never sends: "
        f"{sorted(missing)}"
    )


def test_the_server_sends_the_fields_the_app_reads():
    """And the other way round: the app reads what the server sends."""
    from jarvis.security.policy import PermissionEngine
    from jarvis.tools import build_default_registry

    engine = PermissionEngine(build_default_registry(), None, None)
    produced = engine._row_to_confirmation({
        "confirmation_id": "CONF-x", "request_id": "", "session_id": "s",
        "tool": "browser_save", "params": "{}", "device": "laptop_main",
        "risk": 2, "preview": "browser_save on laptop_main", "state": "pending",
        "created_at": "2026-01-01T00:00:00", "resolved_at": None,
        "requested_by_session": "s", "requested_surface": "dashboard",
        "approved_by_session": "", "approved_surface": "",
    })
    for field in ("confirmation_id", "tool", "preview", "device", "state",
                  "created_at", "session_id"):
        assert field in produced, f"the server does not send {field}"


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------
def test_the_approvals_screen_shows_the_tool_and_the_servers_preview():
    """The card shows what will happen, in the server's own words.

    It used to show `confirmation.action` — a field that does not exist — so
    every card read "Unknown action", and the description came from a
    `description` field that was never sent either. The preview is the
    server's one-line description of the effect, and the app must not
    reconstruct it from the parameters: getting it subtly wrong is exactly
    what a confirmation card exists to prevent.
    """
    source = _swift("Views", "ApprovalsView.swift")
    assert "confirmation.displayTitle" in source, (
        "the card does not show the tool it is about")
    assert "confirmation.displayDetail" in source, (
        "the card does not show the server's preview of the effect")
    # The fields that never existed must be gone.
    assert "confirmation.action" not in source, (
        "the card still reads a field the server never sends")
    assert "confirmation.description" not in source, (
        "the card still reads a field the server never sends")
    assert "confirmation.device_id" not in source, (
        "the card still reads a field the server never sends")


def test_approving_sends_the_approvers_session():
    """The backend records *who* answered, so the app must say who it is.

    A confirmation answered on the phone and one answered on the laptop are
    not the same fact, and the audit needs to tell them apart.
    """
    source = _swift("APIClient", "APIClient.swift")
    assert "session_id" in source, "the confirm call does not carry a session"
    assert "resolveConfirmation" in source, (
        "the confirm call is not a named method, so every caller builds its own URL")
    assert "approve" in source, "the confirm call does not say whether it approved"


def test_the_approvals_screen_uses_the_shared_confirm_method():
    """One way to answer an approval, not two."""
    source = _swift("Views", "ApprovalsView.swift")
    assert "api.resolveConfirmation(" in source, (
        "the approvals screen builds its own confirm URL instead of using the "
        "shared method")


def test_the_command_center_offers_the_approval_where_the_action_was_requested():
    """The person who asked for the action can answer it.

    The Command Center said "Requires confirmation" and offered no way to
    confirm, so a consequential action could never complete from the desktop.
    It now shows the pending action with its approve and deny controls.
    """
    source = _swift("Views", "CommandCenterView.swift")
    assert "pendingConfirmations" in source, (
        "the Command Center does not hold the pending approvals")
    assert "ConfirmationCard(" in source, (
        "the Command Center does not show the approval")
    assert "resolveConfirmation" in source or "resolve(" in source, (
        "the Command Center cannot answer an approval")
    assert "refreshConfirmations" in source, (
        "the Command Center never loads the pending approvals")


def test_the_approvals_screen_reaches_the_confirmations_endpoint():
    """It asks the backend for the list rather than inventing one."""
    source = _swift("APIClient", "APIClient.swift")
    assert '"/confirmations"' in source, (
        "the app does not read the backend's pending confirmations")
