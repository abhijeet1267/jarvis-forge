"""Deliver a notification through the system the person is actually using.

Until now a scheduled reminder's entire effect was a string written into
`record_run` — a run-history row nobody is looking at when the reminder fires. A
reminder that only lands in an audit table is not a reminder.

macOS is reached through `osascript`'s ``display notification``, which needs no
permission for a banner (unlike Accessibility). The honest divide is preserved:

* the call reports whether ``osascript`` accepted the request, and
* says plainly that **acceptance is not the same as a banner the person saw** —
  Focus and notification settings can swallow it, and nothing here can detect
  that. `delivered` is therefore never asserted; `accepted` is what we know.

There is also a watch bridge for paired hardware, but it is configured by an
environment variable and absent by default, so it is reported as unavailable
rather than pretended.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any, Dict

from .logging_setup import get_logger

log = get_logger("notify")

#: A notification body is a single line in the banner; long text is truncated
#: rather than silently dropped.
_MAX_BODY = 300


def _quote(value: str) -> str:
    """AppleScript string literal, escaped for the double-quoted form."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def describe() -> Dict[str, Any]:
    macos = shutil.which("osascript") is not None
    return {
        "macos_banner": macos,
        "watch_bridge": False,
        "detail": ("System banners are available on this machine." if macos
                   else "No osascript here, so no banner can be shown."),
    }


def notify(title: str, body: str = "", subtitle: str = "") -> Dict[str, Any]:
    """Ask the system to show a banner.

    Returns ``{"accepted": bool, ...}``. ``accepted`` means the notification
    system took the request — **not** that a banner appeared, which this cannot
    know. Callers must not report the stronger claim.
    """
    title = (title or "Vantage").strip()[:120]
    body = (body or "").strip().replace("\n", " ")[:_MAX_BODY]
    subtitle = (subtitle or "").strip()[:120]

    if shutil.which("osascript") is None:
        return {"accepted": False, "delivered": None,
                "detail": "No osascript on this machine, so no banner was requested."}

    parts = [f"display notification {_quote(body or title)}", f"with title {_quote(title)}"]
    if subtitle:
        parts.append(f"subtitle {_quote(subtitle)}")
    script = " ".join(parts)

    try:
        done = subprocess.run(["osascript", "-e", script],
                              capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"accepted": False, "delivered": None,
                "detail": f"the notification system did not answer: {exc}"}

    accepted = done.returncode == 0
    detail = ("The system accepted the notification. It may still be suppressed by "
              "Focus or notification settings, which I cannot see."
              if accepted else
              f"The notification system refused it: {(done.stderr or '').strip()[:160]}")
    return {"accepted": accepted, "delivered": None, "detail": detail}
