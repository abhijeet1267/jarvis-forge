"""Adaptive Autonomy Control: one dial, with a floor it cannot go below.

The permission engine already answers *may this run*. It gets that answer from a
fixed policy: level 2+ always confirms, ``paranoid`` promotes everything to a
confirmation. That is a setting, not a dial -- you cannot say "let this project's
routine file moves run without asking me for an afternoon" and have it mean
anything narrower than "turn paranoid off everywhere".

This is the dial. It sits **between** the risk classifier and the final verdict
and can only ever make a decision *stricter*, never looser:

    autonomy = 0   ask before everything (paranoid)
    autonomy = 1   ask before anything above a pure read
    autonomy = 2   ask before anything above a low-risk action
    autonomy = 3   the tool's own tier decides (today's default behaviour)
    autonomy = 4   a standing grant may cover a low-risk repeat (today's grant rule)

Two hard rules make it an autonomy control and not a "yes to everything" switch:

* **It never raises a verdict.** The floor of a tool's declared risk is
  ``EXECUTE``; the dial can lift that to ``CONFIRM`` or ``BLOCK`` but it is asked
  *after* the classifier and can only return a stricter of the two. A dial set
  to 4 on a destructive tool still confirms, because the classifier did and
  nothing here may lower it.
* **Destructive and physical actions have a ceiling.** Above
  ``CEILING_RISK`` no autonomy level auto-executes, ever -- not 4, not a value a
  future caller invents. A higher level is not a licence; it is a request that
  the floor stays where the policy put it.

A scope narrows where the dial applies: "4, but only under this prefix" leaves
everything outside the prefix on the default. The scope is a resource prefix,
matched the same way ``IntentScope`` matches one, so the two cannot disagree
about what "under this directory" means.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.models import PolicyDecision, RiskLevel, ToolSpec
from ..logging_setup import get_logger

log = get_logger("security.autonomy")

#: The levels a dial may hold. The names are what a screen shows; the numbers are
#: what the decision function compares.
LEVELS: Dict[int, str] = {
    0: "Ask before everything",
    1: "Ask before anything above a read",
    2: "Ask before anything above a low-risk action",
    3: "Ask when the action's own risk says to",
    4: "Let a standing grant cover a low-risk repeat",
}

DEFAULT_LEVEL = 3

#: Above this tier, no autonomy level auto-executes. Destructive and physical
#: effects are the two categories where "I trusted it once" is not a reason to
#: stop asking, and this is where that rule lives rather than in a comment.
CEILING_RISK = RiskLevel.DESTRUCTIVE

#: The one file that holds the dial, beside every other small store.
SETTINGS_FILE_NAME = "autonomy.json"


class AutonomyError(ValueError):
    """The requested level or scope is not one this module will hold."""


@dataclass
class AutonomySetting:
    """The dial's position and the scope it applies to."""

    level: int = DEFAULT_LEVEL
    #: When set, the level applies only to resources under this prefix.
    #: Empty means "everywhere".
    scope: str = ""
    note: str = ""
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {"level": self.level, "level_name": LEVELS.get(self.level, "unknown"),
                "scope": self.scope, "note": self.note,
                "updated_at": self.updated_at,
                "ceiling_risk": int(CEILING_RISK)}


def _default_path() -> Path:
    from ..config import data_home

    return data_home() / SETTINGS_FILE_NAME


def _resource_values(params: Dict[str, Any]) -> List[str]:
    """Every path-like value in a tool's params, for scope matching."""
    from .intentshield import RESOURCE_PARAMETER_KEYS

    values: List[str] = []
    for key in RESOURCE_PARAMETER_KEYS:
        raw = params.get(key)
        if isinstance(raw, str) and raw.strip():
            values.append(raw.strip())
    return values


def _scope_matches(scope: str, params: Dict[str, Any]) -> bool:
    """Whether an action falls under ``scope``.

    An empty scope applies everywhere. A non-empty scope applies when **every**
    path-like value the call names is under the prefix, and when the call names
    at least one. That "every" is deliberate: a copy that reads under the scope
    but writes outside it is not a scoped action, it is an escape, and treating
    it as scoped would let one parameter widen the dial.
    """
    if not scope:
        return True
    values = _resource_values(params)
    if not values:
        return False
    prefix = os.path.expanduser(scope.rstrip("/")) + "/"
    for value in values:
        path = os.path.expanduser(value)
        if not (path == prefix.rstrip("/") or path.startswith(prefix)):
            return False
    return True


def _stricter(a: PolicyDecision, b: PolicyDecision) -> PolicyDecision:
    """The more restrictive of two decisions. BLOCK > CONFIRM > EXECUTE."""
    order = {PolicyDecision.EXECUTE: 0, PolicyDecision.CONFIRM: 1, PolicyDecision.BLOCK: 2}
    return a if order.get(a, 0) >= order.get(b, 0) else b


