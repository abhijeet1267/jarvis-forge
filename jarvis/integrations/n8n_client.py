"""The n8n webhook client: one place that talks to n8n, and the only one.

Every result is a typed ``ExecutionResult`` whose ``status`` is taken from what
actually happened — never from the mere fact that a request was sent. The four
outcomes the rest of Vantage branches on are deliberately distinct:

* ``succeeded`` — n8n answered 2xx with a readable body;
* ``failed``    — n8n answered, and said the workflow failed (or 4xx/5xx);
* ``timeout``   — the request outlived its budget;
* ``unreachable`` — nothing answered at all.

Collapsing any of these into "it worked" or "it did not work" would make a
person unable to tell a broken workflow from a stopped container — and the
brief is explicit that a failed execution must never be reported as success.

Duplicate protection is part of the client rather than the caller. A retry after
a timeout is exactly the case that repeats a non-idempotent action, so:

* a correlation id is reserved *before* the request goes out, and a second
  execution with the same id is refused while the first is still in flight;
* a workflow declared non-idempotent is not retried automatically at all.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from ..logging_setup import get_logger
from ..net import DeadlineExceeded, http_read
from ..security.privacy import redact
from .n8n import (
    MAX_OUTPUT_BYTES,
    RiskClass,
    N8NConfig,
    InputRejected,
    WorkflowSpec,
    config_from_settings,
    encode_payload,
    new_correlation_id,
    normalize_base_url,
    validate_inputs,
    workflow,
)

log = get_logger("n8n")

class ExecutionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"
    #: The request was refused before it left Vantage (unknown workflow, invalid
    #: input, duplicate). Not a failure of n8n, and never counted as one.
    REFUSED = "refused"

@dataclass
class ExecutionResult:
    status: ExecutionStatus
    correlation_id: str
    workflow_key: str = ""
    #: A sentence safe to show a person. Already redacted.
    message: str = ""
    #: The workflow's own JSON, parsed and size-checked. Empty unless succeeded.
    output: Dict[str, Any] = field(default_factory=dict)
    http_status: int = 0
    duration_ms: int = 0
    #: Whether repeating this exact run is safe. False for a non-idempotent
    #: workflow, and for the ambiguous case of a timeout on a send.
    retry_safe: bool = False
    #: True when the registry declares this workflow read-only, so a successful
    #: answer is itself the observed result. A write's side effect is never
    #: observed here, so it must not be reported as verified.
    read_only: bool = False

    @property
    def ok(self) -> bool:
        return self.status is ExecutionStatus.SUCCEEDED

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status.value,
            "correlation_id": self.correlation_id,
            "workflow": self.workflow_key,
            "message": self.message,
            "output": self.output,
            "http_status": self.http_status,
            "duration_ms": self.duration_ms,
            "retry_safe": self.retry_safe,
        }

# ---------------------------------------------------------------------------
# In-flight guard
# ---------------------------------------------------------------------------

class _InFlight:
    """Refuses two runs of the same workflow with the same correlation id.

    Process-wide and keyed by (workflow, correlation id). The window is short —
    one request — but it is exactly the window a double-submit or a retry-after-
    timeout lands in.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: set = set()

    def acquire(self, workflow_key: str, correlation_id: str) -> bool:
        token = (workflow_key, correlation_id)
        with self._lock:
            if token in self._active:
                return False
            self._active.add(token)
            return True

    def release(self, workflow_key: str, correlation_id: str) -> None:
        with self._lock:
            self._active.discard((workflow_key, correlation_id))

    def busy(self, workflow_key: str) -> bool:
        with self._lock:
            return any(k == workflow_key for k, _ in self._active)

_in_flight = _InFlight()

# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class N8NClient:
    """Executes approved workflows over their webhooks.

    Deliberately has no method that creates, edits, activates or deletes a
    workflow: those live on n8n's admin API, which this client cannot reach.
    """

    #: How many redirects a webhook exchange may follow. Zero: a redirect from a
    #: workflow endpoint is not part of the design, and following one is how a
    #: local integration is turned into a request to somewhere else.
    MAX_REDIRECTS = 0

    def __init__(self, config: Optional[N8NConfig] = None,
                 settings: Any = None) -> None:
        if config is not None:
            self.config = config
        else:
            from ..config import get_settings

            self.config = config_from_settings(settings or get_settings())

    # -- readiness ---------------------------------------------------------

    def _base(self) -> str:
        return normalize_base_url(self.config.base_url,
                                  allow_remote=self.config.allow_remote)

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.config.api_key:
            headers[self.config.auth_header or "Authorization"] = (
                (self.config.auth_prefix or "") + self.config.api_key)
        return headers

    def _scrub(self, text: str) -> str:
        """Remove the credential and any URL userinfo from a sentence."""
        out = redact(text or "")
        if self.config.api_key:
            out = out.replace(self.config.api_key, "[redacted]")
        return out

    def readiness(self) -> Dict[str, Any]:
        """A safe, bounded readiness check: does the configured base answer at all?

        This asks n8n's own unauthenticated health endpoint rather than a
        webhook, because a webhook proves one workflow while this proves the
        server. It is deliberately *not* proof that a particular workflow is
        registered — that is established by executing it.
        """
        from .n8n import ConnectionState

        if not self.config.enabled:
            return {"state": ConnectionState.DISABLED.value,
                    "detail": "The n8n integration is turned off."}
        if not self.config.configured:
            return {"state": ConnectionState.NOT_CONFIGURED.value,
                    "detail": "No n8n URL is configured."}
        try:
            base = self._base()
        except Exception as exc:
            return {"state": ConnectionState.MISCONFIGURED.value, "detail": str(exc)}

        url = base + "/healthz"
        request = urllib.request.Request(url, headers={"Accept": "application/json"},
                                         method="GET")
        started = time.monotonic()
        try:
            body = http_read(request, total=min(self.config.timeout, 8.0),
                             what="the n8n server")
        except DeadlineExceeded:
            return {"state": ConnectionState.UNREACHABLE.value,
                    "detail": "The n8n server did not answer within 8 seconds.",
                    "latency_ms": int((time.monotonic() - started) * 1000)}
        except urllib.error.HTTPError as exc:
            # Any HTTP answer proves the server is up; an auth-gated health
            # endpoint is still a live server.
            return {"state": ConnectionState.READY.value,
                    "detail": f"n8n answered HTTP {exc.code}.",
                    "http_status": exc.code,
                    "latency_ms": int((time.monotonic() - started) * 1000)}
        except Exception as exc:
            return {"state": ConnectionState.UNREACHABLE.value,
                    "detail": self._scrub(
                        f"Could not reach n8n ({type(exc).__name__})."),
                    "latency_ms": int((time.monotonic() - started) * 1000)}
        detail = "n8n answered its health check."
        try:
            parsed = json.loads(body or b"{}")
            if isinstance(parsed, dict) and parsed.get("status"):
                detail = f"n8n answered its health check (status: {parsed['status']})."
        except Exception:
            pass
        return {"state": ConnectionState.READY.value, "detail": detail,
                "latency_ms": int((time.monotonic() - started) * 1000)}

    # -- execution ---------------------------------------------------------

    def execute(self, key: str, inputs: Optional[Dict[str, Any]] = None, *,
                correlation_id: str = "",
                confirmed: bool = False) -> ExecutionResult:
        """Run one registry workflow.

        The order matters and is the security design:

        1. the key is looked up in the registry (never a URL from the caller);
        2. the allowlist is checked;
        3. a consequential workflow is refused without ``confirmed=True``;
        4. the inputs are validated against the declared schema;
        5. only then is a correlation id reserved and a request sent.
        """
        spec = workflow(key)
        if spec is None:
            return ExecutionResult(
                status=ExecutionStatus.REFUSED, correlation_id=correlation_id or "",
                workflow_key=key,
                message=f"There is no approved workflow called “{key}”. "
                        f"Approved: {', '.join(w.key for w in REGISTRY_FOR_MESSAGE)}.")

        if not self.config.enabled:
            return ExecutionResult(ExecutionStatus.REFUSED, correlation_id,
                                   spec.key, "The n8n integration is turned off.")
        if not self.config.permits(spec.key):
            return ExecutionResult(
                ExecutionStatus.REFUSED, correlation_id or new_correlation_id(),
                spec.key,
                f"“{spec.title}” is not on the Vantage allowlist, so it was not run.")

        if spec.risk.requires_confirmation and not confirmed:
            return ExecutionResult(
                ExecutionStatus.REFUSED, correlation_id, spec.key,
                f"“{spec.title}” can affect things outside Vantage, so it needs your "
                "confirmation before it runs. Nothing was sent.")

        try:
            cleaned = validate_inputs(spec, inputs)
        except InputRejected as exc:
            return ExecutionResult(ExecutionStatus.REFUSED,
                                   correlation_id or new_correlation_id(),
                                   spec.key, str(exc))

        if not correlation_id:
            correlation_id = new_correlation_id()
        if not _in_flight.acquire(spec.key, correlation_id):
            return ExecutionResult(
                ExecutionStatus.REFUSED, correlation_id, spec.key,
                "That exact run is already in progress; it was not started twice.",
                retry_safe=False)

        try:
            return self._send(spec, cleaned, correlation_id)
        finally:
            _in_flight.release(spec.key, correlation_id)

    def _send(self, spec: WorkflowSpec, inputs: Dict[str, Any],
              correlation_id: str) -> ExecutionResult:
        started = time.monotonic()
        try:
            url = self._base() + "/webhook/" + spec.path.lstrip("/")
        except Exception as exc:
            return ExecutionResult(ExecutionStatus.REFUSED, correlation_id, spec.key,
                                   str(exc))

        payload = dict(inputs)
        # A workflow that summarises calendar data gets it from Vantage, which
        # already holds the connection. The workflow is never handed a Google
        # credential: it cannot reach the calendar on its own, and giving it one
        # would be a second copy of a secret and a second thing to revoke.
        #
        # `events_available=False` is carried explicitly so the workflow can
        # report a failed read as a failure rather than as a free day.
        if "events" in spec.context and "events" not in payload:
            events, problem = self._calendar_events()
            payload["events"] = events
            payload["days"] = int(inputs.get("days") or 1)
            if problem:
                payload["events_error"] = problem
            # "" is "no problem", not "not None". The success path returns an
            # empty string, so an `is None` test reported every readable
            # calendar as unavailable — the same empty-vs-failed confusion this
            # project keeps finding, this time in the flag meant to prevent it.
            payload["_events_available"] = not problem
        # The workflow receives the correlation id, so n8n's own execution log
        # can be matched to this request. It is ours, not the caller's.
        payload["_vantage"] = {"correlation_id": correlation_id}

        request = urllib.request.Request(
            url, data=encode_payload(payload), headers=self._headers(), method="POST")

        budget = min(max(spec.timeout, 1.0), self.config.timeout, 120.0)
        try:
            body = http_read(request, total=budget, what=f"the {spec.title} workflow")
        except DeadlineExceeded:
            duration = int((time.monotonic() - started) * 1000)
            log.info("n8n execution timed out",
                     extra_fields={"workflow": spec.key, "correlation": correlation_id,
                                   "seconds": budget})
            return ExecutionResult(
                ExecutionStatus.TIMEOUT, correlation_id, spec.key,
                f"The “{spec.title}” workflow did not finish within {budget:g} seconds. "
                + ("Repeating it is safe." if spec.idempotent
                   else "It may still be running, so it was not retried automatically."),
                duration_ms=duration, retry_safe=spec.idempotent)
        except urllib.error.HTTPError as exc:
            return self._http_failure(spec, exc, correlation_id, started)
        except Exception as exc:
            duration = int((time.monotonic() - started) * 1000)
            detail = self._scrub(f"{type(exc).__name__}: {exc}")
            log.info("n8n unreachable",
                     extra_fields={"workflow": spec.key, "correlation": correlation_id,
                                   "error": detail[:200]})
            return ExecutionResult(
                ExecutionStatus.UNREACHABLE, correlation_id, spec.key,
                "Could not reach n8n, so the workflow was not run. "
                + detail, duration_ms=duration, retry_safe=spec.idempotent)

        return self._read_success(spec, body, correlation_id, started)

    def _calendar_events(self) -> Tuple[List[Dict[str, Any]], str]:
        """Today's events, as the local backend reports them. ``(events, problem)``.

        A failed read returns the reason, never an empty list: a workflow that
        received ``[]`` for an unreachable calendar would summarise a free day
        that does not exist.

        Imported lazily and guarded: Vantage must start and run with this
        integration absent, exactly as it does with n8n off.
        """
        try:
            from ..integrations.calendar_provider import get_calendar_provider

            provider = get_calendar_provider()
            from datetime import datetime, timedelta

            now = datetime.now().astimezone()
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            result = provider.list_events(start, start + timedelta(days=1), limit=200)
            status = getattr(result, "status", None)
            if not getattr(status, "read_succeeded", False):
                detail = getattr(result, "detail", "") or "the calendar could not be read"
                return [], self._scrub(detail)
            events = []
            for event in result:
                as_dict = getattr(event, "as_dict", None)
                if callable(as_dict):
                    events.append(as_dict())
            return events, ""
        except Exception as exc:
            return [], self._scrub(f"the calendar could not be read ({type(exc).__name__})")

    def _http_failure(self, spec: WorkflowSpec, exc: urllib.error.HTTPError,
                      correlation_id: str, started: float) -> ExecutionResult:
        duration = int((time.monotonic() - started) * 1000)
        code = int(exc.code)
        try:
            raw = exc.read() or b""
        except Exception:
            raw = b""
        finally:
            try:
                exc.close()
            except Exception:
                pass
        detail = self._scrub(_message_from_body(raw))
        if code in (401, 403):
            message = ("n8n refused the credential for this workflow. Check the n8n "
                       "API key configured for Vantage.")
        elif code == 404:
            message = (f"n8n has no active webhook at “{spec.path}”. The workflow may "
                       "not be activated, or its webhook path may differ.")
        elif 400 <= code < 500:
            message = f"n8n rejected the request (HTTP {code})."
        else:
            message = f"The “{spec.title}” workflow failed on n8n's side (HTTP {code})."
        if detail:
            message += f" {detail}"
        log.info("n8n http failure",
                 extra_fields={"workflow": spec.key, "correlation": correlation_id,
                               "http_status": code})
        return ExecutionResult(
            ExecutionStatus.FAILED, correlation_id, spec.key, message,
            http_status=code, duration_ms=duration, retry_safe=spec.idempotent)

    def _read_success(self, spec: WorkflowSpec, body: bytes, correlation_id: str,
                      started: float) -> ExecutionResult:
        duration = int((time.monotonic() - started) * 1000)
        if len(body) > MAX_OUTPUT_BYTES:
            return ExecutionResult(
                ExecutionStatus.FAILED, correlation_id, spec.key,
                f"The workflow returned {len(body)} bytes, which is over the "
                f"{MAX_OUTPUT_BYTES}-byte limit. It was not treated as a result.",
                duration_ms=duration, retry_safe=spec.idempotent)
        try:
            parsed = json.loads(body or b"{}")
        except Exception:
            return ExecutionResult(
                ExecutionStatus.FAILED, correlation_id, spec.key,
                "The workflow answered, but not with JSON, so there is no result to "
                "report.", duration_ms=duration, retry_safe=spec.idempotent)
        if not isinstance(parsed, dict):
            parsed = {"result": parsed}

        # A workflow can report its own failure in the body. Honour it: a 200
        # is not a success when the workflow says it failed.
        failed_flag = parsed.get("ok") is False or parsed.get("success") is False
        error_text = parsed.get("error") or parsed.get("message") if failed_flag else None
        if failed_flag:
            return ExecutionResult(
                ExecutionStatus.FAILED, correlation_id, spec.key,
                self._scrub(str(error_text or "The workflow reported that it failed.")),
                http_status=200, duration_ms=duration, retry_safe=spec.idempotent,
                output=parsed)

        # The workflow's own execution id, when it returns one; otherwise ours.
        echoed = str((parsed.get("_vantage") or {}).get("correlation_id") or "")

        log.info("n8n execution succeeded",
                 extra_fields={"workflow": spec.key, "correlation": correlation_id,
                               "echoed": bool(echoed), "ms": duration})
        message = self._success_message(spec, parsed)
        return ExecutionResult(
            ExecutionStatus.SUCCEEDED, correlation_id, spec.key, message,
            output=parsed, http_status=200, duration_ms=duration,
            retry_safe=spec.idempotent,
            read_only=spec.risk is RiskClass.READ)

    def _success_message(self, spec: WorkflowSpec, parsed: Dict[str, Any]) -> str:
        """A sentence built from the workflow's own output, not from a template.

        Falls back to "it completed" rather than inventing a description of
        what it did — the caller shows the payload alongside.
        """
        for key in ("summary", "detail", "message", "text"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return self._scrub(value.strip()[:400])
        if "count" in parsed:
            return f"The {spec.title} workflow completed and returned {parsed['count']} item(s)."
        return f"The {spec.title} workflow completed."

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _message_from_body(raw: bytes) -> str:
    """n8n's own error text, trimmed and never trusted as HTML."""
    try:
        parsed = json.loads(raw or b"{}")
    except Exception:
        return ""
    if not isinstance(parsed, dict):
        return ""
    for key in ("message", "error", "detail", "description"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:240]
    return ""

def client_from_settings(settings: Any = None) -> N8NClient:
    return N8NClient(settings=settings)

#: Imported late so the message in ``execute`` can list the registry without a
#: cycle at module import time. Kept as a module-level alias because the message
#: reads better than an inline import.
from .n8n import REGISTRY as REGISTRY_FOR_MESSAGE  # noqa: E402