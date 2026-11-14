"""The capability registry reports what a model's capabilities are, **as evidenced**.

Iteration 78's state pinned the registry as *dead and dangerous*: wired up as a
request gate it would refuse a working provider, because ``can_handle_request``
reads only a stale hand-maintained profile and is never told about a capability
anything actually proved (R6.4, ``test_r88_*``). That gate stays dead on purpose.

This iteration resolves the "give it a real source of truth" half of R6.4 without
turning the registry into a gate: the **only** trustworthy source of "this model
can actually do the thing" is the route-health ledger, which records every real
completion per provider/model. ``CapabilityRegistry.capability_report`` reads
that ledger and reports each capability as:

* ``verified``  -- a completion to this provider/model succeeded (real evidence);
* ``declared``  -- a static profile names it but nothing has proved it;
* ``unknown``   -- neither.

The report is surfaced in ``GET /ai/providers/{id}/health`` -- the endpoint the
dashboard's provider page already polls -- so the registry is *read* in the live
path as a report, never as a decision about whether a request may run. The two
r88 invariants this must not break: a working provider with no evidence is not
reported as incapable-by-a-gate (the gate is gone), and ``can_handle_request``
still returns ``False`` for an unproven profile (so nothing is tempted to wire
it back up as a gate).
"""

from __future__ import annotations

from typing import List

import pytest

from jarvis.gateway.route_health import get_route_health, reset_route_health
from jarvis.security.providers import CapabilityRegistry, CapabilityType, get_registry


# ---------------------------------------------------------------------------
# capability_report: the three honest tiers
# ---------------------------------------------------------------------------

def test_no_evidence_reports_unknown_not_incapable():
    """A working model with no recorded success is *unknown*, not *incapable*.

    This is the exact flip of the r88 defect: the old gate said "False" (can't
    chat) for a provider that was answering traffic. The report says "no evidence
    yet", which a UI can show truthfully instead of refusing the request.
    """
    reset_route_health()
    try:
        report = get_registry().capability_report("9router", "ag/gemini-2.5-flash")
        assert report["capabilities"][CapabilityType.TEXT.value] == "unknown"
        # No capability is claimed verified with nothing behind it.
        assert not [c for c, t in report["capabilities"].items() if t == "verified"]
    finally:
        reset_route_health()


def test_a_real_completion_verifies_text():
    """A recorded success is the evidence that makes a capability *verified*."""
    reset_route_health()
    try:
        get_route_health().record("9router", "ag/gemini-2.5-flash", ok=True, source="chat")
        report = get_registry().capability_report("9router", "ag/gemini-2.5-flash")
        assert report["capabilities"][CapabilityType.TEXT.value] == "verified"
        assert report["last_success_at"] is not None
    finally:
        reset_route_health()


def test_a_failure_does_not_verify_anything():
    """A recorded failure is not evidence of capability; the capability stays unknown."""
    reset_route_health()
    try:
        get_route_health().record("9router", "m", ok=False, source="chat",
                                  status=429, category="rate_limited")
        report = get_registry().capability_report("9router", "m")
        assert report["capabilities"][CapabilityType.TEXT.value] == "unknown"
        assert not [c for c, t in report["capabilities"].items() if t == "verified"]
    finally:
        reset_route_health()


def test_a_declared_capability_with_no_evidence_is_declared_not_verified():
    """A static profile names vision for gpt-4o, but no completion has proved it.

    The report must not promote a profile claim to *verified*: claiming a model
    can do vision just because it answered a text prompt once is the "name in a
    registry means the model can do it" error this report exists to prevent.
    """
    reset_route_health()
    try:
        report = get_registry().capability_report("openai", "gpt-4o")
        # gpt-4o's static profile declares vision and tool calling.
        assert report["capabilities"][CapabilityType.VISION.value] == "declared"
        assert report["capabilities"][CapabilityType.TOOL_CALLING.value] == "declared"
        # ...but with no recorded completion, even text is declared, not verified.
        assert report["capabilities"][CapabilityType.TEXT.value] == "declared"
    finally:
        reset_route_health()