class AutonomyControl:
    """The dial, plus the rule that applies it to one decision.

    ``apply`` is pure: given the classifier's verdict, the tool's spec, the
    params and the setting, it returns the verdict that actually stands and a
    sentence saying why. Storage lives on the instance only so the dial survives
    a page refresh; the decision never reads the file on the hot path.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or _default_path()
        self._lock = threading.RLock()
        self._setting = self._load()

    # -- storage -------------------------------------------------------------
    def _load(self) -> AutonomySetting:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return AutonomySetting()
        except (OSError, ValueError) as exc:
            # A corrupt dial file must not silently become "default". Default is
            # 3, which asks exactly what it asked yesterday, so returning it is
            # safe -- but say so, because the person's saved level was lost.
            log.info("autonomy setting unreadable", extra_fields={"error": str(exc)})
            return AutonomySetting()
        level = int(data.get("level", DEFAULT_LEVEL))
        if level not in LEVELS:
            level = DEFAULT_LEVEL
        return AutonomySetting(level=level, scope=str(data.get("scope") or ""),
                               note=str(data.get("note") or ""),
                               updated_at=float(data.get("updated_at") or 0.0))

    def _save(self) -> None:
        payload = asdict(self._setting)
        payload["updated_at"] = time.time()
        self._setting.updated_at = payload["updated_at"]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:  # pragma: no cover - disk failure
            log.info("autonomy setting not saved", extra_fields={"error": str(exc)})

    # -- reads ---------------------------------------------------------------
    @property
    def setting(self) -> AutonomySetting:
        return self._setting

    def describe(self) -> Dict[str, Any]:
        current = self._setting.to_dict()
        current["levels"] = [{"level": n, "name": name} for n, name in sorted(LEVELS.items())]
        current["ceiling"] = (
            f"At any level, a {CEILING_RISK.name.lower()} or physical action is never "
            "auto-executed: the dial can only make a decision stricter."
        )
        return current

    def set(self, *, level: int, scope: str = "", note: str = "") -> AutonomySetting:
        """Move the dial. Refuses a level this module does not define."""
        if level not in LEVELS:
            raise AutonomyError(f"autonomy level {level} is not one of {sorted(LEVELS)}")
        with self._lock:
            self._setting = AutonomySetting(level=level, scope=scope.strip(),
                                            note=note.strip(), updated_at=time.time())
            self._save()
            log.info("autonomy set",
                     extra_fields={"level": level, "scope": scope})
            return self._setting

    # -- the decision --------------------------------------------------------
    def apply(self, spec: ToolSpec, params: Dict[str, Any],
              decision: PolicyDecision, reason: str) -> tuple[PolicyDecision, str]:
        """The verdict that stands, given the dial. Never lowers ``decision``."""
        setting = self._setting
        if not _scope_matches(setting.scope, params):
            # Outside the dial's scope the ordinary policy decides, unchanged.
            return decision, reason

        if spec.risk >= CEILING_RISK:
            # The ceiling. Even the loosest level does not auto-execute a
            # destructive or physical action; it stands exactly as classified.
            return decision, reason

        level = setting.level
        if level <= 0:
            proposed = PolicyDecision.CONFIRM
            why = "autonomy is set to ask before everything"
        elif level == 1:
            proposed = PolicyDecision.EXECUTE if spec.risk <= RiskLevel.READ else PolicyDecision.CONFIRM
            why = ("autonomy asks before anything above a read"
                   if proposed is PolicyDecision.CONFIRM else reason)
        elif level == 2:
            proposed = PolicyDecision.EXECUTE if spec.risk <= RiskLevel.LOW else PolicyDecision.CONFIRM
            why = ("autonomy asks before anything above a low-risk action"
                   if proposed is PolicyDecision.CONFIRM else reason)
        else:
            # Levels 3 and 4 leave the classifier's verdict alone. Level 4's
            # extra room is the standing-grant rule the permission engine
            # already applies; this dial does not duplicate it.
            proposed, why = decision, reason

        final = _stricter(decision, proposed)
        if final is decision:
            # The dial could not make it stricter, so it adds no reason.
            return decision, reason
        return final, why


_CONTROL: Optional[AutonomyControl] = None


def get_autonomy() -> AutonomyControl:
    global _CONTROL
    if _CONTROL is None:
        _CONTROL = AutonomyControl()
    return _CONTROL


def reset_autonomy() -> None:
    """Drop the process dial so the next call reloads from disk (tests only)."""
    global _CONTROL
    _CONTROL = None


def describe_autonomy() -> Dict[str, Any]:
    return get_autonomy().describe()