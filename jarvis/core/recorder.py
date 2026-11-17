"""Demonstrate-to-Automate: turn a demonstration into a reviewable workflow.

The product claim is not "we record your clicks". It is that a demonstration
becomes a *semantic* workflow a person can read, edit, authorize and re-run
through the same safe runtime as any other work.

Design decisions that make this honest:

* **You record explicitly and stop explicitly.** A recording is a session with a
  beginning and an end; nothing is captured implicitly, and the in-progress
  session is inspectable while it is open.
* **Steps are typed actions, not screen coordinates.** A recorded step names a
  tool and parameters -- the same shape a mission step has -- so it can be
  replayed by the mission runner, gated by the permission engine, and verified.
  A pixel coordinate never reaches the model.
* **Sensitive values are refused at capture time.** A step whose parameter looks
  like a secret (a password field, a token, a card number) is *not recorded*;
  the capture records that a sensitive step happened and stops. Silent capture
  of a credential is the failure mode this exists to prevent.
* **A recording is not a workflow until a person reviews it,** and a workflow is
  not verified until a replay actually succeeded. ``verdict`` starts at
  ``unverified`` and only a real run moves it.
* **Capture can be deleted.** A discarded recording leaves nothing behind.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger

log = get_logger("core.recorder")

#: Parameter names whose values must never be captured. A match refuses the
#: whole step rather than redacting: a step that recorded a password with the
#: password removed would replay against the wrong field.
_SENSITIVE_HINTS = (
    "password", "passwd", "secret", "token", "api_key", "apikey", "otp",
    "one_time", "card_number", "cvv", "ssn", "social_security",
)

#: The action kinds a demonstration may produce. These are the *registered tool
#: names*, not descriptive labels: a draft's step is dispatched by name through
#: the same agent registry a direct request uses, so a kind with no tool behind
#: it produces a draft whose first replay fails with "no agent owns ...".
#:
#: An earlier version listed `append_file`, `replace_text`, `open_app`,
#: `run_command` and `http_request`. None of those is a tool: appending is
#: `write_file` with `mode="append"`, opening is `open_application`, and running
#: a command is `run_allowlisted_command`. `http_request` and `replace_text`
#: have no tool at all and were removed rather than left to fail at replay.
#: `tests/test_recorder.py` pins every kind here to a registered tool.
SUPPORTED_KINDS = (
    "write_file", "read_file", "delete_file",
    "open_application", "open_url", "run_allowlisted_command",
    "move_file", "copy_file",
    # Real desktop actions (V15 §15). A recording that demonstrates pointer work
    # has to be able to replay it, so the actuation tools a person actually uses
    # are recordable. Each still goes through the permission engine and its own
    # confirmation on replay: being recordable is not being pre-approved.
    "desktop_click", "desktop_double_click", "desktop_right_click",
    "desktop_move_pointer", "desktop_drag", "desktop_scroll",
    "desktop_type_text", "desktop_press_key", "desktop_shortcut",
    "desktop_focus_window", "desktop_press_element",
)

#: The recorded kinds whose tool passes a window expectation through to the
#: executor's staleness guard. A recording stores absolute screen points, so a
#: replay after the target window moved aims at the old place; the executor
#: refuses when the window it was aimed at is gone or has moved -- but only the
#: click, drag and scroll tools accept ``expect_window_id``. The other coordinate
#: kinds take the same points without a guard, and recording a window id for them
#: would only be a parameter their tool rejects.
WINDOW_GUARDED_KINDS = ("desktop_click", "desktop_drag", "desktop_scroll")

#: The parameters each supported tool requires, mirroring the registered
#: ``ToolSpec.required_parameters`` for exactly the kinds above.
#:
#: Not imported from the registry on purpose: ``jarvis/registry/tools`` builds
#: the whole tool catalog, and the recorder must stay importable without one
#: (``tests/test_recorder.py`` constructs it standalone). The mirror is pinned
#: to the registry by ``test_the_recorder_knows_every_required_parameter_the_registry_does``,
#: so a tool that grows a required parameter fails that test rather than
#: silently producing drafts that cannot replay.
#:
#: Optional parameters with a tool-side default are absent on purpose.
#: ``write_file``'s ``mode`` defaults to ``overwrite`` when it is not supplied,
#: so a recording that never set it is complete and must not be reported as
#: needing input.
REQUIRED_PARAMETERS: Dict[str, tuple] = {
    "write_file": ("path", "content"),
    "read_file": ("path",),
    "delete_file": ("path",),
    "open_application": ("app",),
    "open_url": ("url",),
    "run_allowlisted_command": ("command",),
    "move_file": ("source", "destination"),
    "copy_file": ("source", "destination"),
    # Mirrors the registry's required parameters for the desktop tools added
    # above; ``test_the_recorder_knows_every_required_parameter_the_registry_does``
    # fails if either side moves without the other.
    "desktop_click": ("x", "y"),
    "desktop_double_click": ("x", "y"),
    "desktop_right_click": ("x", "y"),
    "desktop_move_pointer": ("x", "y"),
    "desktop_drag": ("from_x", "from_y", "to_x", "to_y"),
    "desktop_scroll": ("x", "y"),
    "desktop_type_text": ("text",),
    "desktop_press_key": ("key",),
    "desktop_shortcut": ("key", "modifiers"),
    "desktop_focus_window": (),
    "desktop_press_element": ("keyword",),
}


def missing_parameters(kind: str, params: Dict[str, Any]) -> List[str]:
    """The required parameters ``params`` does not carry a value for.

    An unknown kind reports nothing missing: whether the tool exists at all is a
    different question, already enforced by ``SUPPORTED_KINDS`` and by the test
    that pins every kind to a registered tool and an owning agent.
    """
    required = REQUIRED_PARAMETERS.get(kind, ())
    return [name for name in required
            if name not in params or params[name] in (None, "")]


def _missing_reason(missing: List[str]) -> str:
    names = ", ".join(missing)
    return (f"{names} {'was' if len(missing) == 1 else 'were'} never captured; "
            f"supply {'it' if len(missing) == 1 else 'them'} before running")


def _window_id_of(value: Any) -> Optional[int]:
    """A usable window id, or None.

    Window ids are opaque positive integers from the window server. A value read
    back from a stored recording is untrusted by the time it is used again, and
    one that is not an integer must become None -- an unguarded replay -- rather
    than reach the executor as a failed conversion, which would report a broken
    step instead of the honest "this recording predates the guard".
    """
    try:
        window_id = int(value)
    except (TypeError, ValueError):
        return None
    return window_id if window_id > 0 else None


def target_window_id() -> Optional[int]:
    """The window a coordinate action would land in right now, or None.

    Asked through the executor's own window list, which is the same source the
    staleness guard compares against on replay: resolving the window from
    anything else would make capture and check disagree about what "the window"
    is. The window server orders that list front-to-back, so the first layer-0
    window is the one under the pointer's own plane -- the one a person
    demonstrating an action is almost always aiming at.

    Returning None for every failure is deliberate. Capture must not depend on
    screen-recording being granted, the executor being built, or the window list
    answering: a recorder that refused to record because it could not name the
    window would lose the demonstration entirely, and an unguarded recording is
    still the honest record of what the person did.

    Only the integer id is returned. A window *title* is free text from whatever
    the person has open and can carry a document name, a subject line or worse;
    it never enters a recording.
    """
    try:
        from ..desktop import executor

        result = executor.windows("")
    except Exception as exc:  # a diagnostic must never fail a recording
        log.info("window identity unavailable", extra_fields={"error": str(exc)})
        return None
    if str(result.get("status") or "") != "verified":
        return None
    windows = result.get("windows")
    if not isinstance(windows, list):
        return None
    for window in windows:
        if not isinstance(window, dict):
            continue
        # Layers above 0 are menus, palettes, tooltips and overlays: real windows
        # in a different plane, never what a click at a document coordinate was
        # aimed at. They sort first when a menu is open, which is exactly when a
        # recording is likely to be in progress.
        if int(window.get("layer") or 0) != 0:
            continue
        if window.get("on_screen") is False:
            continue
        try:
            window_id = int(window.get("window_id"))
        except (TypeError, ValueError):
            continue
        if window_id > 0:
            return window_id
    return None


def _with_window_guard(kind: str, params: Dict[str, Any],
                       window_id: Optional[int]) -> Dict[str, Any]:
    """``params`` plus the executor's window expectation, when there is one.

    Only the kinds whose tool forwards ``expect_window_id`` get it. Handing it
    to any other tool would be an unexpected parameter that the permission
    engine refuses, which is a step that will not run at all -- worse than a
    step that runs without a guard.

    A recorded point is only meaningful for the window it was demonstrated in.
    Without this the guard has nothing to compare and a replay clicks wherever
    the old coordinates now land: measured, a moved window took the click, the
    counter did not change, and every step still reported done.
    """
    guarded = dict(params or {})
    if window_id is not None and kind in WINDOW_GUARDED_KINDS:
        guarded["expect_window_id"] = int(window_id)
    return guarded


def _acceptance_for(kind: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The condition a step's own parameters claim it will establish, or None.

    A demonstration is evidence of intent, not of outcome, so a condition is
    only proposed where the step's parameters *name* something a later observer
    can re-read from disk. Nothing here trusts the executor: each condition is
    checked after the fact by a verifier that reads state, never a return value.

    Only three kinds have an honest observer available:

    * ``write_file`` -- after the write, the path contains the content.
    * ``copy_file`` -- after the copy, the destination holds the source's exact
      bytes. ``file_hash`` needs the source's hash at *verification* time, and a
      step that changed the source between plan and check would produce a
      spurious failure, so the condition is on content rather than a stored hash.
    * ``delete_file`` -- after the delete, the path is gone.

    ``read_file``, ``open_url`` and ``open_application`` observe nothing that
    persists, and ``move_file``'s destination content depends on a source that
    may itself have moved. Those steps run and are reported as run; no condition
    is invented for them. A recorded automation whose steps are all of those
    kinds therefore never reaches ``verified`` -- which is the honest verdict,
    and the reason this function returns None instead of a plausible-looking
    condition nobody can check.
    """
    path = str(params.get("path") or "")
    if kind == "write_file" and path:
        content = str(params.get("content") or "")
        # Empty content would make `needle in text` vacuously true for any file,
        # so it establishes nothing and is not offered as a condition.
        if not content:
            return None
        # Holds for both modes: an overwrite leaves exactly this content, and an
        # append leaves the earlier content followed by it.
        return {"kind": "file_contains", "path": path, "needle": content,
                "statement": f"{path} exists and contains the recorded content"}
    if kind == "copy_file":
        destination = str(params.get("destination") or "")
        source = str(params.get("source") or "")
        if destination and source:
            return {"kind": "file_identical", "path": destination, "source": source,
                    "statement": f"{destination} holds exactly the contents of {source}"}
    if kind == "delete_file" and path:
        return {"kind": "file_absent", "path": path,
                "statement": f"{path} no longer exists"}
    return None


