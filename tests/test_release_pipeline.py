"""Release pipeline must build the NATIVE companion, not the WebView wrapper.

Historically the release shipped `installers/android/` (a WebView wrapper,
package `com.vantage.app`) while the intended app is `android/` (Jetpack
Compose, `com.vantage.companion`). These tests fail CI if the release path is
ever mis-pointed again — a guardrail, not a build.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "installers" / "build_release.sh"
NATIVE = ROOT / "android"
LEGACY = ROOT / "installers" / "android"


def test_release_builds_the_native_android_tree():
    src = RELEASE.read_text(encoding="utf-8")
    # The Android section must invoke the native builder...
    assert "./android/build_apk.sh" in src, "release must build android/ (native)"
    # ...and must NOT invoke the legacy WebView wrapper.
    assert "./installers/android/build_apk.sh" not in src, \
        "release must not build the legacy WebView wrapper"
    assert "uses a WebView" not in src, \
        "release must not advertise the WebView wrapper as the product"


def test_native_tree_is_the_compose_companion():
    build = (NATIVE / "app" / "build.gradle.kts").read_text(encoding="utf-8")
    # The canonical app identity.
    assert "com.vantage.companion" in build
    # It is a Compose app, not a WebView wrapper.
    assert "compose" in build.lower()
    # And the native tree has no WebView dependency.
    assert "webview" not in build.lower()


def test_legacy_tree_is_marked_retired():
    # The wrapper, if it still exists, must be visibly marked so nobody
    # mistakes it for the product.
    if LEGACY.exists():
        marker = False
        for f in LEGACY.rglob("*.sh"):
            if "DEPRECATED" in f.read_text(encoding="utf-8"):
                marker = True
                break
        # Its build.gradle should not advertise compose (it is the wrapper).
        gradle = (LEGACY / "app" / "build.gradle").read_text(encoding="utf-8") \
            if (LEGACY / "app" / "build.gradle").exists() else ""
        assert "com.vantage.app" in gradle
        assert marker, "the legacy wrapper must be marked DEPRECATED"
