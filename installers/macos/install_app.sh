#!/bin/bash
# Install the built Vantage.app into /Applications — safely.
#
# Why this script exists: replacing the installed app by hand produced a real
# failure. A heredoc-driven command ran with the shell's working directory
# somewhere else, so `rm -rf /Applications/Vantage.app` executed while the
# `cp` beside it failed for lack of a source — the installed app was deleted
# and nothing replaced it. The lesson is not "be careful": it is that an
# install is a transaction with a check before every destructive step.
#
# Rules enforced here:
#   1. Paths are absolute, resolved once, and printed before anything happens.
#   2. The source must be the expected native bundle, signed, with an executable.
#   3. /Applications/Vantage.app is only replaced after the source is validated.
#   4. The existing app is copied to a timestamped backup first, and the backup
#      stays until the replacement is verified.
#   5. Every failure exits non-zero with the installed app untouched or restored.
#
# Usage: ./installers/macos/install_app.sh [path/to/Vantage.app]
set -euo pipefail

EXPECTED_ID="com.jarvisforge.vantage"
DEST="/Applications/Vantage.app"

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SRC_IN="${1:-$REPO/macos/Vantage/Vantage.app}"

# Resolve to an absolute path before any use. If this fails, nothing else runs.
SRC="$(cd "$(dirname "$SRC_IN")" 2>/dev/null && pwd)/$(basename "$SRC_IN")" || {
    echo "ERROR: cannot resolve source path: $SRC_IN" >&2; exit 1; }

echo "  source      : $SRC"
echo "  destination : $DEST"

# ── 1. Validate the source ────────────────────────────────────────────────
[ -d "$SRC" ] || { echo "ERROR: source bundle does not exist: $SRC" >&2; exit 1; }
SRC_ID=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$SRC/Contents/Info.plist" 2>/dev/null || echo "?")
[ "$SRC_ID" = "$EXPECTED_ID" ] || {
    echo "ERROR: source bundle id is '$SRC_ID', expected '$EXPECTED_ID'" >&2; exit 1; }
[ -x "$SRC/Contents/MacOS/Vantage" ] || {
    echo "ERROR: source has no executable at Contents/MacOS/Vantage" >&2; exit 1; }
codesign --verify --strict "$SRC" 2>/dev/null || {
    echo "ERROR: source signature does not verify: $SRC" >&2; exit 1; }
echo "  source ok   : id $SRC_ID, signed, executable present"

# ── 2. Back up the installed app (if any) ─────────────────────────────────
BACKUP=""
if [ -d "$DEST" ]; then
    BACKUP="/Applications/Vantage.app.backup-$(date +%Y%m%d-%H%M%S)"
    cp -R "$DEST" "$BACKUP"
    echo "  backup      : $BACKUP"
else
    echo "  backup      : none (nothing installed)"
fi

# ── 3. Replace atomically: stage beside the destination, then swap ────────
STAGE="/Applications/.Vantage.app.staging-$$"
trap 'rm -rf "$STAGE"' EXIT
rm -rf "$STAGE"
cp -R "$SRC" "$STAGE"

if [ -d "$DEST" ]; then
    rm -rf "$DEST"
fi
mv "$STAGE" "$DEST"

# ── 4. Verify the replacement, or restore the backup ──────────────────────
if ! codesign --verify --strict "$DEST" 2>/dev/null; then
    echo "ERROR: installed app does not verify; restoring backup" >&2
    [ -n "$BACKUP" ] && { rm -rf "$DEST"; mv "$BACKUP" "$DEST"; }
    exit 1
fi
DEST_ID=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$DEST/Contents/Info.plist" 2>/dev/null || echo "?")
if [ "$DEST_ID" != "$EXPECTED_ID" ]; then
    echo "ERROR: installed id is '$DEST_ID'; restoring backup" >&2
    [ -n "$BACKUP" ] && { rm -rf "$DEST"; mv "$BACKUP" "$DEST"; }
    exit 1
fi

echo "  installed   : $DEST (id $DEST_ID, signature verified)"
if [ -n "$BACKUP" ]; then
    echo "  recovery    : $BACKUP"
    echo "                (remove it once you are satisfied: rm -rf '$BACKUP')"
fi