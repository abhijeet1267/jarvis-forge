"""Phase 89 — Packaged App Acceptance Tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

# The release target is the native SwiftUI app (bundle id com.jarvisforge.vantage),
# built by `make app` in macos/Vantage. The obsolete Tk launcher (build_app.py ->
# dist/Vantage.app, com.jarvisforge.app) was retired, so these checks now verify the
# current reproducible build rather than the removed one.
REPO = Path(__file__).resolve().parents[1]
APP_PATH = REPO / "macos" / "Vantage" / "Vantage.app"


class TestCleanBuildVerification:
    """Tests verifying clean build before acceptance."""

    def test_bundle_exists(self):
        """Packaged app bundle exists."""
        assert APP_PATH.exists(), (
            f"Vantage.app not found at {APP_PATH} -- run `make app` in macos/Vantage first")

    def test_signature_valid(self):
        """App has valid code signature."""
        assert APP_PATH.exists(), f"Vantage.app not found at {APP_PATH}"
        result = subprocess.run(
            ["codesign", "--verify", "--strict", str(APP_PATH)],
            capture_output=True,
            text=True
        )

        assert result.returncode == 0, f"Signature invalid: {result.stderr}"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
