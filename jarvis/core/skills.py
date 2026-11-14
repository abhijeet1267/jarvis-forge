"""Skills: versioned, inspectable automations -- metadata only, never authority.

A skill is a workflow a person demonstrated once ("write the weekly report"),
saved so it can be inspected, re-run and tested. This module owns the *manifest*
and the *store*, and deliberately does nothing else:

* **A skill never grants itself permission.** The capabilities and scope in a
  manifest are *claims about what the skill needs*, checked against what is
  actually available by :func:`validate_skill`. Executing a skill goes through
  the normal mission lease + IntentShield path -- the same permission engine,
  scope checks and approval a chat action goes through. There is deliberately no
  ``execute()`` here: a manifest that could run tools would be a bypass, not a
  feature.
* **A skill is unverified until a test run says otherwise.** ``reliability``
  reports recorded counts of verified, failed and unknown runs, and returns
  ``success_rate=None`` when nothing has run. A demonstration that happened once
  is not reliability, so it is never counted as one.
* **An externally supplied manifest is untrusted input.** ``from_dict`` rejects
  unknown fields, missing required fields, a non-positive version and a risk
  outside 0..3, because a typo in a permission-relevant field must fail loudly
  rather than be silently ignored.

Persistence is one JSON document at ``<JARVIS_HOME>/skills.json`` (or the path
the caller passes), written whole and replaced atomically, mode 0600. It is
intentionally not a database table: a skill is a document a person edits and
shares, and its schema must not be coupled to the app's migrations. A corrupt
file raises instead of being treated as empty -- overwriting a damaged skills
file would destroy the only copy of someone's saved automation.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ..logging_setup import get_logger

log = get_logger("core.skills")

SKILLS_FILE_NAME = "skills.json"
SCHEMA_VERSION = 1
#: How many individual runs are kept for inspection. Counters are kept forever;
#: the per-run list is bounded so the file cannot grow without limit.
RECENT_RUNS = 20

#: Every field a manifest may carry. Anything else in an untrusted dict is a
#: rejection, not something to keep "just in case".
KNOWN_FIELDS: frozenset = frozenset({
    "skill_id", "version", "name", "description", "inputs", "outputs", "steps",
    "required_capabilities", "required_scope", "risk", "preconditions",
    "verification", "supported_platforms", "owner",
})
REQUIRED_FIELDS: tuple = ("skill_id", "name", "steps")

#: The shape of one step. `satisfies` joins a step to a verification condition.
STEP_FIELDS: frozenset = frozenset({"tool", "params", "satisfies"})


@dataclass
class SkillManifest:
    """What a skill claims it is and needs. It does not authorize anything.

    ``required_capabilities`` and ``required_scope`` are what the skill *asks
    for*; :func:`validate_skill` compares them with what the caller actually has
    available. The manifest itself is never consulted by the permission engine.
    """

    skill_id: str
    version: int = 1
    name: str = ""
    description: str = ""
    #: Input name -> a person-readable description of what it expects.
    inputs: Dict[str, str] = field(default_factory=dict)
    outputs: List[str] = field(default_factory=list)
    #: [{"tool": str, "params": dict, "satisfies": condition_id}, ...]
    steps: List[Dict[str, Any]] = field(default_factory=list)
    required_capabilities: List[str] = field(default_factory=list)
    #: Resource prefixes the skill expects to touch, e.g. ["/Users/me/reports"].
    required_scope: List[str] = field(default_factory=list)
    risk: int = 0
    preconditions: List[str] = field(default_factory=list)
    #: Declarative checks, e.g. {"condition_id": "C1", "kind": "file_contains",
    #: "path": "...", "needle": "..."}. The executor maps these to verifiers;
    #: this module never runs them and never treats a claimed check as proof.
    verification: List[Dict[str, Any]] = field(default_factory=list)
    supported_platforms: List[str] = field(default_factory=list)
    #: Who supplied this manifest ("user", "imported:foo", ...). Provenance is
    #: metadata, not a trust level.
    owner: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Emit exactly the known schema, with copies so callers cannot alias."""
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "name": self.name,
            "description": self.description,
            "inputs": dict(self.inputs),
            "outputs": list(self.outputs),
            "steps": [
                {
                    "tool": str(step.get("tool") or ""),
                    "params": dict(step.get("params") or {}),
                    "satisfies": str(step.get("satisfies") or ""),
                }
                for step in self.steps
            ],
            "required_capabilities": list(self.required_capabilities),
            "required_scope": list(self.required_scope),
            "risk": self.risk,
            "preconditions": list(self.preconditions),
            "verification": [dict(item) for item in self.verification],
            "supported_platforms": list(self.supported_platforms),
            "owner": self.owner,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "SkillManifest":
        """Parse and validate an untrusted manifest. Raises ``ValueError``.

        This is a security boundary: a manifest can arrive from a file, another
        machine, or a model. Unknown fields are refused rather than ignored,
        because an ignored field is a claim no one will check -- and the next
        version of this module might start honouring it.
        """
        if not isinstance(data, dict):
            raise ValueError(
                "a skill manifest must be a JSON object, got "
                f"{type(data).__name__}")

        unknown = sorted(set(data) - KNOWN_FIELDS)
        if unknown:
            raise ValueError(
                "a skill manifest has unknown field(s): "
                + ", ".join(unknown)
                + ". Known fields: " + ", ".join(sorted(KNOWN_FIELDS)) + ".")

        missing = [name for name in REQUIRED_FIELDS if name not in data]
        if missing:
            raise ValueError(
                "a skill manifest is missing required field(s): "
                + ", ".join(missing))

        return cls(
            skill_id=_required_text(data["skill_id"], "skill_id"),
            version=_positive_int(data.get("version", 1), "version"),
            name=_required_text(data["name"], "name"),
            description=_optional_text(data.get("description", ""), "description"),
            inputs=_input_map(data.get("inputs", {})),
            outputs=_string_list(data.get("outputs", []), "outputs"),
            steps=_parse_steps(data["steps"]),
            required_capabilities=_string_list(
                data.get("required_capabilities", []), "required_capabilities"),
            required_scope=_string_list(
                data.get("required_scope", []), "required_scope"),
            risk=_risk_int(data.get("risk", 0)),
            preconditions=_string_list(
                data.get("preconditions", []), "preconditions"),
            verification=_parse_verification(data.get("verification", [])),
            supported_platforms=_string_list(
                data.get("supported_platforms", []), "supported_platforms"),
            owner=_optional_text(data.get("owner", ""), "owner"),
        )


