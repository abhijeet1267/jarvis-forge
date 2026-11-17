#!/bin/bash
# Assemble the Vantage.app bundle from the compiled Swift binary.
# Usage: ./assemble_app.sh [debug|release]

set -euo pipefail

MODE="${1:-release}"
APP_NAME="Vantage"
APP_BUNDLE="${APP_NAME}.app"

if [ "$MODE" = "release" ]; then
    BIN_DIR=$(swift build -c release --show-bin-path)
else
    BIN_DIR=$(swift build --show-bin-path)
fi

BINARY="${BIN_DIR}/${APP_NAME}"

echo "==> Using binary: ${BINARY}"
if [ ! -f "$BINARY" ]; then
    echo "Error: Binary not found. Run 'swift build -c ${MODE}' first."
    exit 1
fi

rm -rf "${APP_BUNDLE}"
mkdir -p "${APP_BUNDLE}/Contents/MacOS"
mkdir -p "${APP_BUNDLE}/Contents/Resources"

# --- Copy binary ---
cp "${BINARY}" "${APP_BUNDLE}/Contents/MacOS/"

# --- Info.plist ---
cp Resources/Info.plist "${APP_BUNDLE}/Contents/"

# --- Stage the backend inside the bundle --------------------------------------
#
# BackendManager.findProjectDir() prefers `Resources/run.py` over any checkout,
# so staging here is what makes the app the user opens run its own backend. Without
# it the app looks for a checkout beside itself and starts `.venv/bin/python` from
# there -- which is the development layout, not a product.
#
# `native/` carries the desktop executor; `scripts/` carries backend_lifecycle.py,
# which the launcher invokes. Both were missing from the earlier bundle.
PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
RESOURCES="${APP_BUNDLE}/Contents/Resources"

if [ -f "${PROJECT_DIR}/run.py" ]; then
    echo "==> Staging backend from ${PROJECT_DIR}"
    for item in run.py jarvis dashboard scripts native pyproject.toml requirements.txt; do
        if [ -d "${PROJECT_DIR}/${item}" ]; then
            rsync -a --exclude '__pycache__' --exclude '*.pyc' \
                  "${PROJECT_DIR}/${item}/" "${RESOURCES}/${item}/"
        elif [ -f "${PROJECT_DIR}/${item}" ]; then
            cp "${PROJECT_DIR}/${item}" "${RESOURCES}/"
        fi
    done

    # Packages travel with the app. Playwright is 161 MB of the 247 MB venv and
    # drives browser automation, not the desktop backend.
    VENV_SITE="${PROJECT_DIR}/.venv/lib/python3.11/site-packages"
    if [ -d "${VENV_SITE}" ]; then
        echo "==> Staging Python packages"
        rsync -a --exclude '__pycache__' --exclude '*.pyc' \
              --exclude 'playwright' --exclude 'pip' --exclude 'setuptools' \
              --exclude 'pytest' --exclude '_pytest' --exclude 'pygments' --exclude 'pypdf' \
              "${VENV_SITE}/" "${RESOURCES}/site-packages/"
    fi

    # The interpreter is still this machine's. Bundling CPython is a separate job.
    printf '%s' "${PROJECT_DIR}/.venv/bin/python" > "${RESOURCES}/python_path.txt"
else
    echo "    (no backend to stage; the app will need a checkout beside it)"
fi

# --- Generate icon ---
ICONSET="${APP_NAME}.iconset"
rm -rf "${ICONSET}"
mkdir -p "${ICONSET}"

# Generate a 1024x1024 blue shield PNG using Python
python3 -c "
import struct, zlib
W = 1024
def make_png():
    raw = b''
    for y in range(W):
        raw += b'\x00'
        for x in range(W):
            cx, cy = W/2, W/2
            r = W * 0.38
            d = ((x - cx)**2 + (y - cy)**2)**0.5
            ed = max(abs(x - cx), abs(y - cy))
            if ed < r and d < r * 1.05:
                raw += bytes([59, 130, 246, 255])
            else:
                raw += bytes([0, 0, 0, 0])
    compress = zlib.compress(raw)
    def chunk(t, d):
        c = struct.pack('>I', len(d)) + t + d
        c += struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
        return c
    sig = b'\x89PNG\r\n\x1a\n'
    ihdr = chunk(b'IHDR', struct.pack('>IIBBBBB', W, W, 8, 6, 0, 0, 0))
    idata = chunk(b'IDAT', compress)
    iend = chunk(b'IEND', b'')
    with open('${ICONSET}/icon_1024x1024.png', 'wb') as f:
        f.write(sig + ihdr + idata + iend)
