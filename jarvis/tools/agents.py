"""Agent-app status: the three questions a person actually asks about another app.

Installed? which version? running right now? -- answered from the filesystem and the
process table, with no guessing between them. This exists because the request was
"tell me whether Qoder is working", and the honest answer to that needs three facts
rather than one: an app can be installed and closed, installed and wedged, or running
under a version the launcher cannot see.

The probe is read-only, so it is never gated. The thing it deliberately does *not* do
is act on the app it is reporting on: no launching, no keystrokes, no handing work to
another agent. That would be a different permission question, and it belongs behind its
own confirmation rather than smuggled in beside a status check.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Tuple

from ..core.models import RiskLevel, ToolSpec
from .base import ToolContext, fail, ok, run_command, unverified

TOOL = "check_agent_status"

#: Where an app bundle is looked for, in order. No `mdfind`, no Spotlight: a name
#: typed by a user should resolve the same way twice.
BUNDLE_ROOTS = ("/Applications", "~/Applications", "~/.qoder/bin")

_VERSION = re.compile(r"\d+(?:\.\d+)+")
#: A name that will not survive being used as a path component or a `ps` needle is
#: refused before either is touched -- not because the calls below are shell-free
#: (they are), but because a report built from a mangled name is a wrong report.
_BAD_NAME = re.compile(r"[\n\r\t/\\;*`$|&<>\"]")


def _candidate_paths(app: str) -> List[str]:
    bundle = f"{app}.app"
    return [os.path.expanduser(os.path.join(root, bundle)) for root in BUNDLE_ROOTS]


async def check_agent_status(ctx: ToolContext, app: str = "") -> Any:
    """Report whether an app is installed, its version, and whether it is running."""
    name = (app or "").strip()
    if not name:
        return fail(TOOL, "Which app should I check?", device=ctx.device_id)
    if _BAD_NAME.search(name):
        return fail(
            TOOL,
            f"I cannot look up {name!r}: an app name has to be one plain word or "
            "phrase, with no path or shell characters in it.",
            device=ctx.device_id,
        )

    installed = next((path for path in _candidate_paths(name) if os.path.isdir(path)), "")
    if not installed:
        # Absent is a complete answer, and it is not a failure of the check.
        return ok(
            TOOL,
            f"{name} is not installed on this laptop, so there is nothing for me to "
            "report about it.",
            device=ctx.device_id,
            found=False,
            version="",
            running=False,
            path="",
        )

    version = ""
    code, out, _err = await run_command(["mdls", "-name", "kMDItemVersion", installed])
    if code == 0:
        match = _VERSION.search(out or "")
        version = match.group(0) if match else ""

    code, out, _err = await run_command(["ps", "-axo", "pid=,comm="])
    needle = os.path.basename(installed).removesuffix(".app").lower()
    running = code == 0 and bool(out) and needle in out.lower()

    parts = [f"{name} is installed"]
    parts.append(f"(version {version})" if version else "(version not reported by the bundle)")
    parts.append("and is running." if running else "and is not running right now.")
    report = " ".join(parts)
    if not version:
        # An unreadable version is stated, not filled in: the two answers a reader
        # must be able to tell apart are "the bundle says nothing" and "0.0".
        return unverified(TOOL, report, device=ctx.device_id, found=True, version="",
                          running=running, path=installed)
    return ok(
        TOOL,
        report,
        device=ctx.device_id,
        found=True,
        version=version,
        running=running,
        path=installed,
    )


SPECS: List[Tuple[ToolSpec, Any]] = [
    (
        ToolSpec(
            name=TOOL,
            description="Report whether an app is installed, its version, and whether it is running.",
            agent="laptop",
            risk=RiskLevel.READ,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"app": "string"},
            required_parameters=["app"],
            tags=["laptop", "read", "agents"],
        ),
        check_agent_status,
    ),
]
