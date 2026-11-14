#!/bin/bash
# Build the NATIVE Vantage companion (com.vantage.companion, Jetpack Compose).
#
# This is the only script that may produce an Android release artifact. The
# legacy WebView wrapper lives in installers/android/ and is NOT this app —
# a regression test (tests/test_release_pipeline.py) fails CI if the release
# path ever points back at it.
#
# Output:
#   android/app/build/outputs/apk/debug/app-debug.apk      (debug)
#   dist/Vantage-<version>-android.apk                     (release, copied)
#   dist/ANDROID_RELEASE_MANIFEST.json                     (provenance)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VERSION="${VERSION:-1.0}"

# ---------------------------------------------------------------- JDK
# AGP 8.7 needs JDK 17–21 for the Gradle daemon. JDK 22+ removes APIs
# (SecurityManager, final fields) that AGP's instrumentation transform
# depends on, causing the build to hang or fail outright. Explicitly
# prefer 17, then 21, and reject anything higher.
if [ -z "${JAVA_HOME:-}" ] && command -v /usr/libexec/java_home &>/dev/null; then
    for v in 17 21; do
        candidate="$(/usr/libexec/java_home -v "$v" 2>/dev/null || true)"
        if [ -n "$candidate" ]; then
            export JAVA_HOME="$candidate"
            break
        fi
    done
fi
# Last resort: look for Homebrew JDK 17 directly.
if [ -z "${JAVA_HOME:-}" ] && [ -x /opt/homebrew/Cellar/openjdk@17/*/bin/java ]; then
    JAVA_HOME="$(echo /opt/homebrew/Cellar/openjdk@17/*/libexec/openjdk.jdk/Contents/Home)"
    export JAVA_HOME
fi
echo "JDK:   ${JAVA_HOME}"

# ---------------------------------------------------------------- SDK
if [ -z "${ANDROID_HOME:-}" ] && [ -z "${ANDROID_SDK_ROOT:-}" ]; then
    for c in "$HOME/Library/Android/sdk" /opt/android-sdk /usr/local/android-sdk; do
        [ -d "$c" ] && export ANDROID_HOME="$c" && break
    done
fi
if [ -z "${ANDROID_HOME:-}" ] && [ -z "${ANDROID_SDK_ROOT:-}" ]; then
    echo "ERROR: Android SDK not found. Set ANDROID_HOME." >&2
    exit 1
fi
echo "SDK:   ${ANDROID_HOME:-$ANDROID_SDK_ROOT}"

# ---------------------------------------------------------------- gradle
# Use the wrapper when present; otherwise the system Gradle.
if [ -x ./gradlew ]; then
    GRADLE="./gradlew"
else
    command -v gradle >/dev/null 2>&1 || { echo "ERROR: no ./gradlew and no system gradle" >&2; exit 1; }
    GRADLE="gradle"
fi

# ---------------------------------------------------------------- build
echo "=== Building native companion v${VERSION} ==="
"$GRADLE" :app:assembleDebug --console=plain

APK="app/build/outputs/apk/debug/app-debug.apk"
[ -f "$APK" ] || { echo "ERROR: expected APK missing: $APK" >&2; exit 1; }

# ---------------------------------------------------------------- identity
# The artifact must be the native companion. An aapt check here means a
# mis-pointed build (e.g. the WebView wrapper's com.vantage.app) fails the
# release instead of shipping.
AAPT2="$(ls "${ANDROID_HOME:-$ANDROID_SDK_ROOT}"/build-tools/*/aapt2 2>/dev/null | sort | tail -1)"
[ -n "$AAPT2" ] || { echo "ERROR: aapt2 not found in SDK build-tools" >&2; exit 1; }

BADGING="$("$AAPT2" dump badging "$APK")"
PKG_ID="$(printf '%s\n' "$BADGING" | sed -n "s/^package: name='\([^']*\)'.*/\1/p")"
LABEL="$(printf '%s\n' "$BADGING" | sed -n "s/^application-label:'\([^']*\)'.*/\1/p")"
ACTIVITY="$(printf '%s\n' "$BADGING" | sed -n "s/^launchable-activity: name='\([^']*\)'.*/\1/p")"

echo "package:   ${PKG_ID}"
echo "label:     ${LABEL}"
echo "launcher:  ${ACTIVITY}"

if [ "$PKG_ID" != "com.vantage.companion" ]; then
    echo "ERROR: APK package is '${PKG_ID}', expected com.vantage.companion." >&2
    echo "       The release pipeline built the wrong application." >&2
    exit 1
fi

# ---------------------------------------------------------------- provenance
COMMIT="$(git -C "$SCRIPT_DIR/.." rev-parse --short HEAD 2>/dev/null || echo local)"
TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
sha256() {
    if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}';
    else sha256sum "$1" | awk '{print $1}'; fi
}
APK_SHA="$(sha256 "$APK")"

# Promote to dist/ under a release name.
DIST="$(cd "$SCRIPT_DIR/.." && pwd)/dist"
mkdir -p "$DIST"
cp "$APK" "$DIST/Vantage-${VERSION}-android-debug.apk"

cat > "$DIST/ANDROID_RELEASE_MANIFEST.json" <<EOF
{
  "version": "${VERSION}",
  "package": "${PKG_ID}",
  "label": "${LABEL}",
  "launcher_activity": "${ACTIVITY}",
  "build_type": "debug",
  "git_commit": "${COMMIT}",
  "built_at": "${TIMESTAMP}",
  "apk_sha256": "${APK_SHA}",
  "note": "Debug-signed build. Release signing requires the owner's keystore; this build must never be pushed to a store."
}
EOF

echo ""
echo "APK:       $DIST/Vantage-${VERSION}-android-debug.apk"
echo "Size:      $(du -h "$DIST/Vantage-${VERSION}-android-debug.apk" | cut -f1)"
echo "Manifest:  $DIST/ANDROID_RELEASE_MANIFEST.json"
