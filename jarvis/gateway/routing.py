"""Capability routing: which provider does which kind of work.

Vantage already has a *text* selection (one provider, one model, used for the
assistant). This adds the rest — speech, images, vision, video, documents — as
**separate assignments**, because one model does not do all of them and a text
provider that answered once is no evidence it can speak.

Three rules, and they are the whole design:

* **A task never falls back into a different task.** A failed text-to-speech
  request is not retried on the chat model: that would "succeed" by returning
  prose where audio was asked for. Fallbacks are only ever within the same
  capability.
* **A fallback must be *verified* for that capability**, not merely configured.
  A model is verified by performing the real bounded request once
  (``gateway/media.verify_*``) and recording the result; until then it is
  *declared*, and declared is not routed to.
* **The answer says what happened.** The result carries whether the primary or
  a fallback ran, and whether the capability was verified or declared, so a
  caller cannot present an unverified path as a proven one.

The assignments live in the same selection file as the text choice
(``ai_selection.json``), under ``capabilities``, so there is one settings store
and one place to inspect.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..logging_setup import get_logger

log = get_logger("gateway.routing")

#: The task kinds that can have their own model. Text and embeddings are
#: handled by the existing single-provider selection and are listed here so a
#: reader can see the whole set rather than infer the ones that exist.
TASKS: Tuple[str, ...] = (
    "chat",
    "document_reading",
    "document_vision",
    "text_to_speech",
    "speech_to_text",
    "image_generation",
    "image_editing",
    "vision",
    "video_generation",
    "video_understanding",
    "embeddings",
)

#: The tasks this module routes. The others are the existing paths, and naming
#: them here without a route would imply a switch that does nothing.
ROUTED: Tuple[str, ...] = (
    "text_to_speech",
    "speech_to_text",
    "image_generation",
    "image_editing",
    "vision",
    "video_generation",
)

#: How long a verification stays good. A provider can revoke a key, a free tier
#: can end, or a model can be withdrawn; an old "verified" is a claim about the
#: past, and routing on it forever would be the same defect as a stale token
#: reported as connected.
VERIFY_TTL_SECONDS = 7 * 24 * 3600

#: A distinction the UI and the router both need.
VERIFIED = "verified"
DECLARED = "declared"
UNKNOWN = "unknown"

@dataclass
class ModelChoice:
    """One provider/model assigned to one task."""

    provider_id: str
    model: str
    base_url: str = ""
    #: Declared capability, by the person who configured it.
    declared: bool = True
    #: When this exact provider/model last completed a real request for this
    #: capability. 0 means never.
    verified_at: float = 0.0
    #: A short, safe note from the last test.
    note: str = ""

    @property
    def verified(self) -> bool:
        if self.verified_at <= 0:
            return False
        return (time.time() - self.verified_at) < VERIFY_TTL_SECONDS

    @property
    def status(self) -> str:
        return VERIFIED if self.verified else (DECLARED if self.declared else UNKNOWN)

    def public(self) -> Dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model": self.model,
            "base_url": self.base_url,
            "status": self.status,
            "verified_at": self.verified_at or None,
            "verified_age_s": (int(time.time() - self.verified_at)
                               if self.verified_at else None),
            "note": self.note,
        }

@dataclass
class Assignment:
    """What one task is assigned, and what may be tried after it."""

    task: str
    primary: Optional[ModelChoice] = None
    fallbacks: List[ModelChoice] = field(default_factory=list)

    def public(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "primary": self.primary.public() if self.primary else None,
            "fallbacks": [f.public() for f in self.fallbacks],
            "configured": self.primary is not None,
            "ready": bool(self.primary and self.primary.verified),
        }

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

CAPABILITY_KEY = "capabilities"

def _path(home: Optional[Path] = None) -> Path:
    from .selection import selection_path

    return selection_path(home)

def _read(home: Optional[Path] = None) -> Dict[str, Any]:
    path = _path(home)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}

def _write(data: Dict[str, Any], home: Optional[Path] = None) -> None:
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass

def load_assignments(home: Optional[Path] = None) -> Dict[str, Assignment]:
    """Every routed task's assignment. An unconfigured task has an empty one."""
    block = _read(home).get(CAPABILITY_KEY)
    out: Dict[str, Assignment] = {task: Assignment(task=task) for task in ROUTED}
    if not isinstance(block, dict):
        return out
    for task in ROUTED:
        raw = block.get(task)
        if not isinstance(raw, dict):
            continue
        primary = _choice(raw.get("primary"))
        fallbacks = [_choice(f) for f in (raw.get("fallbacks") or [])]
        out[task] = Assignment(task=task, primary=primary,
                               fallbacks=[f for f in fallbacks if f is not None])
    return out

