#!/bin/bash
set -e

echo "=== VANTAGE RELEASE VERIFICATION ==="
echo ""

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
