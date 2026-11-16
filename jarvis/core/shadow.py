"""ShadowMission: run the plan in a world where the effects are simulated.

The question a person asks before approving a mission is not "will it crash" but
"what will it change, and what will refuse it". Answering that by actually
running it is the wrong order -- that is the run itself, gated or not.

A shadow answers it by walking the plan step by step and, for each step:

* asking the **real** permission engine what it would decide (no imitation of
  policy: the same engine, the same action shape, the same session prefix);
* for a file effect, reading the target's current identity and computing what
  the file would contain afterwards -- held in memory, never written;
* for an effect that cannot be simulated, saying so instead of pretending.

Three things this deliberately does not do:

* **It does not write.** Nothing in this module opens a file for writing. The
  only filesystem calls are the ones ``ParallelWorld`` already uses to read a
  snapshot.
* **It does not claim a preview is a promise.** A shadow fixture is rolled
  forward in memory and later steps shadowed against it -- so two writes to one
  path show the second one's real before/after -- but a step that reads a file
  earlier in the plan is still predicted, not observed. A person can decline to
  trust that; nobody can remove it.
* **It does not touch the network or launch anything.** ``open_url``,
  ``open_application`` and ``run_allowlisted_command`` are reported as
  un-simulated with the reason, because their effects are outside this process
  and a "simulated" claim would be the kind of fake proof this project exists to
  avoid.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger
from .parallelworld import MAX_PREVIEW_BYTES, ParallelWorld, snapshot

log = get_logger("core.shadow")

#: Tools whose entire effect is on one local file, and which this shadow can
#: describe exactly. Every other tool is reported un-simulated rather than
#: guessed at.
_SIMULATED = frozenset({"write_file", "copy_file", "move_file", "delete_file"})

#: Tools that read a local file. Their shadow result is the *current* file,
#: which is correct unless an earlier step in the same plan changed it -- in
#: which case the shadowed content is used and the step is marked as a
#: prediction.
_READS = frozenset({"read_file"})


def _path_key(value: str) -> str:
    """One spelling of a path, so two steps naming the same file compare equal."""
    return str(Path(str(value or "")).expanduser().resolve())


class ShadowMission:
    """Predict what a plan would do, without doing any of it."""

    def __init__(self, guard=None) -> None:
        #: The files' own protector, so a shadow refuses a path the tools would
        #: refuse -- a preview that says "this would succeed" for a target the
        #: executor then rejects is worse than no preview.
        self._world = ParallelWorld(guard=guard)

    def run(self, steps: List[Dict[str, Any]], *, gate=None,
            device: str = "laptop_main",
            session_id: str = "shadow") -> Dict[str, Any]:
        """Walk ``steps`` in order, predicting each one.

        ``gate`` is called as ``gate(tool, params, device)`` and must return the
        permission engine's own answer for that action. It is injected rather
        than imported so this module stays testable without a live app, and so
        there is exactly one implementation of the policy question.
        """
        #: path -> (text, "shadowed" | "actual"). Rolled forward so the second
        #: write to one path reports the first write's result as its "before".
        world: Dict[str, tuple] = {}
        outcomes: List[Dict[str, Any]] = []
        for index, raw in enumerate(steps or []):
            outcomes.append(self._one(index, raw, world, gate, device, session_id))

        blocked = [o for o in outcomes if o["verdict"] == "would_be_refused"]
        unsimulated = [o for o in outcomes if o["verdict"] == "not_simulated"]
        return {
            "steps": outcomes,
            "summary": {
                "steps": len(outcomes),
                "would_run": sum(1 for o in outcomes
                                 if o["verdict"] in ("would_run", "predicted")),
                "would_be_refused": len(blocked),
                "would_need_approval": sum(
                    1 for o in outcomes if o["verdict"] == "would_need_approval"),
                "not_simulated": len(unsimulated),
            },
            # The one sentence a person should read before the detail. A shadow
            # with un-simulated steps is not a guarantee, and says so.
            "conclusion": _conclusion(outcomes),
            "note": ("Nothing was executed. File effects are computed from the "
                     "contents on disk now, in memory, and are not written. A step "
                     "that reads a file an earlier step wrote sees a prediction, "
                     "not the filesystem."),
        }

    # -- one step -----------------------------------------------------------
    def _one(self, index: int, raw: Dict[str, Any], world: Dict[str, tuple],
             gate, device: str, session_id: str) -> Dict[str, Any]:
        step_id = str(raw.get("step_id") or f"S{index + 1}")
        tool = str(raw.get("tool") or "")
        params = dict(raw.get("params") or {})
        entry: Dict[str, Any] = {
            "step_id": step_id, "title": str(raw.get("title") or tool),
            "tool": tool, "params": params,
        }

        permission = self._ask_gate(gate, tool, params, device, session_id)
        entry["permission"] = permission
        if permission and not permission.get("allowed", True):
            entry["verdict"] = ("would_need_approval"
                                if permission.get("decision") == "CONFIRM"
                                else "would_be_refused")
            entry["detail"] = permission.get("reason") or "the permission engine refused it"
            # A refused step changes nothing, so it is not rolled forward.
            return entry

        if tool in _READS:
            return self._reading(entry, params, world)
        if tool in _SIMULATED:
            return self._file_effect(entry, tool, params, world)
        return self._unsimulated(entry, tool)

    # -- the permission question -------------------------------------------
    def _ask_gate(self, gate, tool: str, params: Dict[str, Any],
                  device: str, session_id: str) -> Optional[Dict[str, Any]]:
        if gate is None:
            return None
        try:
            return gate(tool, params, device, session_id)
        except Exception as exc:  # a shadow must never crash the screen
            log.info("shadow gate raised", extra_fields={"error": str(exc)[:160]})
            return {"allowed": False, "decision": "ERROR",
                    "reason": f"the permission engine could not be asked: {exc}"}

    # -- file effects -------------------------------------------------------
    def _file_effect(self, entry: Dict[str, Any], tool: str,
                     params: Dict[str, Any], world: Dict[str, tuple]) -> Dict[str, Any]:
        if tool == "copy_file":
            source, target = params.get("source"), params.get("destination")
        elif tool == "move_file":
            source, target = params.get("source"), params.get("destination")
        else:
            source, target = None, params.get("path")

        if not target:
            entry["verdict"] = "would_be_refused"
            entry["detail"] = "no destination path is set, so this step cannot run"
            return entry

        key = _path_key(str(target))
        refusal = self._world._guard(str(Path(target).expanduser())) \
            if self._world._guard else None
        if refusal:
            entry["verdict"] = "would_be_refused"
            entry["detail"] = refusal
            return entry

        # What the target holds right now -- or what an earlier step in this same
        # plan would have left there.
        before, before_from = self._current(key, str(target), world)

        if tool == "delete_file":
            entry.update(verdict="would_run", effect="delete",
                         before=before[:MAX_PREVIEW_BYTES], after="",
                         source_is_shadowed=before_from == "shadowed")
            world[key] = ("", "shadowed")
            return entry

        if tool in ("copy_file", "move_file"):
            if not source:
                entry["verdict"] = "would_be_refused"
                entry["detail"] = "no source path is set, so this step cannot run"
                return entry
            source_refusal = self._world._guard(str(Path(source).expanduser())) \
                if self._world._guard else None
            if source_refusal:
                entry["verdict"] = "would_be_refused"
                entry["detail"] = source_refusal
                return entry
            source_key = _path_key(str(source))
            content, content_from = self._current(source_key, str(source), world)
            if content_from == "missing":
                entry["verdict"] = "would_be_refused"
                entry["detail"] = f"{source} does not exist, so there is nothing to copy"
                return entry
            if tool == "move_file":
                world[source_key] = ("", "shadowed")
                entry["source_removed"] = str(source)
            entry.update(verdict="would_run", effect=tool.split("_")[0],
                         before=before[:MAX_PREVIEW_BYTES],
                         after=content[:MAX_PREVIEW_BYTES],
                         source_is_shadowed=content_from == "shadowed")
            world[key] = (content, "shadowed")
            return entry

        # write_file
        content = str(params.get("content") or "")
        mode = str(params.get("mode") or "overwrite")
        after = before + content if mode == "append" else content
        entry.update(verdict="would_run", effect="write",
                     before=before[:MAX_PREVIEW_BYTES], after=after[:MAX_PREVIEW_BYTES],
                     truncates=(len(before) > MAX_PREVIEW_BYTES
                                or len(after) > MAX_PREVIEW_BYTES),
                     source_is_shadowed=before_from == "shadowed")
        world[key] = (after, "shadowed")
        return entry

    def _current(self, key: str, path_text: str,
                 world: Dict[str, tuple]) -> tuple:
        """The text this path holds, and where that text came from."""
        if key in world:
            return world[key]
        snap = snapshot(Path(path_text).expanduser())
        if not snap.exists:
            return "", "missing"
        try:
            data = Path(path_text).expanduser().read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "", "unreadable"
        return data, "actual"

    # -- reading ------------------------------------------------------------
    def _reading(self, entry: Dict[str, Any], params: Dict[str, Any],
                 world: Dict[str, tuple]) -> Dict[str, Any]:
        path_text = str(params.get("path") or "")
        if not path_text:
            entry["verdict"] = "would_be_refused"
            entry["detail"] = "no path is set, so this step cannot run"
            return entry
        content, origin = self._current(_path_key(path_text), path_text, world)
        if origin == "missing":
            entry["verdict"] = "would_be_refused"
            entry["detail"] = f"{path_text} does not exist, so there is nothing to read"
            return entry
        digest = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:16]
        entry.update(
            # A read of a file an earlier shadowed step wrote is a prediction.
            verdict="predicted" if origin == "shadowed" else "would_run",
            effect="read", after=content[:MAX_PREVIEW_BYTES],
            detail=(f"{path_text} would be read ({len(content)} bytes, "
                    f"sha256:{digest})"
                    + ("; this is a prediction of what an earlier step in the plan "
                       "would have written, not what is on disk now"
                       if origin == "shadowed" else "")))
        return entry

    # -- everything else ----------------------------------------------------
    def _unsimulated(self, entry: Dict[str, Any], tool: str) -> Dict[str, Any]:
        entry["verdict"] = "not_simulated"
        entry["detail"] = (
            f"{tool} acts outside this process, so a shadow cannot observe what "
            "it would do. It is not previewed and its effect is unknown until "
            "it runs.")
        return entry


def _conclusion(outcomes: List[Dict[str, Any]]) -> str:
    """One sentence stating what this shadow can and cannot tell you."""
    total = len(outcomes)
    if total == 0:
        return "There are no steps to shadow."

    # A step stopped at the gate is not previewed at all, so it must not be
    # counted among the steps whose effects were simulated -- that would claim a
    # preview that never happened.
    refused = sum(1 for o in outcomes if o["verdict"] == "would_be_refused")
    approving = sum(1 for o in outcomes if o["verdict"] == "would_need_approval")
    unknown = sum(1 for o in outcomes if o["verdict"] == "not_simulated")
    previewed = sum(1 for o in outcomes
                    if o["verdict"] in ("would_run", "predicted"))

    if refused:
        return (f"{refused} of {total} step(s) would be refused as things stand, "
                "so the plan would stop before finishing.")
    if unknown == total:
        return ("None of these steps can be simulated, so this shadow tells you "
                "what the permission engine would allow and nothing about effects.")
    if previewed == 0:
        return (f"No step's effect could be previewed: {approving} step(s) need "
                "your approval first and "
                f"{unknown} act outside Vantage, so what they would change is "
                "unknown until they run.")
    if unknown or approving:
        parts = [f"{previewed} of {total} step(s) can be previewed"]
        if approving:
            parts.append(f"{approving} need your approval first")
        if unknown:
            parts.append(f"{unknown} act outside Vantage, so their effects are "
                         "unknown until they run")
        return ", ".join(parts) + "."
    return "Every step would run, and the file effects below are what would change."