def test_only_text_is_derivable_from_a_completion():
    """A success proves the model did *text*; it does not prove vision or PDF.

    The report must be conservative on everything but the capability a completion
    is direct evidence of.
    """
    reset_route_health()
    try:
        get_route_health().record("openai", "gpt-4o", ok=True, source="chat")
        report = get_registry().capability_report("openai", "gpt-4o")
        assert report["capabilities"][CapabilityType.TEXT.value] == "verified"
        # Declared-but-unprovisioned capabilities do not become verified by a text reply.
        assert report["capabilities"][CapabilityType.VISION.value] != "verified"
        assert report["capabilities"][CapabilityType.PDF_INPUT.value] != "verified"
    finally:
        reset_route_health()


def test_verification_is_keyed_by_provider_and_model():
    """Evidence for one provider/model must not bleed into another's report."""
    reset_route_health()
    try:
        get_route_health().record("9router", "a", ok=True, source="chat")
        # A different model on the same provider has no evidence.
        assert get_registry().capability_report(
            "9router", "b")["capabilities"][CapabilityType.TEXT.value] == "unknown"
        # And the one that succeeded is verified.
        assert get_registry().capability_report(
            "9router", "a")["capabilities"][CapabilityType.TEXT.value] == "verified"
    finally:
        reset_route_health()


def test_can_handle_request_remains_the_conservative_gate_check():
    """r88's invariant: the *gate* check is still declared-only and conservative.

    It must keep returning False for a working-but-unproven provider, so that
    wiring it up as a request gate would still refuse everything -- the danger
    the report is the answer to. The report and the gate must not be conflated.
    """
    reset_route_health()
    try:
        reg = get_registry()
        # Even with a recorded success, the *gate* check is unchanged (declared-only),
        # so a gate built on it would still refuse a provider with no static profile.
        get_route_health().record("9router", "ag/gemini-2.5-flash", ok=True, source="chat")
        assert reg.can_handle_request(
            "9router", "ag/gemini-2.5-flash", [CapabilityType.TEXT]) is False
    finally:
        reset_route_health()


# ---------------------------------------------------------------------------
# the report is surfaced in the live path (/health), as a report not a gate
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from jarvis.api.app import app
    return TestClient(app, base_url="http://127.0.0.1:8765")


def _headers():
    return {"X-JARVIS-Token": "test-token-do-not-use-in-production", "Host": "127.0.0.1"}


def test_health_reports_verified_capability_after_a_completion(client, monkeypatch):
    """The runtime path: a real completion -> /health shows verified capability.

    This is the demonstration the registry *affects capability reporting* in the
    live path. The dashboard's provider page already polls /health, so the
    verified capability reaches the UI without a new endpoint.
    """
    import sys
    api = sys.modules["jarvis.api.app"]
    from jarvis.gateway.route_health import get_route_health
    from jarvis.gateway.selection import save_provider_config

    reset_route_health()
    try:
        monkeypatch.setattr(api.KeychainStorage, "retrieve",
                            staticmethod(lambda pid: "sk-test-123"))
        # Pin the model the route will query, via the same per-provider
        # selection a user sets in the settings screen. The completion is
        # recorded under this exact (provider, model) key, so the report the
        # route reads lines up with the evidence chat() would have recorded.
        model = "ag/gemini-2.5-flash"
        save_provider_config("9router", home=api.get_settings().home,
                             model=model)
        monkeypatch.setattr(api.get_settings(), "llm_model", model, raising=False)

        # Before any completion: nothing is verified.
        body = client.get("/ai/providers/9router/health", headers=_headers()).json()
        assert body["model"] == model  # the route is querying the pinned model
        assert body["capability_verified"] == []
        assert body["capability"][CapabilityType.TEXT.value] == "unknown"

        # A real completion is recorded (this is what chat() does on success).
        get_route_health().record("9router", model, ok=True, source="chat")

        body = client.get("/ai/providers/9router/health", headers=_headers()).json()
        assert body["capability_verified"] == [CapabilityType.TEXT.value]
        assert body["capability"][CapabilityType.TEXT.value] == "verified"
    finally:
        reset_route_health()