def _apply_review(steps: List[Dict[str, Any]],
                  reviewed: List[Dict[str, Any]]) -> tuple:
    """Apply the values a person supplied, then re-derive what can be checked.

    The review is the step list the editor holds: same steps, in the order the
    editor now shows them, with the values it holds. Order is honoured because
    the editor offers reordering, and a reordered workflow runs in that order.

    Parameters the review does not mention keep their recorded value, so a
    client that sends only corrections does not erase the demonstration.
    Conditions are rebuilt from the merged parameters rather than carried over:
    a condition describes a value, and the value may have changed. A step whose
    parameters no longer support any checkable outcome loses its claim instead of
    keeping one that no longer matches what will run.

    A malformed review is refused rather than partially applied -- guessing at
    which steps the person meant is how a review screen silently edits a
    workflow the person did not edit.
    """
    if not isinstance(reviewed, list) or len(reviewed) != len(steps):
        raise RecordingError(
            f"the review has {len(reviewed) if isinstance(reviewed, list) else 'no'} "
            f"step(s); the recording has {len(steps)}. A review must cover every "
            "step, in the order it should run.")

    merged: List[Dict[str, Any]] = []
    for index, supplied in enumerate(reviewed):
        # Each reviewed entry names which recorded step it is editing, by its
        # position in the recording. Matching on values instead would make two
        # identical-looking steps indistinguishable, and zipping by position
        # would make a reorder silently swap one step's parameters onto another
        # step's title and condition.
        if not isinstance(supplied, dict):
            raise RecordingError(f"reviewed step {index + 1} is not an object")
        try:
            source = int(supplied.get("index"))
        except (TypeError, ValueError):
            raise RecordingError(
                f"reviewed step {index + 1} does not say which recorded step it "
                "is; the editor sends each step's original position as 'index'")
        if not 0 <= source < len(steps):
            raise RecordingError(
                f"reviewed step {index + 1} names recorded step {source}, which "
                f"does not exist (the recording has {len(steps)})")
        original = steps[source]

        if str(supplied.get("tool") or original["tool"]) != original["tool"]:
            raise RecordingError(
                f"reviewed step {index + 1} changes the action from "
                f"{original['tool']!r} to {supplied.get('tool')!r}; a review may "
                "supply values and order, not replace what was demonstrated")
        params = dict(original.get("params") or {})
        for name, value in (supplied.get("params") or {}).items():
            params[str(name)] = "" if value is None else str(value)
        # The original already carries the window expectation `derive` added, and
        # a review edits values rather than aims. It is re-applied so an editor
        # that sends back only the fields it displays cannot drop the guard.
        params = _with_window_guard(original["tool"], params,
                                    _window_id_of(params.get("expect_window_id")))
        entry: Dict[str, Any] = {"title": str(supplied.get("title") or original["title"]),
                                 "tool": original["tool"], "params": params}
        missing = missing_parameters(original["tool"], params)
        if missing:
            entry["needs_input"] = _missing_reason(missing)
        merged.append(entry)

    seen = [int(s["index"]) for s in reviewed]
    if sorted(seen) != list(range(len(steps))):
        raise RecordingError(
            "a review must name every recorded step exactly once; it named "
            f"{sorted(seen)} of {len(steps)}")

    conditions: List[Dict[str, Any]] = []
    for entry in merged:
        if "needs_input" in entry:
            continue
        acceptance = _acceptance_for(entry["tool"], entry["params"])
        if acceptance is None:
            continue
        condition_id = f"C{len(conditions) + 1}"
        conditions.append({"condition_id": condition_id,
                           "statement": acceptance["statement"],
                           "verifier": {k: v for k, v in acceptance.items()
                                        if k != "statement"}})
        entry["satisfies"] = condition_id
    return merged, conditions


