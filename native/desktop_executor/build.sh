#!/bin/bash
# Build the native desktop executor.
#
# macOS attaches an Accessibility / Input-Monitoring grant to a *code identity*,
# and for an ad-hoc signature that identity includes the binary's own hash.
# Measured on this machine: rebuilding in place changed the CDHash from
# 4c0c60b6… to be7687e7…, and a grant that had been toggled on twice in System
# Settings stopped applying — with no error anywhere, because macOS simply
# matched nothing.
#
# So this script does NOT overwrite a granted artifact by default. It builds to
# a staging path, and promoting to the granted path is a separate, explicit step
# that tells you it will invalidate the grant.
#
#   ./build.sh                 build to staging (safe; keeps any existing grant)
#   ./build.sh --promote       replace the granted binary (INVALIDATES the grant)
#   ./build.sh --status        show the current identities and whether they match
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIVE="$HERE/vantage-desktop"
STAGING="$HERE/vantage-desktop.staging"

identity_of() {
    # The CDHash is the part of an ad-hoc identity that changes on rebuild.
    /usr/bin/codesign -dvvv "$1" 2>&1 | awk -F= '/^CDHash=/{print $2; exit}'
}

revision() {
    git -C "$HERE/../.." rev-parse --short HEAD 2>/dev/null || echo unknown
}

case "${1:-}" in
  --status)
    echo "source revision: $(revision)"
    for path in "$LIVE" "$STAGING" \
                "$HERE/VantageDesktopDev.app/Contents/MacOS/vantage-desktop"; do
        if [[ -x "$path" ]]; then
            printf '  %-28s %s\n' "$(basename "$(dirname "$path")")/$(basename "$path")" \
                   "CDHash=$(identity_of "$path")"
        fi
    done
    echo
    echo "Grant state, read from the live binary itself:"
    "$LIVE" permissions 2>/dev/null \
        | /usr/bin/python3 -c 'import json,sys; d=json.load(sys.stdin); print("  accessibility=%s post_events=%s" % (d["accessibility"], d["post_events"]))' \
        || echo "  (the live binary did not answer)"
    exit 0
    ;;
  --promote)
    if [[ ! -x "$STAGING" ]]; then
        echo "nothing staged; run ./build.sh first" >&2
        exit 1
    fi
    OLD="$(identity_of "$LIVE" 2>/dev/null || echo none)"
    NEW="$(identity_of "$STAGING" 2>/dev/null || echo none)"
    echo "This replaces the binary macOS was granted."
    echo "  was: $OLD"
    echo "  now: $NEW"
    if [[ "$OLD" != "$NEW" ]]; then
        echo
        echo "  The identity changes, so any Accessibility grant stops applying."
        echo "  You will need to remove the old entry and add this one again."
    fi
    mv "$STAGING" "$LIVE"
    echo "promoted."
    exit 0
    ;;
  --help|-h)
    sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
esac

echo "building to staging: $STAGING"
swiftc -O -o "$STAGING" "$HERE/main.swift"
echo "built:  $STAGING   CDHash=$(identity_of "$STAGING")"
echo "revision: $(revision)"
echo
cat <<'NEXT'
This did NOT touch the granted binary. To swap it in:

    ./build.sh --promote        # warns first; the grant will need re-granting

To see what macOS currently thinks:

    ./build.sh --status
NEXT