make_png()
"

# Resize for all required sizes
sips -z 512 512   "${ICONSET}/icon_1024x1024.png" --out "${ICONSET}/icon_512x512.png" 2>/dev/null
cp                     "${ICONSET}/icon_512x512.png"     "${ICONSET}/icon_256x256@2x.png" 2>/dev/null
sips -z 256 256   "${ICONSET}/icon_1024x1024.png" --out "${ICONSET}/icon_256x256.png" 2>/dev/null
cp                     "${ICONSET}/icon_256x256.png"     "${ICONSET}/icon_128x128@2x.png" 2>/dev/null
sips -z 128 128   "${ICONSET}/icon_256x256.png" --out "${ICONSET}/icon_128x128.png" 2>/dev/null
sips -z 64 64     "${ICONSET}/icon_256x256.png" --out "${ICONSET}/icon_32x32@2x.png" 2>/dev/null
sips -z 32 32     "${ICONSET}/icon_256x256.png" --out "${ICONSET}/icon_32x32.png" 2>/dev/null
cp                     "${ICONSET}/icon_32x32.png"       "${ICONSET}/icon_16x16@2x.png" 2>/dev/null
sips -z 16 16     "${ICONSET}/icon_256x256.png" --out "${ICONSET}/icon_16x16.png" 2>/dev/null

ICNS_PATH="${APP_BUNDLE}/Contents/Resources/${APP_NAME}.icns"
iconutil -c icns "${ICONSET}" -o "${ICNS_PATH}" 2>/dev/null

# --- Ad-hoc signing ---
# Fail loud on a signing or verification failure: a bundle that did not sign (or
# that signed but fails to verify) must never be reported as "Created". `set -e`
# below already stops the script on a non-zero codesign exit; the explicit check
# and the separate `--verify` step mirror build_app.py (R5.1) and the
# "fail loud on codesign failure" invariant, so a bad bundle can't be mistaken
# for a good one at the packaging step.
# --- Relocatable Python -------------------------------------------------------
#
# Before signing: it writes into Contents/Resources, and a signature computed
# first would not cover the interpreter. It also rewrites python_path.txt to the
# bundled runtime, which is what makes the app independent of this checkout.
if [ -x "${PROJECT_DIR}/scripts/bundle_python.sh" ]; then
    echo "==> Bundling relocatable Python"
    if ! "${PROJECT_DIR}/scripts/bundle_python.sh" --app "$(pwd)/${APP_BUNDLE}"; then
        # A bundle without its own interpreter still runs on this machine from
        # the checkout's venv. Say so rather than shipping it silently.
        echo "    ! the bundled Python could not be produced; this app depends on" >&2
        echo "      ${PROJECT_DIR}/.venv and will not run on another Mac (needs network)." >&2
    fi
else
    echo "    (scripts/bundle_python.sh missing; using this machine's interpreter)"
fi

echo "==> Signing ${APP_BUNDLE}"
if ! codesign --force --deep -s - "${APP_BUNDLE}"; then
    echo "Error: codesign failed for ${APP_BUNDLE}" >&2
    exit 1
fi
if ! codesign --verify --strict --verbose=2 "${APP_BUNDLE}"; then
    echo "Error: codesign --verify failed for ${APP_BUNDLE}" >&2
    exit 1
fi
echo "    codesign verify: ok"

echo "==> Created ${APP_BUNDLE}"
echo "    Binary: $(file "${APP_BUNDLE}/Contents/MacOS/${APP_NAME}" | cut -d: -f2-)"
echo "    Size:   $(du -sh "${APP_BUNDLE}" | cut -f1)"