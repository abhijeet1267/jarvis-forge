#!/bin/bash
# Master release builder for Vantage - builds all platform packages.
# Usage:  ./installers/build_release.sh [--platform macos|windows|android|all]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

VERSION="${VERSION:-0.2.0}"
PLATFORM="${1:-all}"

echo "========================================="
echo " Vantage v${VERSION} Release Builder"
echo "========================================="
echo ""

build_macos() {
    echo "--- macOS DMG ---"
    ./installers/macos/build_dmg.sh
    echo ""
}

build_windows() {
    echo "--- Windows Package ---"
    echo "  Creating Windows distribution package..."

    WIN_DIR="dist/windows-package"
    rm -rf "${WIN_DIR}"
    mkdir -p "${WIN_DIR}"

    # Copy application files
    for dir in jarvis dashboard; do
        if [ -d "$dir" ]; then
            cp -R "$dir" "${WIN_DIR}/"
        fi
    done
    for file in run.py requirements.txt pyproject.toml .env.example; do
        if [ -f "$file" ]; then
            cp "$file" "${WIN_DIR}/"
        fi
    done

    # Copy the PowerShell installer
    cp installers/windows/install.ps1 "${WIN_DIR}/"

    # Create README
    cat > "${WIN_DIR}/README.txt" <<'EOF'
VANTAGE FOR WINDOWS - Setup Instructions
========================================

Option 1: Automatic Install (recommended)
  1. Right-click install.ps1 and select "Run with PowerShell"
  2. Follow the prompts

Option 2: Manual Setup
  1. Install Python 3.10+ from https://www.python.org/downloads/
  2. Open Command Prompt and run:
     pip install -r requirements.txt
  3. Start Vantage:
     python run.py serve
  4. Open http://localhost:8765 in your browser

For Android access: open http://<your-pc-ip>:8765/phone
EOF

    # Create a simple self-extracting launcher batch file at root
    cat > "${WIN_DIR}/start-vantage.bat" <<'BATCH'
@echo off
cd /d "%~dp0"
echo Starting Vantage...
echo.
echo Dashboard: http://localhost:8765
echo Phone UI:  http://localhost:8765/phone
echo.
echo Press Ctrl+C to stop.
echo.
python run.py serve
pause
BATCH

    # Zip it
    ZIP_NAME="Vantage-${VERSION}-Windows.zip"
    rm -f "dist/${ZIP_NAME}"
    (cd dist && zip -r "${ZIP_NAME}" windows-package) >/dev/null
    rm -rf "${WIN_DIR}"

    echo "  Created: dist/${ZIP_NAME}"
    echo "  Size:    $(du -h dist/${ZIP_NAME} | cut -f1)"
    echo ""
}

build_android() {
    echo "--- Android APK (native com.vantage.companion) ---"
    # The release artifact comes from the NATIVE app in android/ (Jetpack
    # Compose). The legacy WebView wrapper in installers/android/ is retired:
    # it is a different app (com.vantage.app) and must never be what ships.
    # android/build_apk.sh verifies package identity with aapt2 and fails the
    # release if the wrong application was built.
    ./android/build_apk.sh 2>&1 || {
        echo ""
        echo "  WARNING: Android APK build failed or requires the Android SDK."
        echo "  The native project is android/ (com.vantage.companion)."
        echo "  Prereqs: JDK 17+ and an Android SDK (set ANDROID_HOME)."
        echo "  Retry: ./android/build_apk.sh"
        echo ""
    }
    echo ""
}

case "${PLATFORM}" in
    macos)   build_macos ;;
    windows) build_windows ;;
    android) build_android ;;
    all)
        build_macos
        build_windows
        build_android
        ;;
    *)
        echo "Usage: $0 [--platform macos|windows|android|all]"
        exit 1
        ;;
esac

echo "========================================="
echo " Release build complete!"
echo "========================================="
echo ""
echo "Packages in dist/:"
ls -lh dist/*.dmg dist/*.zip dist/*.apk 2>/dev/null || echo "  (some platforms skipped)"
echo ""
echo "macOS users:   Open the .dmg and drag Vantage to Applications"
echo "Windows users: Extract the .zip and run install.ps1"
echo "Android users: Transfer the .apk and install, or build from installers/android/"