class SkillStoreError(ValueError):
    """The skills file exists but cannot be read or parsed.

    A ``ValueError`` subclass so a caller that already handles validation
    failures catches this too. It is raised instead of degrading to an empty
    store: silently overwriting a corrupt file loses every saved skill.
    """


# ---------------------------------------------------------------------------
# Persistence: one JSON document, read whole, replaced atomically
# ---------------------------------------------------------------------------

def _default_path() -> Path:
    """``<JARVIS_HOME>/skills.json``, the same home every other store uses."""
    root = os.environ.get("JARVIS_HOME") or str(Path.home() / ".jarvis-forge")
    return Path(root).expanduser() / SKILLS_FILE_NAME


class SkillStore:
    """Save, load, version, enable/disable and measure skills.

    The store holds metadata only. It has no method that executes a tool, and it
    must not grow one: execution belongs to the mission runner, where every step
    is gated by the permission engine and, when leases are in force, by a scoped
    lease the person granted.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else _default_path()
        # One process may serve the API and the dashboard at once; the lock
        # keeps two threads' read-modify-write cycles from interleaving. Cross
        # process writers are not serialized -- the atomic replace means a
        # reader sees one complete file or the other, never a torn one.
        self._lock = threading.Lock()

    # -- persistence --------------------------------------------------------
    def _read(self) -> Dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillStoreError(
                f"the skills file {self.path} could not be read: {exc}") from exc
        if not text.strip():
            return {}
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise SkillStoreError(
                f"the skills file {self.path} is not valid JSON: {exc}. "
                "It is left untouched; fix or move it before saving again.") from exc
        if not isinstance(data, dict):
            raise SkillStoreError(
                f"the skills file {self.path} must hold a JSON object")
        return data

    def _write(self, data: Dict[str, Any]) -> None:
        """Write the whole document to a temp file, chmod 0600, then replace.

        ``os.replace`` makes the swap atomic on one filesystem, so a crash mid
        write leaves the previous complete file rather than a truncated one.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".skills-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
            try:
                os.chmod(tmp_name, 0o600)
            except OSError:
                pass
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _entry(self, skill_id: str) -> Optional[Dict[str, Any]]:
        entry = _skills_block(self._read()).get(str(skill_id))
        return entry if isinstance(entry, dict) else None

    # -- writing ------------------------------------------------------------
    def save(self, manifest: SkillManifest) -> SkillManifest:
        """Store a manifest, assigning the next version when it already exists.

        The store is a boundary too: a ``SkillManifest`` built by hand can carry
        invalid values, so it is re-validated through ``from_dict`` before it is
        written. The returned manifest carries the version actually stored, which
        may differ from the caller's ``version`` on an update.
        """
        if not isinstance(manifest, SkillManifest):
            raise TypeError("save expects a SkillManifest")
        checked = SkillManifest.from_dict(manifest.to_dict())

        with self._lock:
            data = self._read()
            block = _skills_block(data)
            entry = block.get(checked.skill_id)
            if not isinstance(entry, dict):
                entry = {"disabled": False, "versions": [], "runs": _empty_runs()}

            versions = [v for v in (entry.get("versions") or [])
                        if isinstance(v, dict)]
            latest = _latest_version_number(versions)
            if versions:
                # An update is always a new version; history is never rewritten.
                checked.version = latest + 1
            stored = checked.to_dict()
            versions = [v for v in versions if v.get("version") != stored["version"]]
            versions.append(stored)
            entry["versions"] = versions
            entry.setdefault("disabled", False)
            entry.setdefault("runs", _empty_runs())
            block[checked.skill_id] = entry
            data["skills"] = block
            data.setdefault("schema", SCHEMA_VERSION)
            self._write(data)

        log.info("skill saved", extra_fields={
            "skill_id": checked.skill_id, "version": checked.version,
            "steps": len(checked.steps), "risk": checked.risk})
        return SkillManifest.from_dict(stored)

    def disable(self, skill_id: str) -> bool:
        """Make a skill non-runnable, keeping its history. False if it is unknown."""
        return self._set_disabled(skill_id, True)

    def enable(self, skill_id: str) -> bool:
        """Allow a skill to be run again. False if it is unknown."""
        return self._set_disabled(skill_id, False)

    def _set_disabled(self, skill_id: str, disabled: bool) -> bool:
        with self._lock:
            data = self._read()
            block = _skills_block(data)
            entry = block.get(str(skill_id))
            if not isinstance(entry, dict):
                return False
            entry["disabled"] = bool(disabled)
            block[str(skill_id)] = entry
            data["skills"] = block
            data.setdefault("schema", SCHEMA_VERSION)
            self._write(data)
        log.info("skill %s" % ("disabled" if disabled else "enabled"),
                 extra_fields={"skill_id": str(skill_id)})
        return True

    def delete(self, skill_id: str) -> bool:
        """Remove a skill and its reliability record. True if it existed."""
        with self._lock:
            data = self._read()
            block = _skills_block(data)
            if str(skill_id) not in block:
                return False
            del block[str(skill_id)]
            data["skills"] = block
            data.setdefault("schema", SCHEMA_VERSION)
            self._write(data)
        log.info("skill deleted", extra_fields={"skill_id": str(skill_id)})
        return True

    # -- reading ------------------------------------------------------------
    def get(self, skill_id: str, version: Optional[int] = None) -> Optional[SkillManifest]:
        """One manifest: the latest version by default, or a specific one."""
        entry = self._entry(skill_id)
        if entry is None:
            return None
        return self._manifest_from(entry, str(skill_id), version)

    def list(self) -> List[SkillManifest]:
        """The latest version of every stored skill, ordered by skill_id."""
        block = _skills_block(self._read())
        out: List[SkillManifest] = []
        for skill_id in sorted(block):
            entry = block[skill_id]
            if not isinstance(entry, dict):
                continue
            manifest = self._manifest_from(entry, str(skill_id), None)
            if manifest is not None:
                out.append(manifest)
        return out

    def is_enabled(self, skill_id: str) -> bool:
        """True only when the skill exists, is enabled, and has a stored version."""
        entry = self._entry(skill_id)
        if entry is None or bool(entry.get("disabled")):
            return False
        return _pick_version(entry, None) is not None

    def get_runnable(self, skill_id: str,
                     version: Optional[int] = None) -> Optional[SkillManifest]:
        """The manifest an execution path may use; None when disabled or absent.

        This is the accessor an executor must call. ``get`` is for inspection
        and deliberately returns a disabled skill so a person can find it and
        re-enable it.
        """
        entry = self._entry(skill_id)
        if entry is None or bool(entry.get("disabled")):
            return None
        return self._manifest_from(entry, str(skill_id), version)

    def _manifest_from(self, entry: Dict[str, Any], skill_id: str,
                       version: Optional[int]) -> Optional[SkillManifest]:
        try:
            pick = _pick_version(entry, version)
        except (TypeError, ValueError) as exc:
            raise SkillStoreError(
                f"skill {skill_id!r} version {version!r} is not a version number"
            ) from exc
        if pick is None:
            return None
        try:
            return SkillManifest.from_dict(pick)
        except ValueError as exc:
            raise SkillStoreError(
                f"stored skill {skill_id!r} is invalid: {exc}") from exc

    # -- reliability --------------------------------------------------------
    def record_run(self, skill_id: str, *, ok: bool, verdict: str,
                   duration_s: float, note: str = "") -> Dict[str, Any]:
        """Record one test run's measured outcome; returns the new counters.

        ``ok`` is whether the run executed; ``verdict`` is what verification
        concluded. Only ``ok and verdict == "verified"`` counts as a verified
        success. A run that executed but was not positively verified -- an
        unknown, a partial, a condition nobody could check -- counts as unknown,
        never as a success. A run that failed, or whose verdict is "failed",
        counts as a failure.
        """
        if not isinstance(ok, bool):
            raise ValueError("record_run needs ok as True/False")
        verdict_text = str(verdict or "").strip().lower()
        if not verdict_text:
            raise ValueError(
                "record_run needs the verifier's verdict, e.g. 'verified', "
                "'failed' or 'unknown'")
        duration = _duration(duration_s)
        note_text = str(note or "").strip()[:500]

        with self._lock:
            data = self._read()
            block = _skills_block(data)
            entry = block.get(str(skill_id))
            if not isinstance(entry, dict):
                raise ValueError(f"no skill {skill_id!r} to record a run for")

            runs = _runs_of(entry)
            if not ok or verdict_text == "failed":
                runs["failures"] += 1
            elif verdict_text == "verified":
                runs["verified_successes"] += 1
            else:
                runs["unknowns"] += 1

            now = time.time()
            runs["last_duration_s"] = duration
            runs["last_verdict"] = verdict_text
            runs["last_run_at"] = now
            recent = [r for r in (runs.get("recent") or []) if isinstance(r, dict)]
            recent.append({"ok": ok, "verdict": verdict_text,
                           "duration_s": duration, "note": note_text, "at": now})
            runs["recent"] = recent[-RECENT_RUNS:]

            entry["runs"] = runs
            block[str(skill_id)] = entry
            data["skills"] = block
            data.setdefault("schema", SCHEMA_VERSION)
            self._write(data)

        return _reliability_payload(str(skill_id), entry)

    def reliability(self, skill_id: str) -> Dict[str, Any]:
        """Measured counters for one skill -- no confidence percentages.

        With zero recorded runs the answer is ``success_rate=None`` and a note
        saying the skill is unverified. It is never 0% or 100% by default: both
        would be invented.
        """
        entry = self._entry(skill_id)
        if entry is None:
            raise ValueError(f"no skill {skill_id!r}")
        return _reliability_payload(str(skill_id), entry)