class RecordingError(ValueError):
    """A recording or a step could not be accepted; the message says why."""


@dataclass
class RecordedStep:
    index: int
    kind: str
    params: Dict[str, Any] = field(default_factory=dict)
    label: str = ""
    at: float = 0.0
    #: True when the step was refused at capture time, so a reviewer sees that
    #: something happened there without the value being kept.
    skipped: bool = False
    skip_reason: str = ""
    #: The window the action was demonstrated in, when one could be resolved.
    #: Kept beside the params rather than in them so it is not mistaken for a
    #: value the person supplied, and `derive` decides which kinds forward it to
    #: their tool. None is a complete recording: it just replays unguarded,
    #: which is what every recording made before this existed does.
    window_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index, "kind": self.kind, "params": dict(self.params),
            "label": self.label, "at": self.at, "window_id": self.window_id,
            "skipped": self.skipped, "skip_reason": self.skip_reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RecordedStep":
        """Rebuild a step from what a recording stored.

        A step written before window ids existed has no ``window_id`` key, and
        must load as one that simply replays unguarded -- refusing the whole
        recording would throw away a demonstration that is still perfectly
        runnable, just without the staleness check.
        """
        return cls(
            index=int(data.get("index") or 0),
            kind=str(data.get("kind") or ""),
            params=dict(data.get("params") or {}),
            label=str(data.get("label") or ""),
            at=float(data.get("at") or 0.0),
            skipped=bool(data.get("skipped")),
            skip_reason=str(data.get("skip_reason") or ""),
            window_id=_window_id_of(data.get("window_id")),
        )


