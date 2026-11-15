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
)


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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index, "kind": self.kind, "params": dict(self.params),
            "label": self.label, "at": self.at,
            "skipped": self.skipped, "skip_reason": self.skip_reason,
        }


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


def _looks_sensitive(params: Dict[str, Any]) -> Optional[str]:
    """The name of a sensitive parameter, or None.

    Only the *name* is inspected, and only the name is reported back: a
    sensitive value must not reach a log, a file, or an error message.
    """
    for key in params:
        lowered = str(key).lower()
        if any(hint in lowered for hint in _SENSITIVE_HINTS):
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

    def capture(self, kind: str, params: Dict[str, Any], *, label: str = "") -> RecordedStep:
        """Record one semantic action.

        A sensitive parameter is refused: the step is kept as a `skipped`
        placeholder so a reviewer knows something happened there, but the value
        is never stored.
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

        step = RecordedStep(index=len(recording.steps), kind=kind,
                            params=dict(params or {}), label=label or kind,
                            at=time.time())
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
    def derive(self, recording: Optional[Recording] = None) -> Dict[str, Any]:
        """Turn a stopped recording into a workflow draft a person can review.

        Every step becomes a mission-shaped step. Required capabilities are
        derived from the kinds actually captured -- not from what the person
        hoped to do -- and the resource scope is the set of paths the
        demonstration touched.
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
        skipped = 0
        for step in rec.steps:
            if step.skipped:
                skipped += 1
                steps.append({"title": f"[needs input] {step.label}",
                              "tool": step.kind, "params": {},
                              "needs_input": step.skip_reason})
                continue
            capabilities.add(step.kind)
            for key in ("path", "source", "target", "destination"):
                value = (step.params or {}).get(key)
                if isinstance(value, str) and value:
                    scope.add(value)
            steps.append({"title": step.label or step.kind,
                          "tool": step.kind,
                          "params": dict(step.params)})

        return {
            "recording_id": rec.recording_id,
            "name": rec.name or "Demonstrated workflow",
            "steps": steps,
            "required_capabilities": sorted(capabilities),
            "required_scope": sorted(scope),
            "needs_input": skipped,
            # A demonstration proves what was done, not that doing it again
            # works. Verification only comes from a replay.
            "verdict": "unverified",
            "review_required": True,
            "notes": (
                f"{len(steps)} step(s) captured, {skipped} needing input. "
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