# ---------------------------------------------------------------------------
# Validation against what the caller actually has
# ---------------------------------------------------------------------------

def validate_skill(manifest: SkillManifest, *,
                   available_capabilities: Set[str],
                   allowed_scope: List[str],
                   max_risk: Optional[int] = None) -> List[str]:
    """Check a manifest against the caller's real capabilities and scope.

    Returns a list of problems; empty means the manifest is structurally
    runnable here. This function does not run anything and does not grant
    anything -- it answers "would the normal permission path be able to
    consider this skill?".

    Scope uses the same literal prefix rule as ``security/leases.py``
    ``_in_scope``: a required prefix must equal an allowed prefix or sit
    beneath it at a path boundary, so ``/a/bc`` is *not* covered by ``/a/b``.
    """
    if not isinstance(manifest, SkillManifest):
        raise TypeError(
            "validate_skill expects a SkillManifest; parse untrusted input "
            "with SkillManifest.from_dict first")
    if max_risk is not None:
        if isinstance(max_risk, bool) or not isinstance(max_risk, int) or max_risk < 0:
            raise ValueError("max_risk must be a non-negative integer")

    problems: List[str] = []

    if not manifest.steps:
        problems.append("the skill has no steps, so there is nothing to run")

    available = {str(capability) for capability in available_capabilities}
    for capability in manifest.required_capabilities:
        if capability not in available:
            problems.append(f"capability {capability!r} is not available here")

    scope = [str(prefix) for prefix in allowed_scope]
    allowed_text = ", ".join(scope) if scope else "none declared"
    for required in manifest.required_scope:
        if not _in_scope(required, scope):
            problems.append(
                f"required scope {required!r} is not covered by the allowed "
                f"scope ({allowed_text})")

    if max_risk is not None and manifest.risk > max_risk:
        problems.append(
            f"risk {manifest.risk} is above the ceiling of {max_risk}")

    return problems


