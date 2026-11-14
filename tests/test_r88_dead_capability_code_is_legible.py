"""The dead capability registry must not be mistaken for a live feature.

**`find_compatible_providers` is the only code in the project that reads
`free_first` and `allow_paid_fallback`** — and nothing in the live path
calls it. The dashboard's "AI Usage Controls" used to offer three checkboxes
for exactly those settings; they were removed in iteration 76 because a
control that looks real and does nothing is worse than no control (it says
"paid fallback is off" when nothing was ever reading the switch).

The wider subsystem around it is dead too, and **dangerous** if it is ever
wired up naively:

    CapabilityRegistry().can_handle_request("9router", "ag/gemini-2.5-flash",
                                            [CapabilityType.TEXT])
    -> False

That is a provider which **is** serving traffic — it answers the assistant's
questions right now — judged incapable of a text request. The registry only
knows a capability if something explicitly told it about one
(`update_model_capabilities`), and nothing in the live path does that. So
wiring this in as a gate would refuse every request.

This file does not delete the code: `jarvis/security/providers.py` is
tracked, and `jarvis/security/providers.py.multimodal` is an **untracked
file the owner owns** (a stale copy of the same module). Neither is
removed here — deleting the owner's untracked file is not this test's call.
What this file does is make the state legible, so the next person does not
either wire up a gate that would refuse everything, or leave a "feature"
that is not there.

**What this file does and does not cover (updated iteration 80).** This file
pins that the *gate* is dead and must stay dead: `check_ai_capability` and
`find_compatible_providers` (the free/paid readers) are still not called in
the live path, and `can_handle_request` is still the conservative declared-only
check that would refuse a working provider if wired up. Iteration 80 gave the
registry a *separate* live reader — `CapabilityRegistry.capability_report`,
surfaced in `GET /ai/providers/{id}/health` — which reports verified capability
from the route-health ledger. That is a report, not a decision about whether a
request may run, so it is out of scope for these gate assertions; it is pinned
in `test_r94_verified_capability_report.py`. The two files are meant to be read
together: the gate stays dead, the report is live.

The properties asserted here are the ones that would break first if the
code were wired up: the registry must not claim a working provider cannot
chat, and the free/paid settings must not be reachable from a UI that
promises to honour them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PROVIDERS = REPO / "jarvis" / "security" / "providers.py"
MULTIMODAL = REPO / "jarvis" / "security" / "providers.py.multimodal"
DASHBOARD = REPO / "dashboard"


def _py(*parts: str) -> str:
    return REPO.joinpath(*parts).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The capability registry would refuse a working provider
# ---------------------------------------------------------------------------
def test_the_capability_registry_would_judge_a_working_provider_incapable():
    """A live provider must not be reported as unable to chat.

    This is the reason the subsystem must not be wired up as a gate as it
    stands: the registry is only told about a capability explicitly, and
    nothing in the live path tells it. Measured on the configured provider:

        can_handle_request("9router", "ag/gemini-2.5-flash", [TEXT]) -> False

    A gate built on that would refuse every request from a provider that is
    answering them right now.
    """
    from jarvis.security.providers import CapabilityRegistry, CapabilityType

    registry = CapabilityRegistry()
    capable = registry.can_handle_request(
        "9router", "ag/gemini-2.5-flash", [CapabilityType.TEXT])

    assert capable is False, (
        "the capability registry now reports a provider as capable of a text "
        "request; if that is because something real populates it, the "
        "comment in jarvis/security/providers.py and this file must both be "
        "updated -- a gate built on it would then be meaningful")


def test_the_capability_registry_is_never_consulted_by_the_live_path():
    """It is dead code, and it must stay harmless dead code.

    `CapabilityRegistry` is constructed by the orchestrator, and
    `check_ai_capability` is defined on it — but nothing calls
    `check_ai_capability`, and nothing calls `find_compatible_providers`.
    """
    source = _py("jarvis", "core", "orchestrator.py")
    assert "check_ai_capability" in source, (
        "the orchestrator's capability method is gone; update this file")
    # It is defined but never called from anywhere in the package. The
    # defining module itself is excluded: the definition is not a call.
    for path in (REPO / "jarvis").rglob("*.py"):
        if path.name.endswith(".multimodal") or path.name == "providers.py":
            continue
        text = path.read_text(encoding="utf-8")
        calls = re.findall(r"\.check_ai_capability\(|find_compatible_providers\(",
                           text)
        if path.name == "orchestrator.py":
            # The definition itself, not a call.
            continue
        assert not calls, (
            f"{path} calls the capability registry; it is dead code that "
            "would refuse a working provider, so this file must be updated")


def test_the_free_and_paid_settings_have_no_live_caller():
    """`free_first` and `allow_paid_fallback` are read by one dead method.

    The dashboard's usage-control checkboxes were removed in iteration 76
    for exactly this reason. If a UI ever offers these again, it must have
    a real setting behind it.
    """
    source = PROVIDERS.read_text(encoding="utf-8")
    assert "free_first" in source, (
        "the free/paid parameters are gone from the providers module; update "
        "this file")
    assert "allow_paid_fallback" in source

    # The only readers are the dead method's own signature and body.
    for path in (REPO / "jarvis").rglob("*.py"):
        if path.name.endswith(".multimodal"):
            continue
        text = path.read_text(encoding="utf-8")
        if path.name == "providers.py":
            continue
        assert "free_first" not in text, (
            f"{path} reads free_first; the setting now has a live reader and "
            "this file must be updated")
        assert "allow_paid_fallback" not in text, (
            f"{path} reads allow_paid_fallback; the setting now has a live "
            "reader and this file must be updated")


def test_no_dashboard_page_offers_a_usage_control_with_nothing_behind_it():
    """The three dead checkboxes must not come back.

    They had no handler, no route and no setting behind them. A page that
    offers them again is offering a switch that does nothing.
    """
    for page in DASHBOARD.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        for name in ("use-free-first", "allow-paid-fallback",
                     "require-paid-confirmation"):
            assert name not in text, (
                f"{page.name} offers the {name!r} control, which has no "
                "setting behind it")


def test_the_stale_multimodal_copy_is_not_shipped():
    """`providers.py.multimodal` is the owner's stale scratch copy.

    It is a 316-line stale copy of the live 619-line module, carrying the same
    dead free/paid code, and nothing imports it. Publishing it into a fresh
    clone would ship dead, confusing code — so it is **excluded from git** while
    being left on disk for its owner (not deleted). This test records that
    decision (2026-11-14, R0.3 closed): the file is not tracked, and the live
    module is not a copy of it.
    """
    if not MULTIMODAL.exists():
        return  # already removed by whoever owns it
    import subprocess

    result = subprocess.run(
        ["git", "ls-files", "jarvis/security/providers.py.multimodal"],
        cwd=str(REPO), capture_output=True, text=True)
    assert not result.stdout.strip(), (
        "the stale .multimodal copy is tracked; it is dead duplicate code and "
        "should be gitignored, not shipped")
    # And it really is a *copy*, not the live module.
    assert MULTIMODAL.read_text(encoding="utf-8") != PROVIDERS.read_text(
            encoding="utf-8"), (
        "the .multimodal copy is no longer a copy; it may have diverged and "
        "this file must be updated")
