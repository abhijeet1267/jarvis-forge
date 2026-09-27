#!/usr/bin/env python3
"""Assemble ``Jarvis Forge.app`` from the project source.

    python build_app.py                # build into ./dist
    python build_app.py --install      # build, then copy to /Applications

The bundle declares the usage descriptions macOS needs before it will grant
Microphone, Speech Recognition or Apple Events access, and is ad-hoc signed so
TCC has a stable identity to attach permissions to.
"""

from __future__ import annotations

import argparse
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
APP_NAME = "Jarvis Forge"
BUNDLE_ID = "com.jarvisforge.app"
VERSION = "0.2.0"

REQUIRED_PYTHON = Path.home() / ".workbuddy-ai/binaries/python/envs/default/bin/python"


def find_python() -> Path:
    """Pick the interpreter the app should use."""
    for candidate in (REQUIRED_PYTHON, Path(sys.executable)):
        if candidate.exists():
            return candidate
    return Path("/usr/bin/python3")


def build_info_plist() -> dict:
    """The bundle's Info.plist.

    The three NS*UsageDescription keys are what make macOS show a permission
    prompt instead of silently refusing (or aborting) the process.
    """
    return {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": "JarvisForge",
        "CFBundleIconFile": "JarvisForge",
        "CFBundlePackageType": "APPL",
        "CFBundleSignature": "????",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": "2",
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSMinimumSystemVersion": "11.0",
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "JARVIS Forge",
        # --- permission prompts -------------------------------------------------
        "NSMicrophoneUsageDescription": (
            "JARVIS Forge uses the microphone to listen for spoken commands."
        ),
        "NSSpeechRecognitionUsageDescription": (
            "JARVIS Forge transcribes your speech on-device to run commands."
        ),
        "NSAppleEventsUsageDescription": (
            "JARVIS Forge uses Apple Events to open and quit applications on "
            "your behalf."
        ),
    }


def build_launcher_executable() -> str:
    """The bundle's main executable: a thin shim that starts the Python launcher."""
    return """#!/bin/sh
# Jarvis Forge launcher shim.
# Locates a usable Python and hands off to the real launcher in Resources/.
set -e

RESOURCES="$(cd "$(dirname "$0")/../Resources" && pwd)"

PY=""
if [ -f "$RESOURCES/python_path.txt" ]; then
    PY="$(cat "$RESOURCES/python_path.txt")"
fi
if [ ! -x "$PY" ]; then
    PY="$(command -v python3 || true)"
fi
if [ -z "$PY" ]; then
    osascript -e 'display dialog "JARVIS Forge needs Python 3.10 or newer, but no python3 was found." with title "JARVIS Forge" buttons {"OK"} with icon caution'
    exit 1
fi

exec "$PY" "$RESOURCES/launcher.py"
"""


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def build(dest_root: Path, python: Path) -> Path:
    app = dest_root / f"{APP_NAME}.app"
    if app.exists():
        shutil.rmtree(app)

    contents = app / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    for directory in (macos, resources):
        directory.mkdir(parents=True, exist_ok=True)

    # --- icon ---------------------------------------------------------------
    # Render into a staging dir so only the .icns lands inside the bundle.
    print("  · rendering icon")
    staging = dest_root / "build"
    staging.mkdir(parents=True, exist_ok=True)
    staged_icns = staging / "JarvisForge.icns"
    proc = run([str(python), str(PROJECT_DIR / "app" / "make_icon.py"), str(staged_icns)])
    if proc.returncode != 0:
        raise RuntimeError(f"icon generation failed:\n{proc.stdout}\n{proc.stderr}")
    shutil.copy2(staged_icns, resources / "JarvisForge.icns")

    # --- payload ------------------------------------------------------------
    print("  · copying launcher and resources")
    shutil.copy2(PROJECT_DIR / "app" / "launcher.py", resources / "launcher.py")
    (resources / "project_path.txt").write_text(str(PROJECT_DIR), encoding="utf-8")
    (resources / "python_path.txt").write_text(str(python), encoding="utf-8")

    # --- Info.plist ---------------------------------------------------------
    print("  · writing Info.plist")
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(build_info_plist(), handle)

    (contents / "PkgInfo").write_text("APPL????", encoding="ascii")

    # --- executable ---------------------------------------------------------
    executable = macos / "JarvisForge"
    executable.write_text(build_launcher_executable(), encoding="utf-8")
    executable.chmod(0o755)

    # --- sign ---------------------------------------------------------------
    print("  · ad-hoc signing (gives TCC a stable identity)")
    signed = run(["codesign", "--force", "--sign", "-", "--timestamp=none", str(app)])
    if signed.returncode != 0:
        print(f"    warning: codesign failed -> {signed.stderr.strip()}")
    else:
        verify = run(["codesign", "--verify", "--verbose=1", str(app)])
        print(f"    codesign verify: {'ok' if verify.returncode == 0 else verify.stderr.strip()}")

    # Clear the quarantine bit so Gatekeeper does not block a local build.
    run(["xattr", "-cr", str(app)])

    return app


def install(app: Path) -> None:
    target = Path("/Applications") / app.name
    print(f"  · installing to {target}")
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(app, target, symlinks=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Jarvis Forge.app")
    parser.add_argument("--install", action="store_true", help="copy the app to /Applications")
    parser.add_argument("--dest", default=str(PROJECT_DIR / "dist"), help="output directory")
    args = parser.parse_args()

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    python = find_python()

    print(f"Building {APP_NAME}.app")
    print(f"  project : {PROJECT_DIR}")
    print(f"  python  : {python}")
    print(f"  dest    : {dest}")

    try:
        app = build(dest, python)
    except RuntimeError as exc:
        print(f"\nbuild failed: {exc}", file=sys.stderr)
        return 1

    if args.install:
        install(app)

    print(f"\nBuilt: {app}")
    print(f"Launch with:  open '{app}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