def _in_scope(required: str, scope: List[str]) -> bool:
    """Literal prefix containment with a path boundary, mirroring lease scope.

    Copied deliberately rather than imported: ``security.leases`` owns the
    check for *spending* a lease, and this module must not acquire an import of
    the database stack just to compare two strings. The rule is identical --
    ``/a/bc`` never satisfies ``/a/b``.
    """
    candidate = str(required).rstrip("/") or "/"
    for prefix in scope:
        normalized = str(prefix).rstrip("/") or "/"
        if candidate == normalized:
            return True
        if candidate.startswith(normalized + "/"):
            return True
    return False


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def _skills_block(data: Dict[str, Any]) -> Dict[str, Any]:
    block = data.get("skills")
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise SkillStoreError(
            "the skills file has a malformed 'skills' block: it must be an "
            "object keyed by skill_id")
    return block


def _latest_version_number(versions: List[Dict[str, Any]]) -> int:
    best = 0
    for item in versions:
        try:
            best = max(best, int(item.get("version") or 0))
        except (TypeError, ValueError):
            continue
    return best


def _pick_version(entry: Dict[str, Any],
                  version: Optional[int]) -> Optional[Dict[str, Any]]:
    versions = [v for v in (entry.get("versions") or []) if isinstance(v, dict)]
    if not versions:
        return None
    if version is None:
        return max(versions, key=lambda v: _safe_int(v.get("version"), 0))
    target = int(version)
    for item in versions:
        if _safe_int(item.get("version"), 0) == target:
            return item
    return None


