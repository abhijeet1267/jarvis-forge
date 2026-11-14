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