@dataclass
class Recording:
    """One explicit recording session."""

    recording_id: str
    name: str = ""
    started_at: float = 0.0
    stopped_at: float = 0.0
    steps: List[RecordedStep] = field(default_factory=list)
    #: A recording is only "stopped" once the person ended it.
    open: bool = True

    @property
    def duration(self) -> float:
        end = self.stopped_at or time.time()
        return max(0.0, end - self.started_at)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "recording_id": self.recording_id,
            "name": self.name,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "open": self.open,
            "duration_s": self.duration,
            "steps": [s.to_dict() for s in self.steps],
            "replayable": [s.to_dict() for s in self.steps if not s.skipped],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Recording":
        """Rebuild a recording that ``to_dict`` produced.

        ``id`` rather than ``recording_id``: a client may round-trip either
        spelling, and a recording that loaded with an empty id would be one no
        later request could name.
        """
        return cls(
            recording_id=str(data.get("recording_id") or data.get("id") or ""),
            name=str(data.get("name") or ""),
            started_at=float(data.get("started_at") or 0.0),
            stopped_at=float(data.get("stopped_at") or 0.0),
            steps=[RecordedStep.from_dict(s) for s in (data.get("steps") or [])
                   if isinstance(s, dict)],
            open=bool(data.get("open")),
        )


