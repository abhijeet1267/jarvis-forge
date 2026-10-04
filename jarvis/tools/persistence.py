"""The login item as a governed capability, not a sentence.

``jarvis.persistence`` knows how to talk to launchd. This module is the part the pipeline is
allowed to reach, and the gating is the point:

* **Reading is a READ.** "Does Vantage start on login" is a question about this machine, and the
  answer is whatever launchd says right now.
* **Installing and removing are EXTERNAL and confirmation-gated.** In this registry EXTERNAL means
  "changes something outside the process", and every EXTERNAL tool is confirmation-gated -- a
  login item is the strongest example of that in the product, because what it changes is what
  runs when nobody is watching.
* **Both write handlers report from the re-read.** ``enable`` returns what launchd says after the
  bootstrap, not what the bootstrap returned, for the reason Phase 56 found with macOS
  permissions: the request's own answer is not the world's answer.

There is deliberately no planner route to either write. A mis-parsed sentence should not be able
to install something that runs unattended; the confirmation card is the door, and the card is
raised by a person pressing a control.
"""

from __future__ import annotations

from typing import Any, Dict

from ..core.models import RiskLevel, ToolSpec
from ..logging_setup import get_logger
from .base import ToolContext, fail, ok

log = get_logger("tools.persistence")

#: What each launchd state means to a person reading one line of output.
_ANSWERS = {
    "loaded": "Vantage starts at login and launchd relaunches it if it crashes.",
    "running": "Vantage starts at login and launchd relaunches it if it crashes.",
    "installed_not_loaded": (
        "The login item's definition file is there but launchd has not accepted it, so nothing "
        "starts at login. That is a half-installed state, not a working one."),
    "loaded_without_file": (
        "launchd is running the job but its definition file is gone. It stays up until the next "
        "login and does not come back after that."),
    "not_installed": "Vantage only runs while its window is open.",
    "unsupported": "This machine has no login-item facility Vantage can use.",
}


def _service(ctx: ToolContext) -> Any:
    from ..persistence import HostPersistenceService

    return HostPersistenceService(settings=ctx.settings)


def _label(state: Dict[str, Any]) -> str:
    return _ANSWERS.get(str(state.get("state")), "Vantage's start-up state could not be read.")


async def get_host_persistence_status(ctx: ToolContext, **_: Any) -> Any:
    state = _service(ctx).status()
    return ok("get_host_persistence_status", _label(state), device=ctx.device_id,
              state=state["state"], installed=state["installed"], loaded=state["loaded"],
              pid=state["pid"], path=state["path"], answered_by=state["answered_by"])


async def enable_host_persistence(ctx: ToolContext, **_: Any) -> Any:
    service = _service(ctx)
    result = service.enable()
    if not result.get("ok"):
        return fail("enable_host_persistence", str(result.get("error")), device=ctx.device_id,
                    state=result.get("state", "not_installed"))
    state = service.status()
    settled = state["state"] in ("loaded", "running")
    return ok("enable_host_persistence", _label(state), device=ctx.device_id,
              verified=settled, state=state["state"], pid=state["pid"], path=state["path"],
              detail=("launchd was asked to take it, and then asked again what it thinks"
                      if settled else "launchd has not accepted the job"))


async def disable_host_persistence(ctx: ToolContext, **_: Any) -> Any:
    service = _service(ctx)
    result = service.disable()
    if not result.get("ok"):
        return fail("disable_host_persistence", str(result.get("error")), device=ctx.device_id,
                    state=result.get("state", "loaded"))
    state = service.status()
    cleared = state["state"] == "not_installed"
    return ok("disable_host_persistence", _label(state), device=ctx.device_id,
              verified=cleared, state=state["state"],
              detail=("the job is gone from launchd and the file is gone from disk"
                      if cleared else "launchd no longer runs it; the file is what remains"))


SPECS = [
    (
        ToolSpec(
            name="get_host_persistence_status",
            description="Report whether Vantage starts at login and is kept running by the system.",
            risk=RiskLevel.READ,
            parameters={},
            tags=["system", "read", "persistence"],
        ),
        get_host_persistence_status,
    ),
    (
        ToolSpec(
            name="enable_host_persistence",
            description=(
                "Install a macOS login item so the laptop keeps answering after its window "
                "closes. Always asks first: it changes what runs when nobody is watching."
            ),
            risk=RiskLevel.EXTERNAL,
            requires_confirmation=True,
            parameters={},
            tags=["system", "persistence"],
        ),
        enable_host_persistence,
    ),
    (
        ToolSpec(
            name="disable_host_persistence",
            description=(
                "Remove the login item, so Vantage again runs only while its window is open. "
                "Reading launchd afterwards decides whether this reports success."
            ),
            risk=RiskLevel.EXTERNAL,
            requires_confirmation=True,
            parameters={},
            tags=["system", "persistence"],
        ),
        disable_host_persistence,
    ),
]
