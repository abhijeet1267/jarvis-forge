#!/bin/sh
# Build Jarvis Forge.app and install it into /Applications.
#
# Safe by design: an existing install is moved aside rather than deleted, so
# you can always roll back.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${JARVIS_PYTHON:-$HOME/.workbuddy-ai/binaries/python/envs/default/bin/python}"
APP_NAME="Jarvis Forge.app"
DEST="/Applications"

if [ ! -x "$PY" ]; then
    PY="$(command -v python3)"
fi

echo "JARVIS Forge installer"
echo "  project : $HERE"
echo "  python  : $PY"
echo

"$PY" "$HERE/build_app.py"

BUILT="$HERE/dist/$APP_NAME"
if [ ! -d "$BUILT" ]; then
    echo "build did not produce $BUILT" >&2
    exit 1
fi

if [ -d "$DEST/$APP_NAME" ]; then
    BACKUP="$DEST/$APP_NAME.previous"
    echo "  an existing install was found; moving it to $(basename "$BACKUP")"
    if [ -d "$BACKUP" ]; then
        echo "  removing the older backup at $BACKUP"
        rm -rf "$BACKUP"
    fi
    mv "$DEST/$APP_NAME" "$BACKUP"
fi

echo "  installing to $DEST/$APP_NAME"
cp -R "$BUILT" "$DEST/$APP_NAME"
xattr -cr "$DEST/$APP_NAME" 2>/dev/null || true

echo
echo "Installed. Launch it from Launchpad, Spotlight, or:"
echo "  open \"$DEST/$APP_NAME\""
echo
echo "First launch will ask for Microphone and Speech Recognition permission."
echo "Grant them in System Settings -> Privacy & Security if you want voice input."
