"""Scheduling mission steps: dependencies first, and no two writers on one file.

The mission runner executes steps in order. That is correct and safe, and it is
also slower than it needs to be when a mission's steps are genuinely
independent. This module decides *which* steps may run at the same time.

Two rules make concurrency safe here, and both fail closed:

* **A dependency is a hard barrier.** A step whose `depends_on` names a step
  that has not finished is never eligible, whatever else is true.
* **Two steps that touch the same resource are never concurrent.** They are
  serialised, not left to race. The resource is the step's declared path (and
  its parent directory, since creating `a/b.txt` and writing `a/` conflict).

The scheduler is pure: it takes the steps and their current statuses and returns
the set that may start now. It performs no I/O and runs nothing, so it can be
unit-tested exhaustively and used by any runner.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence, Set

#: A step in one of these states is finished as far as ordering is concerned.
_FINISHED = frozenset({"done", "failed", "skipped"})

#: A step in one of these states is occupying its resources right now.
_OCCUPYING = frozenset({"running"})


def _resource_keys(step: Any) -> Set[str]:
    """The resources a step claims while it runs.

    A step claims the exact path it names. It does **not** claim the containing
    directory: an earlier version did, on the reasoning that creating `a/b.txt`
    and deleting `a/` conflict -- but that also serialised every write in a
    shared folder, which defeats the point of scheduling at all. The narrower
    claim is the defensible one: two steps that name the same file cannot run
    together, and a step that names a directory is claiming that directory.

    A step with no path claims nothing and can always run alongside another.
    """
    import posixpath

    params = getattr(step, "params", None) or {}
    keys: Set[str] = set()
    for name in ("path", "source", "target", "destination"):
        value = str(params.get(name) or "").strip()
        if value:
            keys.add(posixpath.normpath(value))
    return keys


def ready_steps(steps: Sequence[Any]) -> List[Any]:
    """The steps that may start right now, in declaration order.

    A step is eligible when:

    * it is `pending` (never started),
    * every step it depends on has finished, and
    * no *running* step, and no other step chosen earlier in this same call,
      claims a resource it claims.

    Choosing in declaration order and reserving resources as we go is what keeps
    two writers to one file from both being selected in a single pass.
    """
    by_id = {str(getattr(step, "step_id", "")): step for step in steps}

    finished = {sid for sid, step in by_id.items()
                if str(getattr(step, "status", "")) in _FINISHED}
    occupied: Set[str] = set()
    for step in steps:
        if str(getattr(step, "status", "")) in _OCCUPYING:
            occupied |= _resource_keys(step)

    chosen: List[Any] = []
    for step in steps:
        if str(getattr(step, "status", "")) != "pending":
            continue

        dependencies = [str(d) for d in (getattr(step, "depends_on", None) or [])]
        if any(dep not in finished for dep in dependencies):
            continue

        claims = _resource_keys(step)
        if claims & occupied or _nested_under(claims, occupied):
            # Someone is writing there right now, or a step chosen earlier in
            # this same pass is. Wait rather than race. `_nested_under` covers
            # the case where one step names a directory and another names a
            # file inside it -- different strings, same resource.
            continue

        chosen.append(step)
        occupied |= claims

    return chosen


def _nested_under(claims: Set[str], occupied: Set[str]) -> bool:
    """Whether one claim is a directory containing another's path, or vice versa."""
    for mine in claims:
        for theirs in occupied:
            if mine == theirs:
                return True
            if mine.startswith(theirs.rstrip("/") + "/"):
                return True
            if theirs.startswith(mine.rstrip("/") + "/"):
                return True
    return False


def blocked_reason(step: Any, steps: Sequence[Any]) -> str:
    """Why a pending step is not eligible, in words a person can act on."""
    by_id = {str(getattr(s, "step_id", "")): s for s in steps}
    unfinished = [str(d) for d in (getattr(step, "depends_on", None) or [])
                  if str(getattr(by_id.get(str(d)), "status", "")) not in _FINISHED]
    if unfinished:
        return (f"waiting for {', '.join(unfinished)} to finish")

    claims = _resource_keys(step)
    for other in steps:
        if str(getattr(other, "status", "")) != "running":
            continue
        shared = claims & _resource_keys(other)
        if shared:
            return (f"waiting for {other.step_id}, which is writing "
                    f"{', '.join(sorted(shared))}")
    return ""


def schedule_summary(steps: Sequence[Any]) -> Dict[str, Any]:
    """A snapshot for the UI: what can run, what is running, what is waiting."""
    ready = ready_steps(steps)
    waiting = [s for s in steps if str(getattr(s, "status", "")) == "pending"
               and s not in ready]
    return {
        "ready": [getattr(s, "step_id", "") for s in ready],
        "running": [getattr(s, "step_id", "") for s in steps
                    if str(getattr(s, "status", "")) == "running"],
        "waiting": [{"step_id": getattr(s, "step_id", ""),
                     "reason": blocked_reason(s, steps)} for s in waiting],
        "finished": [getattr(s, "step_id", "") for s in steps
                     if str(getattr(s, "status", "")) in _FINISHED],
    }