def test_health_capability_is_a_report_and_does_not_refuse(client, monkeypatch):
    """Surfacing the capability must not turn the endpoint into a gate.

    An unproven (or unconfigured) provider still gets a 200 health response with
    an honest capability report -- the GET never blocks, rejects, or refuses a
    request based on it.
    """
    import sys
    api = sys.modules["jarvis.api.app"]
    from jarvis.gateway.route_health import get_route_health

    reset_route_health()
    try:
        monkeypatch.setattr(api.KeychainStorage, "retrieve",
                            staticmethod(lambda pid: "sk-test-123"))
        monkeypatch.setattr(api.get_settings(), "llm_model", "some-model", raising=False)

        r = client.get("/ai/providers/openai/health", headers=_headers())
        assert r.status_code == 200, r.text
        body = r.json()
        # The report is present and honest (no evidence -> nothing verified),
        # but the endpoint itself succeeded.
        assert "capability" in body and "capability_verified" in body
        assert body["capability_verified"] == []
    finally:
        reset_route_health()


def test_registry_is_consulted_by_the_health_route():
    """The live-path reader of the registry is the report, not the gate.

    This records *where* the registry is read, so a future change that either
    wires the gate back up as a decision or drops the report both surface here
    instead of the two r88/r94 files silently disagreeing about what is live.
    """
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent
              / "jarvis" / "api" / "app.py").read_text(encoding="utf-8")
    assert "capability_report(" in source, (
        "the /health route no longer reads the capability report; the registry "
        "is dead again and this file must be updated")
    # And the gate method is still NOT wired as a request decision in the API.
    assert "check_ai_capability(" not in source, (
        "check_ai_capability (a gate that refuses a working provider) is now "
        "called in the API; that is the R6.4 danger and this file must be updated")


# ---------------------------------------------------------------------------
# the dashboard renders the verified capability (the user-visible half)
# ---------------------------------------------------------------------------

def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return False
    return True


playwright_required = pytest.mark.skipif(
    not _playwright_available(),
    reason="playwright is not installed here (see docs/RELEASE_AND_BUILD.md)",
)


@playwright_required
def test_ai_connections_page_renders_verified_capability(isolated_server):
    """The report is not just on the wire: the provider page shows it.

    ``ai_connections.html`` polls ``/ai/providers/{id}/health`` for every card and
    renders the response into each card's health box. This drives the real page
    against the isolated server (never the live backend) and intercepts the
    health response to carry the new ``capability_verified`` field, then checks
    the box actually shows it. Without the HTML change the box would show the
    same text minus "verified: text", so this catches a report that is computed
    but never displayed.
    """
    from playwright.sync_api import sync_playwright

    base = isolated_server["base"]

    def fulfill_health(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=(
                '{"provider_id": "9router", "model": "ag/gemini-2.5-flash", '
                '"configured": true, "gateway_reachable": true, '
                '"route_health": "healthy", "route_health_label": "Healthy", '
                '"last_success_at": 1.0, "last_latency_ms": 380, '
                '"upstream_model": "", "capability": {"text": "verified"}, '
                '"capability_verified": ["text"], "message": "Healthy"}'
            ),
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors: List[str] = []
        page.on("pageerror", lambda e: page_errors.append(str(e)[:120]))
        # Intercept every health poll; all cards render the verified capability.
        page.route("**/ai/providers/*/health", fulfill_health)
        page.goto(f"{base}/ai-connections", wait_until="networkidle")
        page.wait_for_timeout(2000)

        boxes = page.locator(".js-health").all_inner_texts()
        assert boxes, "the provider page rendered no health boxes"
        assert any("verified: text" in b for b in boxes), (
            f"the health box does not show the verified capability: {boxes}")
        browser.close()

    assert not page_errors, f"the page raised: {page_errors}"
