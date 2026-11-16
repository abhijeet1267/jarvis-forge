#!/bin/bash
# Build VantageDesktopDev.app around the desktop executor.
#
# Why a bundle at all: macOS lists a permission grant by name in System
# Settings, and a bare binary shows up as "vantage-desktop" with no icon and no
# bundle identity. Wrapping it gives the person a stable, nameable thing to find
# and un-approve, which is the difference between a grant someone can audit and
# one they cannot.
#
# This grants nothing. It moves the binary to a fixed path and writes an
# Info.plist; the switch in System Settings is still the person's to flip.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-$HERE/VantageDesktopDev.app}"
REVISION="$(cd "$HERE/../.." && pwd)/.git"
REV="$(git -C "$HERE/../.." rev-parse --short HEAD 2>/dev/null || echo unknown)"
DIRTY="$(git -C "$HERE/../.." diff --quiet 2>/dev/null && echo clean || echo dirty)"

if [[ ! -x "$HERE/vantage-desktop" ]]; then
    echo "the executor is not built; run build.sh first" >&2
    exit 1
fi

rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Resources"
cp "$HERE/vantage-desktop" "$OUT/Contents/MacOS/vantage-desktop"

cat > "$OUT/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>com.jarvisforge.vantage.desktopdev</string>
  <key>CFBundleName</key><string>Vantage Desktop Dev</string>
  <key>CFBundleDisplayName</key><string>Vantage Desktop Dev</string>
  <key>CFBundleExecutable</key><string>vantage-desktop</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>15.0</string>
  <key>CFBundleVersion</key><string>15</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>VantageSourceRevision</key><string>$REV</string>
  <key>VantageSourceDirty</key><string>$DIRTY</string>
</dict>
</plist>
PLIST

# Ad-hoc sign so the bundle has one coherent identity. There is no Developer ID
# on this machine (measured: `security find-identity -v -p codesigning` reports
# 0 valid identities), so this is the strongest signature available and it does
# NOT survive a rebuild — the identity op reports that caveat rather than
# implying a stable grant.
codesign --force --sign - --identifier com.jarvisforge.vantage.desktopdev "$OUT" 2>&1 | sed 's/^/  /'

echo "built:   $OUT"
echo "exec:    $OUT/Contents/MacOS/vantage-desktop"
echo "revision $REV ($DIRTY)"
echo
echo "Report what macOS sees:"
echo "  $OUT/Contents/MacOS/vantage-desktop identity"
echo
echo "To grant it (your decision, not this script's):"
echo "  open -R $OUT    # then drag it into System Settings → Privacy & Security → Accessibility"