def _empty_runs() -> Dict[str, Any]:
    return {
        "verified_successes": 0,
        "failures": 0,
        "unknowns": 0,
        "last_duration_s": None,
        "last_verdict": "",
        "last_run_at": None,
        "recent": [],
    }


def _runs_of(entry: Dict[str, Any]) -> Dict[str, Any]:
    raw = entry.get("runs") if isinstance(entry.get("runs"), dict) else {}
    return {
        "verified_successes": max(0, _safe_int(raw.get("verified_successes"), 0)),
        "failures": max(0, _safe_int(raw.get("failures"), 0)),
        "unknowns": max(0, _safe_int(raw.get("unknowns"), 0)),
        "last_duration_s": _optional_float(raw.get("last_duration_s")),
        "last_verdict": str(raw.get("last_verdict") or ""),
        "last_run_at": _optional_float(raw.get("last_run_at")),
        "recent": [item for item in (raw.get("recent") or [])
                   if isinstance(item, dict)],
    }


def _reliability_payload(skill_id: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    runs = _runs_of(entry)
    total = (runs["verified_successes"] + runs["failures"] + runs["unknowns"])
    versions = [v for v in (entry.get("versions") or []) if isinstance(v, dict)]
    latest = _latest_version_number(versions)
    return {
        "skill_id": skill_id,
        "version": latest or None,
        "enabled": not bool(entry.get("disabled")),
        "runs": total,
        "verified_successes": runs["verified_successes"],
        "failures": runs["failures"],
        "unknowns": runs["unknowns"],
        # None with zero runs. `verified / total` is a measured ratio of what
        # was recorded, not a confidence and not a prediction.
        "success_rate": None if total == 0 else runs["verified_successes"] / total,
        "last_duration_s": runs["last_duration_s"],
        "last_verdict": runs["last_verdict"],
        "last_run_at": runs["last_run_at"],
        "note": ("no runs recorded: this skill is unverified"
                 if total == 0 else
                 "counts come from recorded test runs only; success_rate is a "
                 "measured ratio, not a guarantee"),
    }


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"skill manifest {field_name} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"skill manifest {field_name} must be a string")
    return value


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            f"skill manifest {field_name} must be a positive integer, "
            f"got {value!r}")
    return value


