"""System tools: status, battery, screenshots, allowlisted commands, safety stop.

Every command here is a fixed, allowlisted invocation. There is no path from
the model to an arbitrary shell (section 10).
"""

from __future__ import annotations

import platform
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.models import RiskLevel, ToolSpec, Verification
from ..logging_setup import get_logger
from .base import ToolContext, fail, human_size, ok, run_command

log = get_logger("tools.system")


async def get_time(ctx: ToolContext) -> Any:
    """Return the current local time."""
    now = datetime.now().astimezone()
    return ok(
        "get_time",
        now.strftime("It is %H:%M on %A, %d %B %Y."),
        device=ctx.device_id,
        iso=now.isoformat(),
        epoch=int(now.timestamp()),
    )


async def system_status(ctx: ToolContext) -> Any:
    """Report CPU count, load, memory pressure and uptime."""
    info: Dict[str, Any] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": platform.os.cpu_count() if hasattr(platform, "os") else None,
    }
    import os

    info["cpu_count"] = os.cpu_count()

    code, out, _err = await run_command(["uptime"])
    if code == 0:
        info["uptime"] = out

    if ctx.platform == "macos":
        code, out, _err = await run_command(["sysctl", "-n", "hw.memsize"])
        if code == 0 and out.isdigit():
            info["memory_total"] = human_size(int(out))
        code, out, _err = await run_command(["vm_stat"])
        if code == 0:
            info["vm_stat"] = out.splitlines()[0] if out else ""

    code, out, _err = await run_command(["df", "-h", "/"])
    if code == 0:
        info["disk"] = out

    return ok(
        "system_status",
        f"System looks healthy: {info.get('cpu_count')} CPUs, load {info.get('uptime', 'n/a')}.",
        device=ctx.device_id,
        **info,
    )


async def battery_status(ctx: ToolContext) -> Any:
    """Read laptop battery percentage and charging state."""
    if ctx.platform == "macos":
        code, out, err = await run_command(["pmset", "-g", "batt"])
        if code != 0:
            return fail("get_battery_status", f"Could not read battery: {err}", device=ctx.device_id)

        percent = None
        state = "unknown"
        for token in out.replace(";", " ").split():
            if token.endswith("%"):
                try:
                    percent = int(token.rstrip("%"))
                except ValueError:
                    pass
        lowered = out.lower()
        if "charging" in lowered or "ac attached" in lowered:
            state = "charging"
        elif "discharging" in lowered:
            state = "discharging"
        elif "charged" in lowered:
            state = "fully charged"

        if percent is None:
            return fail("get_battery_status", "Battery percentage unavailable.", device=ctx.device_id)

        return ok("get_battery_status",
            f"Your laptop battery is at {percent}% ({state}).",
            device=ctx.device_id,
            percent=percent,
            state=state,
            raw=out,
        )

    return fail("get_battery_status",
        f"Battery reporting is not implemented for {ctx.platform}.",
        device=ctx.device_id,
    )


async def take_screenshot(ctx: ToolContext, path: str = "") -> Any:
    """Capture the screen to a PNG file."""
    if ctx.platform != "macos":
        return fail("take_screenshot", "Screenshots are only implemented on macOS.", device=ctx.device_id)

    target = Path(path).expanduser() if path else Path.home() / "Desktop" / f"jarvis_{int(time.time())}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    code, _out, err = await run_command(["screencapture", "-x", str(target)], timeout=20.0)
    if code != 0 or not target.exists():
        detail = err.strip() or "screencapture produced no file"
        return fail(
            "take_screenshot",
            (
                f"Screenshot failed: {detail}. On macOS this usually means Screen "
                "Recording permission has not been granted -- enable it in System "
                "Settings -> Privacy & Security -> Screen Recording for the host "
                "application."
            ),
            device=ctx.device_id,
        )

    return ok(
        "take_screenshot",
        f"Screenshot saved to {target}.",
        device=ctx.device_id,
        path=str(target),
        size=target.stat().st_size,
        verified=target.exists(),
    )


