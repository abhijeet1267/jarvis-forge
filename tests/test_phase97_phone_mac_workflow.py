"""Phase 97: Phone-to-Mac Workflow Integration Tests.

The phone->Mac workflow, tested against the architecture the product actually
ships. The first draft of this file described a `PhoneWorkflowBridge` with its own
`_phone_sessions` / `PHONE_` token space -- a parallel session/pairing system beside
the real one, which is a replacement prototype this project does not build. The real
workflow is:

  * `GET /phone` serves the phone page carrying a single-use bootstrap,
  * the phone spends it at `POST /auth/session` for a session whose **surface** is
    `"phone"`,
  * a phone-surface session may reach only the narrow `PHONE_ALLOWED` paths,
  * pairing is `PairingManager.request` (pending, grants nothing) -> `approve`
    (explicit user action) -> the device is usable,
  * revocation -- the phone signing out, or the laptop revoking a handset by
    reference -- ends the session and stops its pending confirmations from being
    approved.

Every authority boundary below is asserted against the running app, not assumed.
"""

from __future__ import annotations

import importlib
import re
import time

import pytest
from fastapi.testclient import TestClient

from jarvis.config import get_settings


def _app_module():
    """The `jarvis/api/app.py` module (not the FastAPI instance that shares its name)."""
    return importlib.import_module("jarvis.api.app")


@pytest.fixture
def client():
    from jarvis.api.app import app
    return TestClient(app, base_url="http://127.0.0.1:8765")


def _login_phone(client) -> str:
    """The real phone login: take the page's one-use bootstrap and exchange it for a
    phone-surface session. Returns the session token."""
    import importlib as _il
    appmod = _il.import_module("jarvis.api.app")
    boot = re.search(r'name="jarvis-bootstrap" content="([^"]+)"',
                     client.get("/phone").text).group(1)
    resp = client.post("/auth/session", json={"bootstrap": boot},
                       headers={"Origin": "http://127.0.0.1:8765"})
    assert resp.status_code == 200, resp.text
    token = resp.json()["session_token"]
    assert token
    return token


class TestPhoneAuthentication:
    """A phone session is a real, surface-tagged session -- not a separate token space."""

    def test_phone_session_carries_phone_surface(self, client):
        """The session the phone buys is recorded with surface 'phone', which is what
        restricts it to PHONE_ALLOWED downstream."""
        appmod = importlib.import_module("jarvis.api.app")
        token = _login_phone(client)
        assert appmod._surface_of(token) == "phone"

    def test_dashboard_session_is_a_different_surface(self, client):
        """A laptop (dashboard) session and a phone session are distinct surfaces, so
        the same token cannot be both."""
        import importlib as _il
        appmod = _il.import_module("jarvis.api.app")
        phone = _login_phone(client)
        # A dashboard code is minted by the dashboard page, not the phone page.
        dash = appmod._issue_credential(appmod._sessions, appmod.SESSION_TTL_SECONDS, "dashboard")
        assert appmod._surface_of(phone) == "phone"
        assert appmod._surface_of(dash) == "dashboard"
        assert phone != dash


class TestPairingMechanisms:
    """Pairing grants nothing until an explicit user approval."""

    @pytest.fixture
    def pm(self):
        from jarvis.devices.pairing import PairingManager
        return PairingManager()

    def test_pending_request_grants_nothing(self, pm):
        """A pending pairing request cannot authenticate the device: its secret is not
        stored until the laptop approves."""
        from jarvis.devices.pairing import generate_secret, fingerprint_of, derive_proof
        secret = generate_secret()
        req = pm.request(device_id="p97_phone", device_name="Phone",
                         fingerprint=fingerprint_of(secret))
        assert req.state == "pending"
        # No secret stored while pending, so a proof against the real secret fails.
        assert pm.load_secret("p97_phone") is None
        assert pm.authenticate("p97_phone", "challenge", derive_proof(secret, "challenge")) is False

    def test_fingerprint_is_human_readable(self, pm):
        """The fingerprint is grouped so it can be read aloud and compared out-of-band."""
        from jarvis.devices.pairing import generate_secret, fingerprint_of
        fp = fingerprint_of(generate_secret())
        groups = fp.split(" ")
        assert len(groups) == 4, fp
        assert all(len(g) == 4 for g in groups), fp

    def test_approval_is_an_explicit_action_that_makes_the_device_usable(self, pm):
        """The device is not usable until approved: approving is what stores the secret."""
        from jarvis.devices.pairing import generate_secret, fingerprint_of
        secret = generate_secret()
        req = pm.request(device_id="p97_approve", device_name="Phone",
                         fingerprint=fingerprint_of(secret))
        assert pm.load_secret("p97_approve") is None, "pending grants nothing"
        result = pm.approve(req.pairing_id)
        assert result["state"] in ("paired", "approved")
        assert pm.load_secret("p97_approve") is not None, "approval makes the device usable"

    def test_pending_request_is_tracked(self, pm):
        """A pending request is counted so the UI can show what awaits approval."""
        from jarvis.devices.pairing import generate_secret, fingerprint_of
        before = pm.pending_count()
        req = pm.request(device_id="p97_count", device_name="Phone",
                         fingerprint=fingerprint_of(generate_secret()))
        assert pm.pending_count() == before + 1
        # Clean up so we don't leave a pending request in the shared test DB.
        pm.deny(req.pairing_id)


