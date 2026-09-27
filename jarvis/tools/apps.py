"""Application control tools (section 11).

macOS is driven through ``open`` and AppleScript -- the official, scriptable
path -- rather than GUI clicking. Other platforms degrade gracefully.
"""

from __future__ import annotations

from ..core.models import RiskLevel, ToolSpec
from ..logging_setup import get_logger
from .base import ToolContext, fail, ok, run_command

log = get_logger("tools.apps")


async def open_app(ctx: ToolContext, app: str = "") -> "object":
    """Launch an application by key, alias or natural phrase."""
    spec = ctx.apps.resolve(app, platform=ctx.platform)
    if spec is None:
        return fail(
            "open_app",
            f"I could not find an application matching {app!r}.",
            device=ctx.device_id,
            query=app,
        )

    target = ctx.apps.launch_target(spec, platform=ctx.platform)
    if not target:
        return fail(
            "open_app",
            f"{spec.display_name} is not available on {ctx.platform}.",
            device=ctx.device_id,
        )

    if ctx.platform == "macos":
        code, _out, err = await run_command(["open", "-a", target])
    elif ctx.platform == "linux":
        code, _out, err = await run_command([target])
    else:  # windows
        code, _out, err = await run_command(["cmd", "/c", "start", "", target])

    if code != 0:
        return fail(
            "open_app",
            f"Could not open {spec.display_name}: {err or 'launch failed'}",
            device=ctx.device_id,
        )

    return ok(
        "open_app",
        f"Opening {spec.display_name} on your {ctx.platform} machine.",
        device=ctx.device_id,
        app_key=spec.key,
        display_name=spec.display_name,
        risk=int(RiskLevel.LOW),
    )


async def close_app(ctx: ToolContext, app: str = "") -> "object":
    """Quit an application gracefully."""
    spec = ctx.apps.resolve(app, platform=ctx.platform)
    if spec is None:
        return fail(
            "close_app",
            f"I could not find an application matching {app!r}.",
            device=ctx.device_id,
        )

    target = ctx.apps.launch_target(spec, platform=ctx.platform)
    if ctx.platform == "macos":
        script = f'tell application "{target}" to quit'
        code, _out, err = await run_command(["osascript", "-e", script])
    else:
        return fail(
            "close_app",
            f"Quitting applications is only implemented on macOS for now.",
            device=ctx.device_id,
        )

    if code != 0:
        return fail("close_app", f"Could not quit {spec.display_name}: {err}", device=ctx.device_id)

    return ok(
        "close_app",
        f"Quit {spec.display_name}.",
        device=ctx.device_id,
        app_key=spec.key,
    )


async def list_apps(ctx: ToolContext) -> "object":
    """List applications JARVIS knows how to launch."""
    apps = [
        {"key": a.key, "name": a.display_name, "category": a.category}
        for a in ctx.apps.all()
        if ctx.platform in a.platform
    ]
    return ok(
        "list_apps",
        f"I know {len(apps)} applications on this {ctx.platform} machine.",
        device=ctx.device_id,
        apps=apps,
    )


SPECS = [
    (
        ToolSpec(
            name="open_app",
            description="Launch an application (e.g. VS Code, Chrome, Terminal).",
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            reversible=True,
            parameters={"app": "string"},
            required_parameters=["app"],
            tags=["computer", "app"],
        ),
        open_app,
    ),
    (
        ToolSpec(
            name="close_app",
            description="Quit a running application.",
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            reversible=True,
            parameters={"app": "string"},
            required_parameters=["app"],
            tags=["computer", "app"],
        ),
        close_app,
    ),
    (
        ToolSpec(
            name="list_apps",
            description="List applications available to launch on this machine.",
            risk=RiskLevel.READ,
            parameters={},
            required_parameters=[],
            tags=["computer", "app"],
        ),
        list_apps,
    ),
]
