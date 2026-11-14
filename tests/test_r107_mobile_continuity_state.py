"""E11 — where desktop/mobile continuity actually stands, pinned.

Mobile continuity is **not** implementable-and-verifiable in this environment,
and the reason is specific rather than vague. This file records the state so the
next session starts from facts:

1. **The phone surface is deliberately narrow, at two layers.** A phone session
   can reach only `PHONE_ALLOWED` at the backend, and the companion gateway's own
   allowlist must remain a *strict subset* of that. The new product surfaces
   (`/planner/*`, `/work`, `/operator`, `/attention`) are **not** on it. Widening
   it is a security decision about what a lost phone could read — not a parity
   afterthought — so it is left to the owner.

2. **There is no Android runtime here.** No emulator binary, no system images, no
   attached device. The Android client has no planner surface today; adding an
   unverifiable UI is exactly the "compiles, therefore works" claim this project
   refuses.

The client itself is real and tested (`test_r87`); this file adds only the
boundary facts, so nobody re-derives them.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def test_the_new_product_surfaces_are_not_on_the_phone_allowlist():
    """Narrow by design: the phone may not read the planner/work/operator data."""
    from jarvis.companion.gateway import PHONE_ALLOWED

    allowed = set(PHONE_ALLOWED)
    for product_path in ("/planner/tasks", "/planner/today", "/attention", "/work", "/operator"):
        assert product_path not in allowed, (
            f"{product_path} is on the phone allowlist; that widens the phone "
            "surface and is an owner security decision, not a parity fix")
    # What the phone *does* get stays as it was.
    assert {"/command", "/confirmations", "/devices", "/health"} <= allowed


def test_the_gateway_allowlist_stays_a_strict_subset_of_the_backend_surface():
    """The two-layer rule: the gateway may never grant more than the backend."""
    src = (REPO / "tests" / "test_companion_gateway.py").read_text()
    assert "BACKEND_PHONE_ALLOWED" in src and "PHONE_ALLOWED - {\"/health\"}" in src, (
        "the subset relationship between the two allowlists must stay pinned")


def test_the_android_client_still_has_no_planner_surface():
    """No unverifiable Android UI: the state recorded at iteration 87 still holds.

    If a planner surface is added to the Android client, this test fails — which
    is the prompt to also add a real device/emulator check, not to delete the
    assertion.
    """
    root = REPO / "android" / "app" / "src" / "main"
    hits = [p for p in root.rglob("*.kt") if "planner" in p.read_text(encoding="utf-8").lower()]
    assert not hits, f"an Android planner surface appeared ({hits}); verify it on a device"


@pytest.mark.skipif(shutil.which("adb") is not None
                    and Path.home().joinpath("Library/Android/sdk").exists(),
                    reason="an Android SDK is present; check for a device/emulator")
def test_no_android_runtime_is_available_here():
    """Records the external prerequisite rather than asserting a runtime result."""
    sdk = Path.home() / "Library" / "Android" / "sdk"
    assert not (sdk / "emulator").exists(), "an emulator appeared; run device-level verification"
    assert not (sdk / "system-images").exists(), "a system image appeared; run it"