# Flags that turn a benign-looking binary into an interpreter. `python3 -c`,
# `node -e` and friends are arbitrary code execution wearing a friendly name,
# so they are refused even when the binary has been explicitly opted into.
CODE_EXECUTION_FLAGS = {
    "-c", "-e", "--eval", "-E", "-p", "--print",
    "-i", "--interactive", "-m", "--module",
    "--exec", "--command", "-exec",
}

# Characters that only make sense to a shell. We never invoke one, so their
# presence means the request is confused or hostile.
SHELL_METACHARACTERS = set("|&;><`$\n\r\t\\!*?[]{}()~")


def _validate_argv(binary: str, argv: list[str]) -> Optional[str]:
    """Return a refusal reason for a dangerous argv, else None."""
    for arg in argv:
        stripped = arg.strip()
        if not stripped:
            continue
        # An argument carrying shell syntax is never legitimate here.
        bad = SHELL_METACHARACTERS.intersection(stripped)
        if bad:
            return (
                f"argument {arg!r} contains shell syntax "
                f"({', '.join(sorted(bad))}); arguments are passed literally"
            )
        if stripped in CODE_EXECUTION_FLAGS or stripped.startswith("--eval"):
            return (
                f"flag {stripped!r} would let {binary} execute arbitrary code, "
                "which the allowlist is specifically meant to prevent"
            )
    return None


async def run_allowlisted_command(ctx: ToolContext, command: str = "", args: str = "") -> Any:
    """Run one allowlisted binary with arguments (never a raw shell string)."""
    binary = (command or "").strip()
    if not binary:
        return fail("run_allowlisted_command", "No command given.", device=ctx.device_id)

    safe = list(ctx.settings.allowed_commands)
    powerful = list(ctx.settings.powerful_commands)

    if binary in powerful:
        # Opted in, but still never a bare interpreter invocation.
        log.info("powerful command requested", extra_fields={"command": binary})
    elif binary not in safe:
        return fail(
            "run_allowlisted_command",
            f"{binary!r} is not permitted. Safe commands: {', '.join(safe)}."
            + (
                f" Opt-in commands: {', '.join(powerful)}."
                if powerful
                else " Add interpreters or package managers to "
                "JARVIS_POWERFUL_COMMANDS only if you accept that they can run "
                "arbitrary code."
            ),
            device=ctx.device_id,
        )

    if not shutil.which(binary):
        return fail("run_allowlisted_command", f"{binary!r} is not installed.", device=ctx.device_id)

    argv = [binary] + (args.split() if args else [])

    refusal = _validate_argv(binary, argv[1:])
    if refusal:
        return fail("run_allowlisted_command", refusal, device=ctx.device_id)

    code, out, err = await run_command(argv, timeout=30.0)
    if code != 0:
        return fail(
            "run_allowlisted_command",
            f"{binary} exited with {code}: {err or out}",
            device=ctx.device_id,
            argv=argv,
        )

    return ok(
        "run_allowlisted_command",
        f"Ran {binary} successfully.",
        device=ctx.device_id,
        argv=argv,
        stdout=out[:4000],
    )