def _risk_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3:
        raise ValueError(
            f"skill manifest risk must be an integer in 0..3, got {value!r}")
    return value


def _string_list(value: Any, field_name: str) -> List[str]:
    if not isinstance(value, list):
        raise ValueError(
            f"skill manifest {field_name} must be a list of strings")
    out: List[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"skill manifest {field_name}[{index}] must be a non-empty string")
        out.append(item.strip())
    return out


def _input_map(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(
            "skill manifest inputs must be an object of name -> description")
    out: Dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(
                "skill manifest inputs keys must be non-empty strings")
        if not isinstance(item, str):
            raise ValueError(
                f"skill manifest inputs[{key!r}] must be a string description")
        out[key.strip()] = item
    return out


def _parse_steps(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("skill manifest steps must be a list")
    steps: List[Dict[str, Any]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ValueError(f"skill step {index} must be an object")
        unknown = sorted(set(raw) - STEP_FIELDS)
        if unknown:
            raise ValueError(
                f"skill step {index} has unknown field(s): "
                + ", ".join(unknown)
                + ". Known fields: " + ", ".join(sorted(STEP_FIELDS)) + ".")
        tool = raw.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            raise ValueError(f"skill step {index} must name the tool it calls")
        params = raw.get("params", {})
        if not isinstance(params, dict):
            raise ValueError(f"skill step {index} params must be an object")
        satisfies = raw.get("satisfies", "")
        if not isinstance(satisfies, str):
            raise ValueError(f"skill step {index} satisfies must be a condition id")
        steps.append({"tool": tool.strip(), "params": dict(params),
                      "satisfies": satisfies.strip()})
    return steps


def _parse_verification(value: Any) -> List[Dict[str, Any]]:
    """Validate the declarative verification shape and keep it otherwise as-is.

    The entries are hints for the executor's verifier mapping, never executed
    here. Extra keys are preserved because a future verifier kind may need them;
    the executor treats an unknown ``kind`` as UNKNOWN, never as verified.
    """
    if not isinstance(value, list):
        raise ValueError("skill manifest verification must be a list")
    out: List[Dict[str, Any]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise ValueError(f"skill verification {index} must be an object")
        condition_id = raw.get("condition_id")
        if not isinstance(condition_id, str) or not condition_id.strip():
            raise ValueError(
                f"skill verification {index} must name its condition_id")
        kind = raw.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError(
                f"skill verification {index} must name its kind")
        entry = dict(raw)
        entry["condition_id"] = condition_id.strip()
        entry["kind"] = kind.strip()
        for key in ("path", "needle"):
            if key in entry and not isinstance(entry[key], str):
                raise ValueError(
                    f"skill verification {index} {key} must be a string")
        out.append(entry)
    return out


def _duration(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("record_run duration_s must be a non-negative number")
    try:
        duration = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "record_run duration_s must be a non-negative number") from exc
    if not math.isfinite(duration) or duration < 0:
        raise ValueError(
            "record_run duration_s must be a finite, non-negative number")
    return duration


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None