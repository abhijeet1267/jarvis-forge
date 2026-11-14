"""E12 — the DMG ships the NATIVE app by default; the choice is recorded, not implicit.

Two bundles exist by design: the Tk launcher (`com.jarvisforge.app`, the TCC
identity carrier) and the native SwiftUI GUI (`com.jarvisforge.vantage`).

**The decision has since been made**: the native app is the product. The earlier
default shipped the launcher, and because that bundle contains no SwiftUI
screens, a person who built the app the documented way saw an older interface
than the one in `Sources/` — the Settings voice control and the redesigned
screens never reached the running app. The default is now `--native`, and
`--launcher` remains for the TCC path and as an explicit fallback.

These tests pin the *new* decision and keep the two properties that made the
original safe: either bundle can still be selected explicitly, and the build
always states which one it shipped.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "installers" / "macos" / "build_dmg.sh"


def test_the_script_defaults_to_the_native_app_and_can_select_either():
    src = SCRIPT.read_text()
    assert "SHIP_NATIVE=1" in src, "the default must ship the native app (the product)"
    assert "--launcher" in src, "the launcher must remain explicitly selectable (TCC path)"
    assert "--native" in src, "there is no way to package the native app explicitly"
    assert "macos/Vantage" in src, "the native path must build from macos/Vantage"
    assert "assemble_app.sh release" in src, "the native bundle must be assembled, not copied stale"


def test_the_script_reports_which_bundle_it_shipped():
    src = SCRIPT.read_text()
    assert "CFBundleIdentifier" in src, (
        "the build must state the shipped bundle id, or a DMG can be mistaken "
        "for the other app")
    assert "shipping:" in src


def test_the_decision_is_recorded_in_the_release_doc():
    doc = (REPO / "docs" / "RELEASE_AND_BUILD.md").read_text()
    assert "native" in doc.lower() and "DMG" in doc
    # The decision, once made, must be stated as decided — not left "open".
    assert "default ships the Tk launcher" not in doc, (
        "the release doc still says the launcher is what ships")