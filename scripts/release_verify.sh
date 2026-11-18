#!/bin/bash
set -e

echo "=== VANTAGE RELEASE VERIFICATION ==="
echo ""

# This script builds and verifies the PYTHON LAUNCHER (com.jarvisforge.app),
# which is NOT the product that ships. The shipping artifact is the native
# SwiftUI app (com.jarvisforge.vantage), built by
# `macos/Vantage/assemble_app.sh` and packaged by
# `installers/macos/build_dmg.sh` (SHIP_NATIVE=1).
#
# Two live scripts producing two different applications is how the wrong one
# gets shipped, so say which is which rather than leaving it implicit. If you
# meant to verify the shipping product, use installers/build_release.sh.
if [ "${VANTAGE_VERIFY_LAUNCHER:-0}" != "1" ]; then
    cat <<'NOTICE'
This verifies the Python launcher (com.jarvisforge.app), not the shipping app.

  shipping product : macos/Vantage/Vantage.app  (com.jarvisforge.vantage)
  build it with    : installers/build_release.sh   -> installers/macos/build_dmg.sh
  verify the DMG   : tests/test_r106_dmg_ships_either_artifact.py

Set VANTAGE_VERIFY_LAUNCHER=1 to verify the launcher deliberately.
NOTICE
    exit 2
fi

# Check source directory
if [ ! -f "build_app.py" ]; then
    echo "❌ Missing build_app.py"
    exit 1
fi
echo "✓ Source structure valid"

# Clean previous builds so a stale artifact can never pass as the new one.
rm -rf dist/Vantage.app dist/Vantage-macOS.zip dist/RELEASE_MANIFEST.json

# Build app — output is NOT suppressed: a silent build is how a broken
# artifact gets shipped, and build_app.py fails loudly on signing errors.
echo ""
echo "Building Vantage.app..."
python build_app.py --dest dist

# Verify signature
echo ""
echo "Verifying code signature..."
if codesign --verify -vvv dist/Vantage.app; then
    echo "✓ Code signing valid"
else
    echo "❌ Code signing failed"
    exit 1
fi

# Create distribution zip
echo ""
echo "Creating distribution archive..."
zip -rq dist/Vantage-macOS.zip dist/Vantage.app

# Provenance: whoever receives this artifact must be able to tell exactly
# which commit, on which day, produced it — and that the bytes haven't
# changed in transit.
COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "local")
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
sha256() {
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        sha256sum "$1" | awk '{print $1}'
    fi
}
APP_SHA=$(sha256 dist/Vantage.app/Contents/MacOS/JarvisForge)
ZIP_SHA=$(sha256 dist/Vantage-macOS.zip)
cat > dist/RELEASE_MANIFEST.json << MANIFEST
{
  "version": "0.2.0",
  "build": "2",
  "bundle_id": "com.jarvisforge.app",
  "git_commit": "$COMMIT",
  "built_at": "$TIMESTAMP",
  "executable_sha256": "$APP_SHA",
  "zip_sha256": "$ZIP_SHA",
  "signing_status": "ad-hoc signed",
  "notarization_status": "local_only_testing"
}
MANIFEST

# The manifest must describe the artifact we just made, not an ideal one.
if [ "$(grep -o "\"zip_sha256\": \"[a-f0-9]*\"" dist/RELEASE_MANIFEST.json | cut -d'"' -f4)" != "$ZIP_SHA" ]; then
    echo "❌ Manifest checksum does not match the built archive"
    exit 1
fi
echo "✓ Release manifest generated (commit $COMMIT, built $TIMESTAMP)"
echo ""
echo "=== BUILD SUCCESSFUL ==="
echo "Created: dist/Vantage.app, dist/Vantage-macOS.zip, dist/RELEASE_MANIFEST.json"