#: Parameters whose *name* says nothing about sensitivity but whose *value* is
#: typed into whatever is focused. For these, the value itself is inspected --
#: `desktop_type_text(text="password=hunter2")` was accepted by the name-only
#: check, which inspected "text" and found it innocent.
_VALUE_IS_TYPED = frozenset({"text", "content", "value"})


def _looks_sensitive(params: Dict[str, Any]) -> Optional[str]:
    """The name of a sensitive parameter, or None.

    Only the *name* is reported back: a sensitive value must not reach a log, a
    file, or an error message.

    A typed parameter is the exception to name-only inspection. When the value
    is going to be sent as keystrokes, what it contains matters as much as what
    the field is called: a recording that stored `text="password=hunter2"` would
    replay the password on every run, and it would sit in the draft in plain
    text. The value is *inspected* here and never included in the result.
    """
    for key, value in params.items():
        name = str(key).lower()
        if any(hint in name for hint in _SENSITIVE_HINTS):
            return str(key)
        if name in _VALUE_IS_TYPED and isinstance(value, str):
            lowered = value.lower()
            # `key=value` and `key: value` are the shapes a secret is pasted in.
            # A bare word match would refuse "the token expired" as a note, and
            # that is a document about a problem, not a credential.
            for hint in _SENSITIVE_HINTS:
                if f"{hint}=" in lowered or f"{hint}:" in lowered \
                        or f"{hint} =" in lowered or f"{hint} :" in lowered:
                    return str(key)
    return None


