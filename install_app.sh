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
# This used to promise a first-launch prompt for Microphone and Speech Recognition.
# It never comes: nothing in the tree calls
# AVCaptureDevice.requestAccess -- jarvis/voice/native/mic_state.swift deliberately
# queries consent instead of requesting it, because a helper binary that asked would
# either never answer or abort. Measured on a freshly built bundle, the self-test
# reports permission "denied" / capture "blocked" with no prompt ever shown, so the
# accurate instruction is the manual one, plus the command that shows the real state.
echo "The app does not ask for microphone or speech-recognition access on launch --"
echo "it reports whatever macOS already granted. If voice input shows blocked:"
echo "  1. System Settings -> Privacy & Security -> Microphone -> enable $APP_NAME"
echo "     (and the same for Speech Recognition)"
echo "  2. quit and relaunch the app"
echo "  3. check what it can actually see:"
echo "       JARVIS_SELFTEST=1 \"$DEST/$APP_NAME/Contents/MacOS/JarvisForge\""
echo "     which prints the live permission state rather than assuming it."
