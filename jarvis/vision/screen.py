"""Screen capture and reading (v1.3).

Captures with the platform's own tool, then applies whatever vision backend is
available. With no vision model configured, "reading the screen" means OCR plus
pixel measurements — which is genuinely useful, and is described as such rather
than dressed up as comprehension.
"""

from __future__ import annotations

import platform
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

from ..logging_setup import get_logger

log = get_logger("vision.screen")

CAPTURE_TIMEOUT = 20.0


def capture(path: str = "") -> Dict[str, Any]:
    """Capture the screen to a PNG."""
    if platform.system() != "Darwin":
        return {"ok": False, "error": "Screen capture is only implemented on macOS."}

    target = Path(path).expanduser() if path else Path.home() / f"jarvis_screen_{int(time.time())}.png"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"I could not create {target.parent}: {exc}"}

    try:
        completed = subprocess.run(
            ["screencapture", "-x", str(target)],
            capture_output=True,
            text=True,
            timeout=CAPTURE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"ok": False, "error": f"screen capture failed: {exc}"}

    if completed.returncode != 0 or not target.exists():
        # Permission is the usual cause; say so rather than inventing a reason.
        return {
            "ok": False,
            "error": (
                "screen capture failed. This usually means Screen Recording "
                "permission is missing for the app running JARVIS."
            ),
        }

    return {"ok": True, "path": str(target), "bytes": target.stat().st_size}


def read_screen(path: str = "") -> Dict[str, Any]:
    """Capture the screen and read it as far as the backend allows."""
    shot = capture(path)
    if not shot.get("ok"):
        return shot

    from .image import inspect
    from .ocr import read_text

    measured = inspect(shot["path"])
    recognised = read_text(shot["path"])

    result: Dict[str, Any] = {
        "ok": True,
        "path": shot["path"],
        "measured": measured if measured.get("ok") else {"error": measured.get("error")},
        "text": recognised.get("text", "") if recognised.get("ok") else "",
        "ocr_error": recognised.get("error") if not recognised.get("ok") else None,
    }

    if result["text"]:
        result["summary"] = (
            f"Captured {shot['path']}. I read {recognised.get('line_count', 0)} line(s) of text "
            "from it."
        )
    else:
        reason = result["ocr_error"] or "no text was recognised"
        result["summary"] = f"Captured {shot['path']}. I could not read text from it: {reason}."

    result["understands_content"] = False
    result["caveat"] = (
        "I can measure and read text, but I cannot describe what is depicted "
        "without a vision model configured."
    )
    return result
