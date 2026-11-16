"""HTTP surface for real desktop control.

A thin, authenticated router over :mod:`jarvis.desktop.executor`, following the
same shape as the other feature routers in this package. It adds no server and no
transport of its own: the native executor is a local binary invoked by the
backend, which is the channel that already exists.

Two deliberate properties:

* **Every write carries the same confirmation the tool layer requires.** The
  routes do not bypass ``PermissionEngine``; they call the registered tools, so a
  click made through this surface goes through exactly the gate a click made by a
  mission does. A separate code path here would be a second door into the same
  room.
* **Reads are open to any authenticated caller, writes are not.** Asking what
  macOS allows costs nothing and changes nothing; posting an event does neither.

The router is mounted under ``/desktop``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException

from ..desktop import executor


#: The tools whose dispatch must pass the Control Tower's pause gate. A move is
#: included: it changes what the user sees, and a takeover that kept moving the
#: pointer while somebody tried to work would be the badge-that-lies again.
_GATED_TOOLS = {
    "move": "desktop_move_pointer",
    "click": "desktop_click",
    "scroll": "desktop_scroll",
    "drag": "desktop_drag",
    "type": "desktop_type_text",
    "key": "desktop_press_key",
    "press-element": "desktop_press_element",
    "focus": "desktop_focus_window",
    # Recovery is gated too: it posts input events, and a takeover that still
    # released buttons would be acting on the machine while somebody else has it.
    "release-input": "desktop_release_input",
}


def _admit(action: str) -> None:
    """Refuse a desktop action the operator has paused or stopped.

    The desktop routes were a second door into the same room: they called the
    executor directly, so a human takeover stopped the mission runner while a
    click from this surface still went through. Measured in the V14 acceptance
    run before this guard existed: a pointer move succeeded *during* a takeover.

    Raises HTTP 409 carrying the tower's own reason, so the caller sees why.
    """
    tool = _GATED_TOOLS.get(action)
    if tool is None:
        return
    from ..core.control_tower import ControlTower, WorkerRefusal

    try:
        ControlTower.get().admit(tool)
    except WorkerRefusal as refusal:
        raise HTTPException(status_code=409, detail=str(refusal))


def build_router(require_auth) -> APIRouter:
    router = APIRouter(prefix="/desktop", tags=["desktop"],
                       dependencies=[Depends(require_auth)])

    @router.get("")
    async def posture() -> Dict[str, Any]:
        """What this machine will actually let Vantage do, and what it will not.

        The answer distinguishes the three permissions, because they are granted
        separately and fail separately: cursor movement needs only screen
        recording, click/type needs post-event access, and reading the UI needs
        Accessibility.
        """
        result = executor.permissions()
        return {
            **result,
            "executor": str(executor.executor_path() or ""),
            "capabilities": {
                "move_cursor": bool(result.get("can_move_cursor")),
                "click": bool(result.get("can_click")),
                "type": bool(result.get("can_type")),
                "read_ui": bool(result.get("can_read_ui")),
                "screen_capture": bool(result.get("screen_capture")),
            },
            # Stated in the payload so a UI cannot render "ready" from a partial
            # answer: a click needs a permission a cursor move does not.
            "note": ("cursor movement, clicking, typing and UI reading are granted "
                     "separately. A yes for one is not a yes for another."),
        }

    @router.post("/request-access")
    async def request_access() -> Dict[str, Any]:
        """Ask macOS to show its own Accessibility prompt for the executor."""
        return executor.request_access()

    @router.post("/move")
    async def move(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Move the real pointer. Verified by reading its position back."""
        _admit("move")
        body = body or {}
        x, y = body.get("x"), body.get("y")
        if x is None or y is None:
            raise HTTPException(status_code=422, detail="a move needs x and y")
        return executor.move(float(x), float(y),
                             smooth=bool(body.get("smooth")),
                             duration_ms=int(body.get("duration_ms") or 0))

    @router.post("/click")
    async def click(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        """Post one click. Returns performed-unverified unless a caller verifies."""
        _admit("click")
        body = body or {}
        x, y = body.get("x"), body.get("y")
        if x is None or y is None:
            raise HTTPException(status_code=422, detail="a click needs x and y")
        return executor.click(float(x), float(y),
                              button=str(body.get("button") or "left"),
                              count=int(body.get("count") or 1))

    @router.post("/scroll")
    async def scroll(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        _admit("scroll")
        body = body or {}
        x, y = body.get("x"), body.get("y")
        if x is None or y is None:
            raise HTTPException(status_code=422, detail="a scroll needs x and y")
        return executor.scroll(float(x), float(y),
                               lines=int(body.get("lines") or 3),
                               horizontal=bool(body.get("horizontal")))

    @router.post("/drag")
    async def drag(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        _admit("drag")
        body = body or {}
        try:
            args = [float(body[key]) for key in ("from_x", "from_y", "to_x", "to_y")]
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=422,
                                detail="a drag needs from_x, from_y, to_x and to_y")
        return executor.drag(*args, duration_ms=int(body.get("duration_ms") or 400))

    @router.post("/type")
    async def type_text(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        _admit("type")
        text = str((body or {}).get("text") or "")
        if not text:
            raise HTTPException(status_code=422, detail="there was no text to type")
        return executor.type_text(text)

    @router.post("/key")
    async def press_key(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        _admit("key")
        body = body or {}
        key = str(body.get("key") or "")
        if not key:
            raise HTTPException(status_code=422, detail="no key was named")
        modifiers = body.get("modifiers") or []
        if isinstance(modifiers, str):
            modifiers = [part.strip() for part in modifiers.split(",") if part.strip()]
        return executor.press_key(key, list(modifiers))

    @router.post("/focus")
    async def focus(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        _admit("focus")
        body = body or {}
        return executor.focus_application(str(body.get("application") or ""),
                                          str(body.get("window") or ""))

    @router.get("/inspect")
    async def inspect(application: str = "", depth: int = 6) -> Dict[str, Any]:
        """Read an application's Accessibility tree. Read-only."""
        return executor.inspect(application, depth=depth)

    @router.post("/press-element")
    async def press_element(body: Dict[str, Any] = Body(None)) -> Dict[str, Any]:
        _admit("press-element")
        body = body or {}
        keyword = str(body.get("keyword") or "")
        if not keyword:
            raise HTTPException(status_code=422,
                                detail="a semantic press needs the element's label")
        return executor.press_element(str(body.get("application") or ""), keyword,
                                      role=str(body.get("role") or ""))

    @router.post("/probe")
    async def probe() -> Dict[str, Any]:
        """Move the pointer a bounded square and report the readback per leg.

        Deliberately moves only: this is the "is this real?" control, so it must
        be safe to press on a machine somebody is using.
        """
        _admit("move")
        from ..desktop import executor as _executor

        posture = _executor.permissions()
        origin = posture.get("cursor") or {}
        ox, oy = float(origin.get("x") or 0), float(origin.get("y") or 0)
        legs, checked = [(ox + 40, oy), (ox + 40, oy + 40),
                         (ox, oy + 40), (ox, oy)], []
        for tx, ty in legs:
            result = _executor.move(tx, ty)
            if result.get("status") != "verified":
                return {**result, "legs_checked": checked}
            after = result.get("cursor_after") or {}
            checked.append({"target": [tx, ty],
                            "observed": [after.get("x"), after.get("y")]})
        return {
            "status": "verified",
            "reason": (f"The pointer moved through {len(checked)} points and each move "
                       f"was confirmed by reading its position back."),
            "legs": checked,
            "process_name": posture.get("process_name"),
            "executable": posture.get("executable"),
        }

    @router.post("/release-input")
    async def release_input() -> Dict[str, Any]:
        """Release a synthetic button or modifier left held. Recovery only."""
        _admit("release-input")
        return executor.release_all()

    @router.get("/plan")
    async def plan(request: str = "", application: str = "",
                   x: float = -1, y: float = -1) -> Dict[str, Any]:
        """Turn a desktop instruction into typed operations, without acting.

        The planner is the only path from a sentence to a desktop action, and it
        never invents a target: an ambiguous request comes back as a
        ``clarification`` with no steps.
        """
        from ..desktop.planner import plan_desktop

        context: Dict[str, Any] = {}
        if application:
            context["application"] = application
        if x >= 0 and y >= 0:
            context["point"] = (x, y)
        result = plan_desktop(request, context=context or None)
        return {
            "status": "verified",
            "steps": [{"tool": s.tool, "params": s.params, "description": s.description}
                      for s in result.steps],
            "clarification": result.clarification,
            "notes": result.notes,
        }

    @router.get("/observe")
    async def observe(application: str = "", keyword: str = "",
                      role: str = "") -> Dict[str, Any]:
        if not keyword:
            raise HTTPException(status_code=422,
                                detail="an observation needs the element's label")
        return executor.read_element(application, keyword, role=role)

    @router.get("/verify")
    async def verify(application: str = "", keyword: str = "", key: str = "",
                     value: str = "", role: str = "") -> Dict[str, Any]:
        """The only read whose 'verified' means a UI outcome was observed."""
        if not (keyword and key):
            raise HTTPException(status_code=422,
                                detail="a change check needs the label and the attribute")
        return executor.verify_change(application, keyword, key=key, value=value,
                                      role=role)

    return router