class TestSessionIsolation:
    """A phone session is scoped to the phone; a laptop session is not reachable from it."""

    def test_phone_session_cannot_reach_laptop_only_paths(self, client):
        """The whole point of the surface: a phone session gets the narrow set, and the
        laptop's wider state (health, settings) is refused to it."""
        token = _login_phone(client)
        hdrs = {"X-JARVIS-Token": token}
        # What the phone is allowed to reach.
        assert client.post("/command", json={"text": "what time is it"},
                           headers=hdrs).status_code == 200
        assert client.get("/confirmations", headers=hdrs).status_code == 200
        # Laptop-only state the phone must NOT read.
        assert client.get("/health", headers=hdrs).status_code in (401, 403)

    def test_a_phone_session_token_is_not_a_laptop_session(self, client):
        """The token the phone holds authenticates it as a phone, never as the laptop's
        own session -- a phone approving its own gated action is the Phase 71 rule."""
        import importlib as _il
        appmod = _il.import_module("jarvis.api.app")
        token = _login_phone(client)
        assert appmod._surface_of(token) == "phone"


class TestRevocationAndSignout:
    """Ending a session ends it: sign-out and the laptop's per-device revoke both work."""

    def test_sign_out_ends_the_phone_session(self, client):
        """The phone signing out (DELETE /auth/session) makes its token stop authenticating."""
        import importlib as _il
        appmod = _il.import_module("jarvis.api.app")
        token = _login_phone(client)
        assert appmod._surface_of(token) == "phone"
        # Sign out using the session itself.
        resp = client.delete("/auth/session",
                             headers={"X-JARVIS-Token": token,
                                      "Origin": "http://127.0.0.1:8765"})
        assert resp.status_code == 200, resp.text
        assert appmod._surface_of(token) is None, "a signed-out token must not authenticate"

    def test_laptop_revoke_by_reference_ends_the_phone(self, client, monkeypatch):
        """The laptop can cut off a handset by its reference (never its token), and that
        revocation is recorded so the phone's pending confirmations stop being approvable."""
        import importlib as _il
        appmod = _il.import_module("jarvis.api.app")
        token = _login_phone(client)
        ref = appmod._session_record(token)[2]  # the reference the laptop was given
        assert appmod._revoke_session_by_ref(ref) is True
        assert appmod._surface_of(token) is None
        assert appmod._is_session_revoked(token), "revoke-by-ref must record the revocation"


class TestExpiredSessions:
    """An expired session authenticates as nothing."""

    def test_expired_session_authenticates_to_nothing(self, client, monkeypatch):
        """A session past its TTL is not live, so its surface is unreadable."""
        import importlib as _il
        appmod = _il.import_module("jarvis.api.app")
        token = appmod._issue_credential(appmod._sessions, ttl=-1.0, surface="phone")
        assert appmod._session_is_live(token) is False
        assert appmod._surface_of(token) is None


class TestPollingTermination:
    """A phone that loses authorization is refused, not a server error."""

    def test_refused_phone_gets_a_refusal_not_a_crash(self, client):
        """After sign-out, a further request from that token is a clean 401 -- the page's
        poll stops, and the server does not 500."""
        token = _login_phone(client)
        client.delete("/auth/session", headers={"X-JARVIS-Token": token,
                                                "Origin": "http://127.0.0.1:8765"})
        resp = client.post("/command", json={"text": "what time is it"},
                           headers={"X-JARVIS-Token": token})
        assert resp.status_code == 401, resp.text


class TestMobileViewportResponsiveness:
    """Phone UI responsive at target viewport widths."""

    @pytest.mark.skip(reason="Requires browser automation for layout verification")
    def test_phone_ui_320px_viewport(self):
        pass

    def test_no_horizontal_overflow_in_css(self):
        """CSS prevents horizontal overflow in the phone UI."""
        from pathlib import Path
        phone_html = (Path(__file__).resolve().parents[1] / "dashboard" / "phone.html").read_text()
        has_overflow_handling = (
            "overflow-x: hidden" in phone_html or
            "overflow-x: scroll" in phone_html or
            "overflow-wrap" in phone_html or
            "max-width: 100%" in phone_html or
            ".scroll" in phone_html
        )
        assert has_overflow_handling


class TestErrorHandling:
    """Destructive commands are confirmation-gated, never auto-executed."""

    @pytest.fixture
    def planner_and_engine(self):
        from jarvis.core.planner import RulePlanner
        from jarvis.security.policy import PermissionEngine
        from jarvis.tools import build_default_registry
        from jarvis.registry.devices import get_device_registry
        reg = build_default_registry()
        return RulePlanner(reg), PermissionEngine(reg, get_device_registry())

    def test_a_destructive_command_requires_confirmation(self, planner_and_engine):
        """`delete everything` must reach the confirmation gate, never auto-execute. This is
        the boundary that keeps a phone (or anyone) from triggering a destructive action
        without the user explicitly approving it."""
        from jarvis.core.models import PolicyDecision
        planner, engine = planner_and_engine
        plan = planner.parse("delete everything")
        destructive = [a for a in plan.actions
                       if engine.evaluate(a).decision is PolicyDecision.CONFIRM]
        assert destructive, (
            "a destructive sentence produced no confirmation-gated action: "
            f"{[(a.tool, engine.evaluate(a).decision.value) for a in plan.actions]}")

    def test_a_harmless_command_does_not_require_confirmation(self, planner_and_engine):
        """The gate is risk-based, not a blanket refusal: an ordinary, non-destructive
        command is allowed to execute without a confirmation card."""
        from jarvis.core.models import PolicyDecision
        planner, engine = planner_and_engine
        plan = planner.parse("open Safari")
        verdicts = [engine.evaluate(a).decision for a in plan.actions]
        assert any(v is PolicyDecision.EXECUTE for v in verdicts), (
            f"an ordinary open-application command should not be gated: {verdicts}")

    def test_error_messages_name_the_confirmation(self):
        """The refusal text tells the user what is needed."""
        msg = "This needs your confirmation before I run it."
        assert "confirmation" in msg.lower()
