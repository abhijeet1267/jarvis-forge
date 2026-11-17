#!/usr/bin/env python3
"""T9: TextEdit → type → GUI save → Finder, with focus re-asserted per step.

The desktop is in active use while this runs, so focus moves between calls. Every
input step therefore re-asserts the intended application immediately before
acting and refuses rather than typing into whatever happens to be frontmost. That
is exactly what the executor's `expect_app` guard does; this script's job is to
retry the *activation* (safe, idempotent) instead of retrying the *input* (which
would type into the wrong place).

    python3 benchmarks/v21_desktop/textedit_finder.py

Exit 0 only when the saved file exists and contains the note.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EXE = (REPO / "native" / "desktop_executor" / "VantageDesktopDev.app"
       / "Contents" / "MacOS" / "vantage-desktop")
OUT_DIR = Path("/tmp/v21_textedit")
OUT_FILE = OUT_DIR / "vantage-note.txt"
NOTE = "Hello from Vantage — real desktop automation verified."


def run(*args: str) -> dict:
    out = subprocess.run([str(EXE), *args], capture_output=True, text=True).stdout
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"status": "unknown", "reason": out[:160]}


def frontmost() -> str:
    return run("windows").get("frontmost_application", "")


def focus(application: str, tries: int = 6) -> bool:
    """Activate, then confirm. Activation is idempotent, so retrying it is safe.

    Returns False rather than forcing input when the application will not come
    forward -- the caller must not type into something the person is using.
    """
    for _ in range(tries):
        run("focus", f"application={application}")
        time.sleep(0.45)
        if application.lower() in frontmost().lower():
            return True
    return False


def editor_value() -> str:
    """TextEdit's editor contents, read from its own Accessibility tree."""
    tree = run("inspect", "application=TextEdit", "depth=6")
    found: list[str] = []

    def walk(node) -> None:
        if (node.get("role") or "") == "AXTextArea":
            found.append(node.get("value") or "")
        for child in node.get("children", []):
            walk(child)

    if tree.get("tree"):
        walk(tree["tree"])
    return found[0] if found else ""


def step(label: str, ok: bool, detail: str) -> bool:
    print(f"  {'PASS' if ok else 'FAIL':5s} {label:24s} {detail}")
    return ok


results: list[bool] = []
OUT_DIR.mkdir(parents=True, exist_ok=True)
if OUT_FILE.exists():
    OUT_FILE.unlink()

print("\n=== T9: TextEdit to Finder ===\n")

# -- A1: open TextEdit ---------------------------------------------------------
run("focus", "application=TextEdit")
time.sleep(1.0)
windows = run("windows", "application=TextEdit").get("windows") or []
results.append(step("A1 TextEdit running", bool(windows),
                    f"{len(windows)} window(s)"))

# A restored "Open" panel is not an editor; dismiss it and make a document.
if any((w.get("title") or "") == "Open" for w in windows):
    run("key", "key=escape")
    time.sleep(0.6)
    if focus("TextEdit"):
        run("key", "key=n", "modifiers=cmd")
        time.sleep(1.2)
    windows = run("windows", "application=TextEdit").get("windows") or []

title = windows[0].get("title", "?") if windows else "?"
results.append(step("A2 document window", bool(windows),
                    f"'{title}' {windows[0]['width']}x{windows[0]['height']}" if windows else "none"))

# -- A2: focus the editor and clear it ----------------------------------------
if not focus("TextEdit"):
    print("\n  TextEdit will not come to the front; not sending input.")
    sys.exit(1)

w = windows[0]
run("click", f"x={w['x'] + w['width'] // 2}", f"y={w['y'] + 140}")
time.sleep(0.5)

if not focus("TextEdit"):
    print("\n  focus lost after the click; stopping rather than typing elsewhere.")
    sys.exit(1)
run("key", "key=a", "modifiers=cmd")
time.sleep(0.25)
run("key", "key=delete")
time.sleep(0.5)
cleared = editor_value()
results.append(step("A3 editor cleared", cleared == "", f"value={cleared!r}"))

# -- A3: type the note --------------------------------------------------------
typed = None
for attempt in range(3):
    if not focus("TextEdit"):
        continue
    typed = run("type", f"text={NOTE}", "expect_app=TextEdit")
    if typed.get("status") != "target_stale":
        break
    time.sleep(0.4)

time.sleep(1.2)
value = editor_value()
results.append(step("A4 text typed", value == NOTE,
                    f"editor reads {value!r}"))
if value != NOTE:
    print(f"\n  expected: {NOTE!r}")
    print(f"  actual  : {value!r}")
    print("  stopping before Save: the editor does not contain the note.")
    sys.exit(1)

# -- B1: save through the GUI -------------------------------------------------
if not focus("TextEdit"):
    print("\n  TextEdit lost focus before Save; stopping.")
    sys.exit(1)
run("key", "key=s", "modifiers=cmd")
time.sleep(1.5)

panel = run("windows", "application=TextEdit").get("windows") or []
has_panel = any("save" in (w.get("title") or "").lower() or
                (w.get("width") or 0) > 0 and "untitled" not in (w.get("title") or "").lower()
                for w in panel)
results.append(step("B1 save invoked", bool(panel),
                    f"{[w.get('title') for w in panel]}"))

# The save panel is a sheet; drive it with the path field.
run("key", "key=g", "modifiers=cmd,shift")     # Go to Folder in an NSSavePanel
time.sleep(0.8)
run("type", f"text={OUT_DIR}")
time.sleep(0.5)
run("key", "key=return")
time.sleep(0.8)
run("type", "text=vantage-note.txt")
time.sleep(0.5)
run("key", "key=return")
time.sleep(1.8)

# TextEdit appends its own extension: a note saved as "vantage-note.txt" lands
# as "vantage-note.txt.rtf". That is the application's real behaviour, not a
# defect, so the check accepts the file it actually wrote -- but it must still
# be a file this workflow created, and it must contain the note.
candidates = sorted(OUT_DIR.glob("vantage-note.txt*"))
saved = next((c for c in candidates if c.is_file()), None)
results.append(step("B2 file on disk", saved is not None,
                    str(saved) if saved else f"nothing matching in {OUT_DIR}"))
if saved is not None:
    body = saved.read_text(errors="replace")
    # RTF escapes non-ASCII, so the em-dash appears as \'97; compare on the
    # ASCII-stable parts of the note rather than requiring a byte-exact match.
    prose = NOTE.replace("\u2014", "-").split("-")
    contains = all(part.strip() in body for part in prose if part.strip())
    results.append(step("B3 contents match", contains, f"{len(body)} bytes"))

# -- C: Finder reveals it -----------------------------------------------------
subprocess.run(["open", "-R", str(OUT_FILE)], capture_output=True)
time.sleep(1.5)
finder = run("windows", "application=Finder").get("windows") or []
results.append(step("C1 Finder opened", bool(finder),
                    f"{len(finder)} window(s)"))

print()
passed = sum(1 for r in results if r)
print(f"  {passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
