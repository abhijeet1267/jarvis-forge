"""Disk space, read from the volume rather than estimated.

Asked for "control on the system", one of the first sentences was "check storage" -- and
this build had no tool for it at all, so the answer was "I understood the words but not the
goal". The numbers come from `df`, which asks the kernel; nothing here walks the filesystem
to guess what is using space, because that answer takes minutes and this one takes
milliseconds.

Sizes are reported in binary gibibytes (the unit `df` counts in is the 1 KiB block), and the
unit is part of every returned number, because "41.7 free" without one is a coin flip
against what the Finder says.
"""

from __future__ import annotations

import re
from typing import Any, List, Tuple

from ..core.models import RiskLevel, ToolSpec
from .base import ToolContext, fail, ok, run_command

TOOL = "check_storage"

#: A `df -k` line: filesystem, 1K-blocks, used, available, use%, mounted-on.
_ROW = re.compile(
    r"^(?P<device>\S+)\s+(?P<total>\d+)\s+(?P<used>\d+)\s+(?P<free>\d+)\s+"
    r"(?P<percent>\d+)%\s+.*\s(?P<mount>/\S*)$")


async def check_storage(ctx: ToolContext, volume: str = "/") -> Any:
    """Report total, used and free space on a volume."""
    target = (volume or "/").strip() or "/"
    code, out, err = await run_command(["df", "-k", target])
    if code != 0:
        return fail(TOOL, f"I could not read the disk: {(err or out).strip()[:120]}",
                    device=ctx.device_id)

    row = next((m for m in (_ROW.match(line) for line in out.splitlines()[1:]) if m), None)
    if row is None:
        return fail(TOOL, f"`df` answered something I cannot read for {target!r}.",
                    device=ctx.device_id, raw=out[:200])

    def gib(kib: str) -> float:
        return round(int(kib) / 1048576, 1)

    total, used, free = gib(row["total"]), gib(row["used"]), gib(row["free"])
    percent = int(row["percent"])
    mount, device = row["mount"], row["device"]
    line = (f"Storage: {free} GiB free of {total} GiB on {mount} "
            f"({percent}% used, {device})")
    return ok(TOOL, line, device=ctx.device_id, verified=True,
              mount=mount, volume_device=device, total_gb=total, used_gb=used,
              free_gb=free, used_percent=percent, unit="GiB")


SPECS: List[Tuple[ToolSpec, Any]] = [
    (
        ToolSpec(
            name=TOOL,
            description="Report free and total space on a disk volume.",
            agent="laptop",
            risk=RiskLevel.READ,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"volume": "string"},
            tags=["laptop", "read", "storage", "disk"],
        ),
        check_storage,
    ),
]