class Recorder:
    """Collects a demonstration and derives a reviewable workflow from it."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path else _default_path()
        self._recording: Optional[Recording] = None

    # -- session ------------------------------------------------------------
    def start(self, name: str = "") -> Recording:
        """Begin an explicit recording. Only one may be open at a time."""
        if self._recording is not None and self._recording.open:
            raise RecordingError(
                "a recording is already open; stop it before starting another")
        from .models import new_id

        self._recording = Recording(recording_id=new_id("REC"),
                                    name=str(name or ""), started_at=time.time())
        log.info("recording started", extra_fields={"recording_id": self._recording.recording_id})
        return self._recording

    def capture(self, kind: str, params: Dict[str, Any], *, label: str = "",
                window_id: Optional[int] = None) -> RecordedStep:
        """Record one semantic action.

        A sensitive parameter is refused: the step is kept as a `skipped`
        placeholder so a reviewer knows something happened there, but the value
        is never stored.

        A coordinate-driven action also records which window it was aimed at, so
        the executor's staleness guard can refuse a replay after that window has
        moved. The id is resolved once per step, here, because the moment the
        person acts is the only moment at which "the window they meant" is
        knowable; a caller that already resolved one passes it in rather than
        paying for a second window-list read that could disagree with the first.
        """
        recording = self._open_recording()
        if kind not in SUPPORTED_KINDS:
            raise RecordingError(
                f"{kind!r} is not an action this recorder can replay; "
                f"known kinds are {', '.join(SUPPORTED_KINDS)}")

        sensitive = _looks_sensitive(params or {})
        if sensitive is not None:
            step = RecordedStep(
                index=len(recording.steps), kind=kind, params={}, label=label,
                at=time.time(), skipped=True,
                skip_reason=(f"a sensitive value ({sensitive}) was not captured; "
                             "replay will need it supplied at run time"))
            recording.steps.append(step)
            log.info("recording skipped a sensitive step",
                     extra_fields={"recording_id": recording.recording_id,
                                   "parameter": sensitive})
            return step

        if window_id is None and kind in WINDOW_GUARDED_KINDS:
            window_id = target_window_id()

        step = RecordedStep(index=len(recording.steps), kind=kind,
                            params=dict(params or {}), label=label or kind,
                            at=time.time(), window_id=_window_id_of(window_id))
        recording.steps.append(step)
        return step

    def stop(self) -> Recording:
        recording = self._open_recording()
        recording.open = False
        recording.stopped_at = time.time()
        log.info("recording stopped", extra_fields={
            "recording_id": recording.recording_id, "steps": len(recording.steps)})
        return recording

    def current(self) -> Optional[Recording]:
        """The open recording, for a UI that shows capture in progress."""
        return self._recording

    def discard(self) -> bool:
        """Forget the current recording entirely. Nothing is persisted."""
        if self._recording is None:
            return False
        self._recording = None
        return True

    def _open_recording(self) -> Recording:
        if self._recording is None:
            raise RecordingError("no recording is open; start one first")
        if not self._recording.open:
            raise RecordingError("the recording has stopped; start a new one")
        return self._recording

    # -- deriving a workflow ------------------------------------------------
    def derive(self, recording: Optional[Recording] = None,
               *, reviewed: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Turn a stopped recording into a workflow draft a person can review.

        Every step becomes a mission-shaped step. Required capabilities are
        derived from the kinds actually captured -- not from what the person
        hoped to do -- and the resource scope is the set of paths the
        demonstration touched.

        A step is only reported as complete when it carries every parameter its
        tool declares. The recorder used to count only *skipped* steps as
        needing input, so a `copy_file` captured with a `source` and no
        `destination` produced a draft that said "0 needing input" and failed at
        replay with "missing required parameter 'destination'". A draft that
        reviews as complete and cannot run is worse than one that asks for the
        value, so the missing parameters are named here and the step is counted.

        ``reviewed`` replaces the recorded values with what a person supplied in
        the editor, in capture order. It exists because a value filled in after
        capture can *create* a checkable outcome -- a `copy_file` gains a
        `destination` the demonstration never had, and only then is there
        something to verify afterwards. Re-deriving from the raw recording left
        such a step with no condition at all, so the run reported the outcome as
        unverified no matter what the copy actually did.
        """
        rec = recording or self._recording
        if rec is None:
            raise RecordingError("there is no recording to derive a workflow from")
        if rec.open:
            raise RecordingError(
                "the recording is still open; stop it before deriving a workflow")

        steps = []
        capabilities = set()
        scope = set()
        conditions: List[Dict[str, Any]] = []
        skipped = 0
        incomplete = 0
        for step in rec.steps:
            if step.skipped:
                skipped += 1
                incomplete += 1
                steps.append({"title": f"[needs input] {step.label}",
                              "tool": step.kind, "params": {},
                              "needs_input": step.skip_reason})
                continue
            capabilities.add(step.kind)
            for key in ("path", "source", "target", "destination"):
                value = (step.params or {}).get(key)
                if isinstance(value, str) and value:
                    scope.add(value)
            missing = missing_parameters(step.kind, step.params or {})
            if missing:
                incomplete += 1
                steps.append({"title": step.label or step.kind,
                              "tool": step.kind,
                              "params": dict(step.params or {}),
                              "needs_input": _missing_reason(missing)})
                continue

            entry: Dict[str, Any] = {"title": step.label or step.kind,
                                     "tool": step.kind,
                                     "params": _with_window_guard(
                                         step.kind, step.params, step.window_id)}
            acceptance = _acceptance_for(step.kind, step.params or {})
            if acceptance is not None:
                condition_id = f"C{len(conditions) + 1}"
                conditions.append({"condition_id": condition_id,
                                   "statement": acceptance["statement"],
                                   "verifier": {k: v for k, v in acceptance.items()
                                                if k != "statement"}})
                entry["satisfies"] = condition_id
            steps.append(entry)

        if reviewed is not None:
            steps, conditions = _apply_review(steps, reviewed)
            incomplete = sum(1 for s in steps if s.get("needs_input"))

        return {
            "recording_id": rec.recording_id,
            "name": rec.name or "Demonstrated workflow",
            "steps": steps,
            "conditions": conditions,
            "required_capabilities": sorted(capabilities),
            "required_scope": sorted(scope),
            "needs_input": incomplete,
            # A demonstration proves what was done, not that doing it again
            # works. Verification only comes from a replay.
            "verdict": "unverified",
            "review_required": True,
            "notes": (
                f"{len(steps)} step(s) captured, {incomplete} needing input, "
                f"{len(conditions)} checkable outcome(s). "
                "Review before saving: this draft is what you demonstrated, not "
                "a verified automation."),
        }


def _default_path() -> Path:
    from ..config import data_home

    return data_home() / "recordings.json"

#: The one recorder the API uses. A demonstration is a single live session, so
#: two request handlers must not each hold their own capture buffer.
_RECORDER: Optional[Recorder] = None


def _recorder_singleton() -> Recorder:
    global _RECORDER
    if _RECORDER is None:
        _RECORDER = Recorder()
    return _RECORDER
