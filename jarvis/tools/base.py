"""Tool execution context and helpers.

Every tool handler receives a :class:`ToolContext` giving it access to
configuration, persistence, memory and the audit log -- but never to a raw
unrestricted shell.
"""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config import Settings, get_settings
from ..core.models import ActionResult, Verification
from ..db.database import get_db
from ..logging_setup import get_logger
from ..memory.graph import ProjectGraph
from ..memory.store import MemoryStore
from ..registry.apps import AppRegistry
from ..registry.devices import DeviceRegistry, get_device_registry

log = get_logger("tools.base")


@dataclass
class ToolContext:
    """Everything a tool may need to do its job."""

    device_id: str = "laptop_main"
    session_id: str = ""
    request_id: str = ""
    settings: Settings = field(default_factory=get_settings)
    apps: AppRegistry = field(default_factory=AppRegistry)
    devices: DeviceRegistry = field(default_factory=get_device_registry)
    #: The live core, when a tool needs to drive the pipeline (e.g. run a
    #: scheduled job or compose a briefing). None in unit tests.
    core: Any = None
    memory: MemoryStore = field(default_factory=MemoryStore)
    graph: ProjectGraph = field(default_factory=ProjectGraph)

    @property
    def platform(self) -> str:
        device = self.devices.get(self.device_id)
        return device.platform if device else "macos"


async def run_command(
    args: List[str],
    *,
    timeout: float = 20.0,
    cwd: Optional[str] = None,
    detach: bool = False,
) -> tuple[int, str, str]:
    """Run an argv list directly (no shell) and capture output.

    Using ``create_subprocess_exec`` with an explicit argv avoids shell
    interpolation entirely -- this is the core of "do not give the model
    unrestricted shell access".

    ``detach`` is for launching applications. A GUI app keeps the pipe it inherited
    open for as long as it runs, so a capturing caller waits for the app to *quit*
    and reports a timeout on a launch that worked. The caller is then expected to
    observe the real effect (that the editor opened the folder) rather than read an
    exit code as proof.
    """
    log.info("exec", extra_fields={"argv": args, "detach": detach})
    if detach:
        try:
            await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=cwd,
                start_new_session=True,
            )
        except FileNotFoundError:
            return 127, "", f"{args[0] if args else ''} is not installed"
        except OSError as exc:
            return 126, "", f"could not start {args[0] if args else ''}: {exc}"
        return 0, "", ""

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"command timed out after {timeout}s"
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


def ok(
    tool: str,
    message: str,
    device: str = "laptop_main",
    *,
    verified: Optional[bool] = None,
    verification: Optional[Verification] = None,
    **output: Any,
) -> ActionResult:
    """Build a successful :class:`ActionResult`.

    ``verified`` used to be hardcoded ``True`` and placed *after* ``**output``,
    so a tool reporting ``verified=dst.exists()`` wrote into the output dict and
    the field silently claimed success anyway -- which is how an offline robot
    command with no bridge ended up audited as a success. Both spellings are
    now honoured, and a handler that observed nothing can say so.

    A bare ``ok()`` still means "this handler established what it reports",
    which is true for reads and local computation. Anything claiming an effect
    in the world outside this process must pass its observation.
    """
    if verification is None:
        verification = Verification.SUCCESS if verified is not False else Verification.UNKNOWN
    return ActionResult(
        ok=True,
        tool=tool,
        device=device,
        message=message,
        output=output,
        verified=bool(verification is Verification.SUCCESS),
        verification=verification,
    )


def unverified(tool: str, message: str, device: str = "laptop_main", **output: Any) -> ActionResult:
    """The handler ran; whether the world changed is not established."""
    return ok(tool, message, device, verification=Verification.UNKNOWN, **output)


def fail(tool: str, error: str, device: str = "laptop_main", **output: Any) -> ActionResult:
    """Build a failed :class:`ActionResult`."""
    return ActionResult(
        ok=False,
        tool=tool,
        device=device,
        error=error,
        output=output,
        verified=False,
    )


def quote(value: str) -> str:
    """Shell-quote a single value for AppleScript/argv construction."""
    return shlex.quote(value)


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def as_dict(**kwargs: Any) -> Dict[str, Any]:
    return {k: v for k, v in kwargs.items() if v is not None}
