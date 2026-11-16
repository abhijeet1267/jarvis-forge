"""Reality HUD: what this Mac actually looks like right now, and a takeover that bites.

An agent acting on a machine needs one screen that answers "what is really going
on?" without asking anybody to trust a claim. This module assembles that picture
from sources that already exist and are already honest:

* **Permissions** come from :class:`PermissionStatusService`, which preflights the
  real macOS APIs and reports ``UNKNOWN`` when it cannot ask -- it does not
  upgrade a guess.
* **Workers** come from the Control Tower: what is running, in flight, paused,
  stopped or out of budget.
* **Blocked dispatch** counts come from the tower's own refusal counters, so
  "takeover is working" is a number that only moves when a call was refused.

The takeover half is the part with a design question in it. A "human takeover"
button that only redraws a badge is worse than none, so this reuses the one
control that is *enforced at a dispatch boundary*: the tower's pause gate, which
every tool call in the system passes through (chat, mission runner, recovery
retry all funnel into ``Agent.execute``).

What it deliberately does **not** claim:

* It cannot freeze a call that is already inside a handler. Pausing stops the
  *next* dispatch; saying otherwise would be the badge-that-lies this module
  exists to avoid.
* It does not take over the screen. There is no pointer control, no synthetic
  input and no screenshot here. A takeover hands the *machine* back by stopping
  what the agents would do next, which is the part this system can actually
  enforce.
* :func:`release_all` resumes only what *this* screen paused -- the ones the
  tower says were paused by the operator with this module's reason. A worker a
  person paused by hand is left paused.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger

log = get_logger("core.reality_hud")

#: Marks a pause this module set, so releasing does not touch anybody else's.
TAKEOVER_REASON = "human takeover: what is on screen did not match what it reported"

#: Permission states that mean a capability is genuinely usable right now. Only
#: these are counted as available; every other value is a reason it is not.
_USABLE = frozenset({"GRANTED", "NOT_REQUIRED"})


def _permissions(service=None) -> Dict[str, Any]:
    """The real permission posture, with the caveat the service itself states."""
    if service is None:
        from ..permissions.service import PermissionStatusService

        service = PermissionStatusService()
    try:
        rows = service.status()
    except Exception as exc:
        # A HUD that cannot read permissions must say so, never show a green row.
        return {"rows": [], "usable": [], "unusable": [],
                "note": f"permission status could not be read: {exc}",
                "readable": False}

    usable: List[str] = []
    unusable: List[Dict[str, str]] = []
    for row in rows:
        name = str(row.get("capability") or "")
        status = str(row.get("status") or "UNKNOWN")
        if status in _USABLE:
            usable.append(name)
        else:
            unusable.append({"capability": name, "status": status,
                             "why": str(row.get("explanation") or "")[:200]})
    return {
        "rows": rows,
        "usable": usable,
        "unusable": unusable,
        # Whether these values describe the bundled app, which is the question
        # `status()` is careful to answer separately.
        "about_bundled_host": bool(rows[0].get("about_bundled_host")) if rows else False,
        "note": str(rows[0].get("status_note") or "") if rows else "",
        "readable": True,
    }


def _workers(tower) -> Dict[str, Any]:
    """What the tower says each worker is doing, including refusals."""
    rows: List[Dict[str, Any]] = []
    for worker in tower.workers():
        body = worker.to_dict()
        rows.append({
            "worker": body.get("name"),
            "state": body.get("state"),
            "in_flight": int(body.get("in_flight") or 0),
            "calls_made": int(body.get("calls_made") or 0),
            "calls_refused": int(body.get("calls_refused") or 0),
            "failures": int(body.get("failures") or 0),
            "remaining_budget": body.get("remaining_budget"),
            "paused_by": body.get("paused_by") or "",
            "paused_reason": body.get("paused_reason") or "",
            "last_tool": body.get("last_tool") or "",
            "last_call_at": body.get("last_call_at"),
        })
    return {
        "workers": rows,
        "running": sum(1 for r in rows if r["in_flight"] > 0),
        "paused": sum(1 for r in rows if r["state"] == "paused"),
        "stopped": sum(1 for r in rows if r["state"] == "stopped"),
        "refused_total": sum(r["calls_refused"] for r in rows),
    }


def snapshot(*, tower=None, permission_service=None) -> Dict[str, Any]:
    """One picture of the machine: permissions, workers, and the takeover gate."""
    if tower is None:
        from .control_tower import ControlTower

        tower = ControlTower.get()

    perms = _permissions(permission_service)
    work = _workers(tower)

    held = [r for r in work["workers"] if r["state"] == TAKEOVER_ACTIVE_STATE()]
    return {
        "read_at": time.time(),
        "permissions": perms,
        "workers": work,
        # The state of the takeover gate itself, stated rather than inferred
        # from the worker list by the reader.
        "takeover": {
            "held": bool(held),
            "held_workers": [r["worker"] for r in held],
            "reason": TAKEOVER_REASON,
            "enforced_at": "the dispatch boundary every tool call passes through",
            "cannot": ("freeze a call already inside a handler; only the next "
                       "dispatch is refused"),
        },
        "honest": (
            "permissions are preflighted from the real macOS APIs and stay UNKNOWN "
            "when they cannot be asked; worker counts come from the tower's own "
            "refusal counters, so 'blocked' only moves when a call was refused"
        ),
    }


def TAKEOVER_ACTIVE_STATE() -> str:
    """The tower state a takeover produces. Kept as a call so one definition."""
    from .control_tower import PAUSED

    return PAUSED


def take_over(*, tower=None, reason: str = "") -> Dict[str, Any]:
    """Pause every running worker, and report exactly what that did.

    Pausing is the enforced control: a paused worker refuses its next dispatch
    with a reason. Workers already paused by somebody else are left alone and
    named, so "I took over" never means "I changed your pause".
    """
    if tower is None:
        from .control_tower import ControlTower

        tower = ControlTower.get()

    text = (reason or TAKEOVER_REASON).strip()
    paused: List[str] = []
    already: List[str] = []
    skipped: List[Dict[str, str]] = []
    for worker in tower.workers():
        if worker.stopped:
            skipped.append({"worker": worker.name, "why": "already stopped"})
            continue
        if worker.paused:
            # Somebody else's pause. Names it rather than adopting it.
            already.append(worker.name)
            continue
        try:
            tower.pause(worker.name, by="operator", reason=text)
            paused.append(worker.name)
        except Exception as exc:
            skipped.append({"worker": worker.name, "why": str(exc)})

    log.info("human takeover", extra_fields={"paused": paused, "already": already})

    # Pausing stops the *next* dispatch. It does nothing about input that is
    # already held: a drag in progress has the mouse button down, and a person
    # who grabs the mouse to stop it would find their own pointer dragging
    # something. Taking over therefore also releases whatever the executor is
    # holding, and reports what that actually did rather than assuming.
    released = _release_held_input()

    return {
        "paused": paused,
        "already_paused_by_someone_else": already,
        "skipped": skipped,
        "reason": text,
        "released_input": released,
        "effect": ("each paused worker now refuses its next dispatch, and any "
                   "mouse button or modifier the executor was holding is released"),
        "cannot": ("a call already inside a handler is not interrupted; the next "
                   "dispatch is what stops"),
    }


def _release_held_input() -> Dict[str, Any]:
    """Release any synthetic button or modifier, and say what happened.

    Never raises: a takeover must complete even when the desktop executor is
    missing or refuses. The result is reported either way, because a takeover
    that silently failed to release a stuck button is the exact state this is
    meant to prevent.
    """
    try:
        from ..desktop import executor

        return executor.release_all()
    except Exception as exc:
        return {"status": "failed", "reason": f"held input could not be released: {exc}"}


def release_all(*, tower=None) -> Dict[str, Any]:
    """Resume the workers *this module* paused, and leave every other pause alone.

    The check is on the recorded pause reason, not on the state: a worker paused
    by a person is indistinguishable from one paused by a takeover if you only
    look at ``paused``, and resuming it would undo somebody's decision.
    """
    if tower is None:
        from .control_tower import ControlTower

        tower = ControlTower.get()

    resumed: List[str] = []
    left: List[Dict[str, str]] = []
    for worker in tower.workers():
        if not worker.paused:
            continue
        if str(worker.paused_by) == "operator" and \
                str(worker.paused_reason).startswith("human takeover"):
            try:
                tower.resume(worker.name, by="operator")
                resumed.append(worker.name)
            except Exception as exc:
                left.append({"worker": worker.name, "why": str(exc)})
        else:
            left.append({"worker": worker.name,
                         "why": f"paused by {worker.paused_by or 'someone'}: "
                                f"{worker.paused_reason or 'no reason recorded'}"})

    log.info("human takeover released", extra_fields={"resumed": resumed})
    return {
        "resumed": resumed,
        "left_paused": left,
        "note": ("only workers this screen paused were resumed; a pause somebody "
                 "set by hand is theirs to lift"),
    }