def _choice(raw: Any) -> Optional[ModelChoice]:
    if not isinstance(raw, dict):
        return None
    provider_id = str(raw.get("provider_id") or "").strip()
    model = str(raw.get("model") or "").strip()
    if not provider_id:
        return None
    try:
        verified_at = float(raw.get("verified_at") or 0)
    except (TypeError, ValueError):
        verified_at = 0.0
    return ModelChoice(
        provider_id=provider_id,
        model=model,
        base_url=str(raw.get("base_url") or ""),
        declared=bool(raw.get("declared", True)),
        verified_at=verified_at,
        note=str(raw.get("note") or "")[:200],
    )

def set_assignment(task: str, *, provider_id: str, model: str = "",
                   base_url: str = "", fallbacks: Optional[List[Dict[str, str]]] = None,
                   home: Optional[Path] = None) -> Assignment:
    """Assign a provider/model to one task. Raises ``ValueError`` for a bad task."""
    if task not in ROUTED:
        raise ValueError(
            f"“{task}” is not a routed capability. Routed: {', '.join(ROUTED)}.")
    provider_id = (provider_id or "").strip()
    if not provider_id:
        raise ValueError("A capability needs a provider.")

    data = _read(home)
    block = data.get(CAPABILITY_KEY)
    if not isinstance(block, dict):
        block = {}
    existing = block.get(task) if isinstance(block.get(task), dict) else {}
    old_primary = existing.get("primary") if isinstance(existing.get("primary"), dict) else {}

    # Re-assigning the same provider/model keeps its verification; assigning a
    # different one starts unverified, because the old evidence is about a
    # different model and carrying it over would be the "name implies
    # capability" error.
    same = (old_primary.get("provider_id") == provider_id
            and (old_primary.get("model") or "") == model)
    primary = {
        "provider_id": provider_id,
        "model": model,
        "base_url": (base_url or old_primary.get("base_url") or "").strip()
                    if same else (base_url or "").strip(),
        "declared": True,
        "verified_at": float(old_primary.get("verified_at") or 0) if same else 0.0,
        "note": str(old_primary.get("note") or "") if same else "",
    }
    block[task] = {
        "primary": primary,
        "fallbacks": list(fallbacks or []),
    }
    data[CAPABILITY_KEY] = block
    _write(data, home)
    return load_assignments(home)[task]

def record_verification(task: str, *, provider_id: str, model: str, ok: bool,
                        note: str = "", home: Optional[Path] = None) -> Assignment:
    """Record the outcome of a real request for one assigned model.

    A failure clears the verification rather than leaving an older success in
    place: "it worked last week" is not a statement about now, and the router
    falls back for exactly the case where the primary stopped working.
    """
    data = _read(home)
    block = data.get(CAPABILITY_KEY)
    if not isinstance(block, dict) or not isinstance(block.get(task), dict):
        return load_assignments(home).get(task) or Assignment(task=task)
    entry = block[task]
    for key in ("primary",):
        candidate = entry.get(key)
        if isinstance(candidate, dict) \
                and candidate.get("provider_id") == provider_id \
                and (candidate.get("model") or "") == model:
            candidate["verified_at"] = time.time() if ok else 0.0
            candidate["note"] = (note or "")[:200]
    for candidate in entry.get("fallbacks") or []:
        if isinstance(candidate, dict) \
                and candidate.get("provider_id") == provider_id \
                and (candidate.get("model") or "") == model:
            candidate["verified_at"] = time.time() if ok else 0.0
            candidate["note"] = (note or "")[:200]
    data[CAPABILITY_KEY] = block
    _write(data, home)
    log.info("capability verification recorded",
             extra_fields={"task": task, "provider": provider_id, "model": model,
                           "ok": ok})
    return load_assignments(home).get(task) or Assignment(task=task)

# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

@dataclass
class RouteAttempt:
    provider_id: str
    model: str
    status: str          # verified | declared
    ok: bool
    error: str = ""
    duration_s: float = 0.0

    def public(self) -> Dict[str, Any]:
        return {"provider_id": self.provider_id, "model": self.model,
                "status": self.status, "ok": self.ok,
                "error": self.error[:240], "duration_s": round(self.duration_s, 3)}

@dataclass
class RouteResult:
    task: str
    ok: bool = False
    result: Any = None
    used: Optional[ModelChoice] = None
    attempts: List[RouteAttempt] = field(default_factory=list)
    error: str = ""

    @property
    def used_fallback(self) -> bool:
        return bool(self.attempts) and self.attempts[0].ok is False

    def public(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "ok": self.ok,
            "used_fallback": self.used_fallback,
            "attempts": [a.public() for a in self.attempts],
            "error": self.error,
        }

def route(task: str, perform, *, settings: Any = None,
          home: Optional[Path] = None, allow_fallback: bool = True) -> RouteResult:
    """Run ``perform(adapter, model)`` on the assigned provider, with fallbacks.

    ``perform`` receives the built adapter and is responsible for making the
    real request. Its ``MediaResult.ok`` decides whether the next candidate is
    tried. Only *verified* candidates are tried when a fallback is needed, and
    only candidates assigned to this same task are ever considered.
    """
    from ..gateway import media

    assignments = load_assignments(home)
    assignment = assignments.get(task)
    out = RouteResult(task=task)
    if assignment is None or assignment.primary is None:
        out.error = f"No provider is assigned to {task.replace('_', ' ')}."
        return out

    candidates: List[ModelChoice] = [assignment.primary]
    if allow_fallback:
        candidates += [f for f in assignment.fallbacks if f.verified]

    for index, choice in enumerate(candidates):
        if index > 0 and not choice.verified:
            continue          # a declared fallback is not a fallback yet
        adapter = _make(task, choice, settings)
        if adapter is None:
            out.attempts.append(RouteAttempt(choice.provider_id, choice.model,
                                             choice.status, False,
                                             error="No adapter for this capability."))
            continue
        started = time.monotonic()
        try:
            result = perform(adapter)
        except Exception as exc:
            out.attempts.append(RouteAttempt(
                choice.provider_id, choice.model, choice.status, False,
                error=f"{type(exc).__name__}: {exc}",
                duration_s=time.monotonic() - started))
            continue
        ok = bool(getattr(result, "ok", False))
        out.attempts.append(RouteAttempt(
            choice.provider_id, choice.model, choice.status, ok,
            error="" if ok else str(getattr(result, "error", "") or "")[:240],
            duration_s=time.monotonic() - started))
        if ok:
            out.ok = True
            out.result = result
            out.used = choice
            return out

    failures = [a.error for a in out.attempts if a.error]
    out.error = failures[0] if failures else f"The {task.replace('_', ' ')} request failed."
    return out

def _make(task: str, choice: ModelChoice, settings: Any):
    """The adapter for one (task, choice). None when the capability is unsupported."""
    from ..gateway import media
    from .selection import effective_base_url, effective_key

    family = {
        "text_to_speech": "text_to_speech",
        "speech_to_text": "speech_to_text",
        "image_generation": "image_generation",
        "image_editing": "image_editing",
        "vision": "vision",
        "video_generation": "video_generation",
    }.get(task)
    if family is None:
        return None
    api_key = ""
    try:
        api_key = effective_key(settings, choice.provider_id) or ""
    except Exception:
        api_key = ""
    base_url = choice.base_url
    if not base_url:
        try:
            base_url = effective_base_url(settings, choice.provider_id) or ""
        except Exception:
            base_url = ""
    if not base_url:
        return None
    return media.build_for(choice.provider_id, family, base_url=base_url,
                           api_key=api_key, model=choice.model)