#!/usr/bin/env python3
"""Assemble ``Vantage.app`` from the project source.

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
APP_NAME = "Vantage"
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
        "NSHumanReadableCopyright": "Vantage",
        # --- permission prompts -------------------------------------------------
        "NSMicrophoneUsageDescription": (
            "Vantage uses the microphone to listen for spoken commands."
        ),
        "NSSpeechRecognitionUsageDescription": (
            "Vantage transcribes your speech on-device to run commands."
        ),
        "NSAppleEventsUsageDescription": (
            "Vantage uses Apple Events to open and quit applications on "
            "your behalf."
        ),
    }


#: The compiled launcher source, kept beside this build script so the bundle is
#: reproducible from the checkout.
LAUNCHER_SOURCE = Path(__file__).resolve().parent / "app" / "launcher_main.swift"


def build_launcher_executable(destination: Path) -> None:
    """Compile the bundle's main executable.

    It must be a Mach-O binary, not a shell script. Measured: with /bin/sh as
    CFBundleExecutable, macOS attributed the native executor's Accessibility
    request to the Homebrew python the script exec'd -- "python3-55554944..." --
    so a grant to Vantage.app never applied and the standalone app could not post
    input while the same binary launched from a terminal could.

    A shell script has no code identity to grant. A compiled executable inside the
    signed bundle does, and everything it spawns inherits that attribution.
    """
    if not LAUNCHER_SOURCE.exists():
        raise RuntimeError(f"the launcher source is missing: {LAUNCHER_SOURCE}")
    compiled = run(["swiftc", "-O", "-o", str(destination), str(LAUNCHER_SOURCE)])
    if compiled.returncode != 0:
        raise RuntimeError(
            "swiftc failed building the launcher:\n"
            f"{compiled.stdout.strip()}\n{compiled.stderr.strip()}")
    destination.chmod(0o755)


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


#: What the app needs to run, and nothing else. The checkout holds a 300 MB interpreter,
#: the test suite, previous builds and every cache in the tree; copying it wholesale
#: would ship those inside an application bundle and make the bundle slow to build,
#: impossible to sign quickly, and larger than the thing it launches.
#: What the application runs, copied into Resources/app.
#
# `native/` joined this list in V22: the desktop executor lived only in the
# checkout, so an installed app resolved it to a source-tree path that a copied
# bundle does not have. The executor is a build artifact and is excluded by
# STAGED_SKIP, so the built binary is copied separately by stage_native().
#
# `scripts/` joined it for the same class of reason: the native app invokes
# `scripts/backend_lifecycle.py`, and without it the app could not start its own
# backend.
STAGED_ITEMS = ("jarvis", "dashboard", "scripts", "run.py", "requirements.txt",
                "pyproject.toml", ".env.example")

#: Paths inside Resources/app that the staged copy must never carry.
STAGED_NATIVE = "native"
STAGED_SKIP = {".venv", "venv", "tests", "dist", "build", ".git", "__pycache__",
               ".pytest_cache", ".ruff_cache", "node_modules"}


def stage_project(resources: Path) -> Path:
    """Copy the source the app actually runs into the bundle.

    Until now `project_path.txt` held an absolute path to this checkout, so the built
    `.app` was a shortcut rather than an application: move it, or delete the checkout it
    points at, and the icon exits 1 -- the Phase 10 finding, still live in Phase 35.
    """
    target = resources / "app"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {n for n in names if n in STAGED_SKIP or n.endswith((".pyc", ".pyo"))}

    for item in STAGED_ITEMS:
        source = PROJECT_DIR / item
        if not source.exists():
            continue
        if source.is_dir():
            shutil.copytree(source, target / item, ignore=ignore)
        else:
            shutil.copy2(source, target / item)
    return target


def stage_native(resources: Path) -> Path | None:
    """Copy the built desktop executor into the bundle.

    The source is a build artifact, not source: `native/desktop_executor/main.swift`
    is staged as source so the app can rebuild it, but the compiled binary has to be
    copied explicitly because STAGED_SKIP excludes build outputs.

    The destination mirrors the checkout layout, because that is what
    `jarvis/desktop/executor.py` searches for -- `native/desktop_executor/vantage-desktop`
    relative to the project root. Keeping the two in step means the same resolution
    code works from the checkout and from an installed bundle, instead of the bundle
    silently falling back to `shutil.which`.

    A missing binary is reported and skipped rather than failing the build: the app
    still runs, and its own permission panel says the executor is not built, which is
    the honest answer rather than a broken app.
    """
    source = PROJECT_DIR / "native" / "desktop_executor" / "vantage-desktop"
    target_dir = resources / "app" / "native" / "desktop_executor"
    if not source.is_file():
        print("  ! the desktop executor is not built; "
              "run native/desktop_executor/build.sh to include it")
        return None
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "vantage-desktop"
    shutil.copy2(source, target)
    target.chmod(0o755)
    return target


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
    print("  · staging the project inside the bundle")
    stage_project(resources)
    print("  · staging the native desktop executor")
    staged_executor = stage_native(resources)

    # --- Info.plist ---------------------------------------------------------
    print("  · writing Info.plist")
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(build_info_plist(), handle)

    (contents / "PkgInfo").write_text("APPL????", encoding="ascii")

    # --- executable ---------------------------------------------------------
    executable = macos / "JarvisForge"
    print("  · compiling the launcher (Mach-O, so it can hold a TCC identity)")
    build_launcher_executable(executable)

    # --- sign ---------------------------------------------------------------
    # Ad-hoc signing is not optional here: without a stable code identity TCC
    # cannot persist microphone/accessibility grants, so an unsigned bundle
    # would "work" until the first permission prompt — then silently lose it.
    # A signing failure therefore fails the build instead of shipping a bundle
    # whose permissions were never real.
    print("  · ad-hoc signing (gives TCC a stable identity)")
    signed = run(["codesign", "--force", "--sign", "-", "--timestamp=none", str(app)])
    if signed.returncode != 0:
        raise RuntimeError(f"codesign failed -> {signed.stderr.strip() or signed.stdout.strip()}")
    verify = run(["codesign", "--verify", "--verbose=1", str(app)])
    if verify.returncode != 0:
        raise RuntimeError(f"codesign verify failed -> {verify.stderr.strip()}")
    print("    codesign verify: ok")

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
    parser = argparse.ArgumentParser(description="Build Vantage.app")
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
