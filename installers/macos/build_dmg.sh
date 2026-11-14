#!/bin/bash
# Build a DMG installer for Vantage on macOS.
#
# Usage:  ./installers/macos/build_dmg.sh [--native]
# Output: dist/Vantage-<version>.dmg
#
# **Which app goes in the DMG is an open owner decision** (see
# docs/RELEASE_AND_BUILD.md §4): the Tk launcher (com.jarvisforge.app, the TCC
# identity carrier) or the native SwiftUI GUI (com.jarvisforge.vantage). This
# script does not decide it. The default stays the launcher — today's behaviour,
# unchanged. `--native` packages the native app built from current source, so
# the owner's choice is a flag, not a rewrite.
set -euo pipefail
cd "$(dirname "$0")/../.."

VERSION="${VERSION:-0.2.0}"
APP_NAME="Vantage"
# Which bundle to package. Default: the NATIVE SwiftUI app (com.jarvisforge.vantage).
#
# This default used to be the Tk launcher, and that is why a person who built the
# app the documented way saw an older interface than the one in `Sources/`: the
# launcher bundle contains no SwiftUI screens at all, so the Settings voice
# control, Command Center, Connections and Planner never reached the running app.
# The native app is the product; the launcher is kept only for the TCC/permission
# path and as an explicit fallback (`--launcher`).
SHIP_NATIVE=1
for arg in "$@"; do
    case "$arg" in
        --native) SHIP_NATIVE=1 ;;
        --launcher) SHIP_NATIVE=0 ;;
        -h|--help)
            echo "usage: $0 [--native|--launcher]   (default: --native)"; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done
DMG_NAME="${APP_NAME}-${VERSION}.dmg"
STAGING="dist/dmg-staging"
FINAL_DMG="dist/${DMG_NAME}"

if [ "$SHIP_NATIVE" = "1" ]; then
    echo "=== Step 1: Build the NATIVE Vantage.app (SwiftUI) ==="
    ( cd macos/Vantage && swift build -c release && ./assemble_app.sh release )
    SRC_APP="macos/Vantage/Vantage.app"
else
    echo "=== Step 1: Build Vantage.app (Tk launcher) ==="
    python3 build_app.py
    SRC_APP="dist/${APP_NAME}.app"
fi

# The two bundles have different ids by design; print what is actually shipped so
# the DMG can never be mistaken for the other app.
if [ ! -d "$SRC_APP" ]; then
    echo "ERROR: expected bundle not found: $SRC_APP" >&2
    exit 1
fi
SHIPPED_ID=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$SRC_APP/Contents/Info.plist" 2>/dev/null || echo "?")
echo "  · shipping: $SRC_APP  (bundle id: $SHIPPED_ID)"

echo ""
echo "=== Step 2: Prepare DMG staging ==="
rm -rf "${STAGING}"
mkdir -p "${STAGING}"

# Copy the built .app (whichever the mode selected)
cp -R "$SRC_APP" "${STAGING}/"

# Create a symlink to /Applications for drag-to-install
ln -s /Applications "${STAGING}/Applications"

# Create a small volume icon if we have the source icon
ICON_SRC="app/icon-1024.png"
if [ -f "${ICON_SRC}" ]; then
    echo "  · generating volume icon"
    sips -z 128 128 "${ICON_SRC}" --out "${STAGING}/.VolumeIcon.icns" 2>/dev/null || true
    if [ -f "${STAGING}/.VolumeIcon.icns" ]; then
        SetFile -a C "${STAGING}" 2>/dev/null || xattr -wx com.apple.FinderInfo \
            "0000000000000000040000000000000000000000000000000000000000000000" "${STAGING}" 2>/dev/null || true
    fi
fi

echo ""
echo "=== Step 3: Create DMG ==="
rm -f "${FINAL_DMG}"

hdiutil create -volname "${APP_NAME}" \
    -srcfolder "${STAGING}" \
    -ov -format UDZO \
    -imagekey zlib-level=9 \
    "${FINAL_DMG}"

echo ""
echo "=== Step 4: Code-sign the DMG ==="
# Fail loud: an unsigned (or unverified) DMG is a broken installer, and
# `set -e` above means a bare failure already exits — the old `|| echo
# "(skipped)"` was papering over exactly that, shipping an unsigned DMG.
if ! codesign --force --sign - --timestamp=none "${FINAL_DMG}"; then
    echo "ERROR: DMG code-signing failed." >&2
    exit 1
fi
if ! codesign --verify --verbose=1 "${FINAL_DMG}" 2>/dev/null; then
    echo "ERROR: DMG code-sign verification failed." >&2
    exit 1
fi
echo "  . DMG signed and verified"

# Clean up staging
rm -rf "${STAGING}"

echo ""
echo "=== Done ==="
echo "DMG:  ${FINAL_DMG}"
echo "Size: $(du -h "${FINAL_DMG}" | cut -f1)"
echo ""
echo "To install: open '${FINAL_DMG}' and drag Vantage to Applications"