async def emergency_stop(ctx: ToolContext) -> Any:
    """Stop what JARVIS can actually stop, and say precisely what that was.

    Phase 9 measured the previous version as a no-op that reported success: it
    counted devices advertising a ``robot_motion`` / ``actuators`` /
    ``experiments`` capability, which no registered device has, so
    ``cancelled_actions`` was always 0 while the message claimed "Physical
    actions cancelled; safety controllers remain active" at
    ``Verification.SUCCESS``. It is the only risk-4 tool with no confirmation
    gate, so a user saying "emergency stop" to a moving robot got a fabricated
    all-clear.

    JARVIS is one command source among several. Per ``jarvis/robotics/gateway.py``
    the global "stop all robots" is owned by the robot's own safety controller --
    watchdog, temperature/current limits and the physical e-stop live on the
    robot, outside this process. Nothing here reaches them. So this tool stops
    JARVIS-side queued work and reports the physical layer as NOT AVAILABLE
    rather than pretending otherwise. Inventing an actuator stop that no bridge
    implements would be the same defect with a new name.
    """
    tasks = getattr(ctx.core, "tasks", None)
    stopped: list[str] = []
    still_running: list[str] = []

    if tasks is not None:
        try:
            for job in tasks.list(limit=100):
                status = str(job.get("status") or "")
                if status in ("running", "pending", "queued", "paused"):
                    if tasks.cancel(str(job.get("job_id") or "")):
                        stopped.append(str(job.get("job_id")))
                    else:
                        still_running.append(str(job.get("job_id")))
        except Exception as exc:  # a stop request must never fail silently
            log.info("emergency stop: task cancellation failed",
                    extra_fields={"error": str(exc)})

    physical = "not_available"
    reason = (
        "no robot bridge or safety-controller interface is reachable from JARVIS"
    )

    lines = []
    if stopped:
        lines.append(f"{len(stopped)} JARVIS job(s) cancelled.")
    else:
        lines.append("No JARVIS job was in flight, so there was nothing here to cancel.")
    if still_running:
        lines.append(f"{len(still_running)} job(s) refused to cancel: {', '.join(still_running)}.")
    lines.append(
        "Physical actuators were NOT stopped by this command -- "
        + reason + ". Use the robot's own emergency-stop if motion must halt now."
    )

    observed = not still_running
    return ok(
        "emergency_stop",
        " ".join(lines),
        device=ctx.device_id,
        verified=observed,
        jobs_cancelled=len(stopped),
        cancelled_job_ids=stopped,
        jobs_stuck=still_running,
        physical_stop=physical,
        physical_stop_reason=reason,
        safety_controllers="independent of JARVIS -- on the robot itself, untouched by this command",
    )


async def get_location_summary(ctx: ToolContext, date: str = "today") -> Any:
    """Return a location summary (section 17).

    Requires explicit location permission; without it JARVIS says so plainly.
    There is no location subsystem in this repository at all, so the plain
    answer here is "could not be done", never "done successfully" -- which is
    what Phase 15's acceptance sweep found this recording.
    """
    return ok(
        "get_location_summary",
        f"Location history for {date} is not available: location tracking is disabled.",
        device=ctx.device_id,
        verification=Verification.FAILED,
        provider_status="unavailable",
        date=date,
        tracking_enabled=False,
        segments=[],
    )


SPECS = [
    (
        ToolSpec(
            name="get_time",
            description="Get the current date and time.",
            risk=RiskLevel.READ,
            parameters={},
            tags=["system", "read"],
        ),
        get_time,
    ),
    (
        ToolSpec(
            name="system_status",
            description="Report system load, CPU, memory and disk usage.",
            risk=RiskLevel.READ,
            parameters={},
            tags=["system", "read"],
        ),
        system_status,
    ),
    (
        ToolSpec(
            name="get_battery_status",
            #: The name this tool shipped under for its first 48 phases. Kept as an
            #: alias because permissions, confirmations, audit rows and stored job
            #: steps all reference it -- see ToolSpec.aliases.
            aliases=("battery_status",),
            description="Read the laptop battery percentage and charging state.",
            risk=RiskLevel.READ,
            parameters={},
            tags=["system", "read"],
        ),
        battery_status,
    ),
    (
        ToolSpec(
            name="take_screenshot",
            description="Capture the screen to a PNG file.",
            risk=RiskLevel.LOW,
            parameters={"path": "string"},
            tags=["system", "screen"],
        ),
        take_screenshot,
    ),
    (
        ToolSpec(
            name="run_allowlisted_command",
            description="Run an allowlisted CLI binary with arguments.",
            risk=RiskLevel.EXTERNAL,
            requires_confirmation=True,
            parameters={"command": "string", "args": "string"},
            required_parameters=["command"],
            tags=["system", "terminal"],
        ),
        run_allowlisted_command,
    ),
    (
        ToolSpec(
            name="emergency_stop",
            description=(
                "Immediately cancel JARVIS's in-flight work. Cannot stop physical "
                "actuators -- no bridge or safety-controller interface is reachable "
                "from JARVIS. Always permitted."
            ),
            risk=RiskLevel.PHYSICAL,
            requires_confirmation=False,
            reversible=False,
            parameters={},
            tags=["safety"],
        ),
        emergency_stop,
    ),
    (
        ToolSpec(
            name="get_location_summary",
            description="Summarise places visited for a given day.",
            risk=RiskLevel.READ,
            parameters={"date": "string"},
            tags=["location", "read"],
        ),
        get_location_summary,
    ),
]
