#!/bin/bash
#!/bin/bash
# =============================================================================
# DEPRECATED — LEGACY WEBVIEW WRAPPER (com.vantage.app)
# This is NOT the native Vantage companion. The release pipeline builds
# android/ (com.vantage.companion) via android/build_apk.sh instead. This
# script remains only so the legacy wrapper can still be built manually; it
# must never be what a release ships (tests/test_release_pipeline.py enforces
# that the release path targets the native tree).
# =============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VERSION="${VERSION:-0.2.0}"

echo "=== Building Vantage Android APK v${VERSION} ==="

# Check for JDK
if [ -z "${JAVA_HOME:-}" ]; then
    if command -v java &>/dev/null; then
        export JAVA_HOME="$(dirname "$(dirname "$(readlink -f "$(command -v java)" 2>/dev/null || command -v java)")")"
    fi
fi

if [ -z "${JAVA_HOME:-}" ]; then
    echo "ERROR: JAVA_HOME is not set. Install JDK 17+ and set JAVA_HOME." >&2
    echo "  macOS: brew install openjdk@17" >&2
    echo "  Then: export JAVA_HOME=/opt/homebrew/opt/openjdk@17" >&2
    exit 1
fi

echo "  JAVA_HOME: ${JAVA_HOME}"

# Check for Android SDK
if [ -z "${ANDROID_HOME:-}" ] && [ -z "${ANDROID_SDK_ROOT:-}" ]; then
    # Try common locations
    for candidate in "$HOME/Library/Android/sdk" "$HOME/Android/Sdk" "/opt/android-sdk"; do
        if [ -d "$candidate" ]; then
            export ANDROID_HOME="$candidate"
            break
        fi
    done
fi

if [ -z "${ANDROID_HOME:-}" ]; then
    echo "ERROR: Android SDK not found. Install Android Studio or command-line tools." >&2
    echo "  https://developer.android.com/studio" >&2
    exit 1
fi

echo "  ANDROID_HOME: ${ANDROID_HOME}"

# Generate a debug keystore if none exists (for release builds, use a proper keystore)
KEYSTORE="app/vantage.keystore"
if [ ! -f "$KEYSTORE" ]; then
    echo "  · generating debug keystore"
    keytool -genkey -v \
        -keystore "$KEYSTORE" \
        -alias vantage \
        -keyalg RSA \
        -keysize 2048 \
        -validity 10000 \
        -storepass vantage \
        -keypass vantage \
        -dname "CN=Vantage, OU=Dev, O=Vantage, L=Unknown, ST=Unknown, C=US" \
        2>/dev/null || {
            echo "  Warning: keytool not available; building debug APK only"
            touch "$KEYSTORE"
        }
fi

# Make gradlew if not present
if [ ! -f "gradlew" ]; then
    echo "  · downloading Gradle wrapper"
    if command -v gradle &>/dev/null; then
        gradle wrapper --gradle-version 8.2
    else
        echo "ERROR: gradle not found. Install Gradle or download gradlew manually." >&2
        exit 1
    fi
fi

# Build
echo "  · building APK..."
./gradlew assembleRelease 2>&1 | tail -5

APK_PATH="app/build/outputs/apk/release/app-release-unsigned.apk"
if [ -f "$APK_PATH" ]; then
    cp "$APK_PATH" "vantage-${VERSION}.apk"
    echo ""
    echo "=== Done ==="
    echo "APK: $(pwd)/vantage-${VERSION}.apk"
    echo "Size: $(ls -lh vantage-${VERSION}.apk | awk '{print $5}')"
    echo ""
    echo "Transfer to your Android device and install."
    echo "You may need to enable 'Install from unknown sources' in Settings."
else
    echo "ERROR: APK build failed (check output above)" >&2
    exit 1
fi