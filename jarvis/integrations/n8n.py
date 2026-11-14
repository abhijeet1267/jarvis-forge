"""n8n automation: configuration, the approved-workflow registry, and schemas.

Three deliberate boundaries, the same ones ``mcp_client`` draws for its server:

* **A workflow the model can name is not a workflow n8n will run.** Execution
  accepts only a registry key, never a workflow id or URL from a request. The
  registry is a hand-written list in this file: adding a workflow to it is a
  deliberate act by a person editing code, not something discovery can do.
* **The n8n API key never leaves the backend.** It is read from the environment
  or the OS keychain, is used only for the outbound request, and is redacted
  from every message this module can produce. Nothing here returns it.
* **Vantage talks to webhooks, not to n8n's admin API.** The admin API is an
  edition feature on some deployments, and it can create and activate
  workflows. The adapter deliberately cannot reach it, so a compromised chat
  turn cannot reprogram the automation server.

Inputs are validated against a per-workflow schema *before* anything is sent,
and the response is parsed against limits, because a workflow is code the
person wrote and its output is untrusted for the same reason a web page is.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from ..security.privacy import redact

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

#: The largest input payload accepted. n8n will happily accept megabytes, and
#: so will a compromised model; a bounded body keeps one request from becoming a
#: memory event in the backend or a storage event in n8n.
MAX_INPUT_BYTES = 32_000
#: The largest response body read. A workflow that returns a file should return
#: a URL, not the file.
MAX_OUTPUT_BYTES = 512_000
#: Default and ceiling for one execution, in seconds. Synchronous webhooks are
#: expected to answer quickly; anything longer belongs in the async path.
DEFAULT_TIMEOUT = 20.0
MAX_TIMEOUT = 120.0
#: How many parameter names one workflow may declare.
MAX_FIELDS = 24

class RiskClass(str, Enum):
    """How much a person should be asked before a workflow runs.

    Separate from the tool registry's ``RiskLevel`` because the question is
    slightly different: this one is about what the *workflow* does, and the
    registry cannot see inside it. The class is therefore a declaration by the
    person who added the workflow, and the adapter treats an under-declaration
    as their responsibility rather than pretending to verify it.
    """

    READ = "read"            # returns information; changes nothing external
    MUTATING = "mutating"    # creates/updates something inside the account
    EXTERNAL = "external"    # leaves the machine: sends mail, posts, calls a paid API

    @property
    def requires_confirmation(self) -> bool:
        return self is not RiskClass.READ

#: The field kinds a workflow input may declare.
FieldKind = str                    # "string" | "text" | "integer" | "boolean" | "choice"

@dataclass(frozen=True)
class FieldSpec:
    """One input a workflow needs, and the rules its value must satisfy."""

    name: str
    kind: FieldKind = "string"
    required: bool = False
    description: str = ""
    #: For ``choice``: the exact accepted values.
    choices: Tuple[str, ...] = ()
    max_length: int = 500
    minimum: Optional[int] = None
    maximum: Optional[int] = None

    def public(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "required": self.required,
            "description": self.description,
        }
        if self.choices:
            out["choices"] = list(self.choices)
        if self.kind in ("string", "text"):
            out["max_length"] = self.max_length
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        return out

@dataclass(frozen=True)
class WorkflowSpec:
    """One approved workflow, as Vantage sees it.

    ``key`` is Vantage's own name and the only identifier a request may carry.
    ``path`` is the webhook path on the n8n side; it is deliberately *not* handed
    to clients, because knowing a webhook path is most of what a local attacker
    would need to bypass the registry.
    """

    key: str
    title: str
    description: str
    #: Webhook path, relative to the configured base URL. e.g. ``vantage/echo``.
    path: str
    risk: RiskClass = RiskClass.READ
    #: ``sync``: the webhook answers with the result. ``async``: it answers
    #: ``202``/an execution id and the caller polls the status route.
    mode: str = "sync"
    fields: Tuple[FieldSpec, ...] = ()
    #: What the workflow is documented to return. Advisory only -- the adapter
    #: reports what it actually got; this is here so the UI can describe it.
    returns: str = ""
    #: Whether retrying a failed run is safe. A read is idempotent; a send is not.
    idempotent: bool = True
    timeout: float = DEFAULT_TIMEOUT
    #: Data Vantage gathers and supplies with the request. ``events`` means the
    #: workflow summarises the calendar Vantage is already connected to, rather
    #: than needing a credential of its own. This is the only context source
    #: that exists, deliberately: each addition is a new decision, not a default.
    context: Tuple[str, ...] = ()

    def public(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "description": self.description,
            "risk": self.risk.value,
            "requires_confirmation": self.risk.requires_confirmation,
            "mode": self.mode,
            "idempotent": self.idempotent,
            "fields": [f.public() for f in self.fields],
            "returns": self.returns,
        }

# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
#
# Everything below is deliberate. A workflow exists in Vantage only because a
# person added it here, with a description, its inputs, and its risk. Nothing
# discovers and auto-registers.

REGISTRY: Tuple[WorkflowSpec, ...] = (
    WorkflowSpec(
        key="echo",
        title="Diagnostic echo",
        description="Returns what it was given, plus the time n8n saw it. Proves the "
                    "path from Vantage to n8n and back without touching any service.",
        path="vantage/echo",
        risk=RiskClass.READ,
        fields=(
            FieldSpec("message", "text", required=True,
                      description="Any text. It is echoed back, not interpreted.",
                      max_length=2000),
            FieldSpec("tag", "string", required=False,
                      description="A label to echo alongside the message.",
                      max_length=64),
        ),
        returns="message, tag, received_at, and the workflow's own name.",
        idempotent=True,
    ),
    WorkflowSpec(
        key="daily_agenda",
        title="Daily agenda",
        description="Reads the next day of events and returns a structured summary. "
                    "Read-only: it creates and changes nothing.",
        path="vantage/daily-agenda",
        risk=RiskClass.READ,
        fields=(
            FieldSpec("days", "integer", required=False,
                      description="How many days ahead to include (1–14).",
                      minimum=1, maximum=14),
        ),
        returns="events[], each with title, start, end and location; a count.",
        idempotent=True,
        context=("events",),
    ),
    WorkflowSpec(
        key="notify",
        title="Send a notification",
        description="Sends a message through the notification provider configured in "
                    "the workflow. When none is configured the workflow performs a dry "
                    "run and says so — it never claims a delivery that did not happen.",
        path="vantage/notify",
        risk=RiskClass.EXTERNAL,
        fields=(
            FieldSpec("message", "text", required=True,
                      description="The notification body.", max_length=500),
            FieldSpec("level", "choice", required=False,
                      description="How urgent it is.",
                      choices=("info", "warning", "critical")),
        ),
        returns="delivered (true/false), dry_run (true/false), provider, detail.",
        idempotent=False,
    ),
)

_BY_KEY: Dict[str, WorkflowSpec] = {w.key: w for w in REGISTRY}

def workflow(key: str) -> Optional[WorkflowSpec]:
    """The approved workflow with this key, or None. Case-insensitive on the key."""
    return _BY_KEY.get((key or "").strip().lower())

def workflow_keys() -> List[str]:
    return [w.key for w in REGISTRY]

def catalog() -> List[Dict[str, Any]]:
    return [w.public() for w in REGISTRY]

# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

class InputRejected(ValueError):
    """The inputs are not what this workflow declared. Safe to show a person."""

def _coerce(spec: FieldSpec, raw: Any) -> Any:
    if spec.kind == "boolean":
        if isinstance(raw, bool):
            return raw
        raise InputRejected(f"“{spec.name}” must be true or false.")
    if spec.kind == "integer":
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise InputRejected(f"“{spec.name}” must be a whole number.")
        if spec.minimum is not None and value < spec.minimum:
            raise InputRejected(f"“{spec.name}” must be at least {spec.minimum}.")
        if spec.maximum is not None and value > spec.maximum:
            raise InputRejected(f"“{spec.name}” must be at most {spec.maximum}.")
        return value
    if raw is None:
        raise InputRejected(f"“{spec.name}” must be text.")
    text = raw if isinstance(raw, str) else str(raw)
    if spec.kind in ("string", "text", "choice"):
        text = text.strip()
        if len(text) > spec.max_length:
            raise InputRejected(
                f"“{spec.name}” is {len(text)} characters; the maximum is "
                f"{spec.max_length}.")
        if spec.kind == "choice":
            if not spec.choices:
                raise InputRejected(f"“{spec.name}” accepts no values.")
            if text not in spec.choices:
                raise InputRejected(
                    f"“{spec.name}” must be one of: {', '.join(spec.choices)}.")
        if spec.kind == "string" and "\n" in text:
            raise InputRejected(f"“{spec.name}” must be a single line.")
        return text
    raise InputRejected(f"“{spec.name}” has an unsupported type.")

def validate_inputs(spec: WorkflowSpec, raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the cleaned inputs, or raise ``InputRejected``.

    Unknown fields are refused rather than passed through: a workflow that
    ignores an extra field is fine, but a workflow that *uses* it (a template,
    a URL) is exactly how an unvalidated input becomes an action. Refusing is
    the only answer that cannot surprise the person who wrote the workflow.
    """
    raw = raw or {}
    if not isinstance(raw, dict):
        raise InputRejected("Inputs must be an object of field names and values.")
    known = {f.name for f in spec.fields}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise InputRejected(
            "This workflow does not take "
            + ", ".join(f"“{u}”" for u in unknown)
            + ". It takes: "
            + (", ".join(sorted(known)) if known else "no inputs")
            + ".")

    cleaned: Dict[str, Any] = {}
    for f in spec.fields:
        present = f.name in raw and raw[f.name] not in (None, "")
        if not present:
            if f.required:
                raise InputRejected(f"“{f.name}” is required. {f.description}".strip())
            continue
        cleaned[f.name] = _coerce(f, raw[f.name])

    size = len(encode_payload(cleaned))
    if size > MAX_INPUT_BYTES:
        raise InputRejected(
            f"The inputs are {size} bytes; the maximum is {MAX_INPUT_BYTES}.")
    return cleaned

def encode_payload(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class ConfigError(ValueError):
    """The n8n configuration cannot be used, and this says exactly why."""

class ConnectionState(str, Enum):
    DISABLED = "disabled"          # the toggle is off
    NOT_CONFIGURED = "not_configured"
    UNREACHABLE = "unreachable"    # network/connection refused
    UNAUTHORIZED = "unauthorized"  # n8n rejected the credential
    MISCONFIGURED = "misconfigured"  # URL/token shape is wrong
    READY = "ready"                # a webhook answered
    ERROR = "error"

#: Loopback hosts that local-only mode allows. ``localhost`` and the two literal
#: v4/v6 loopback addresses; not a hostname suffix, so a DNS-rebinding name that
#: *ends* in "localhost" is not silently accepted.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}

def normalize_base_url(raw: str, *, allow_remote: bool = False) -> str:
    """Validate and normalise the n8n base URL, or raise ``ConfigError``.

    Only ``http``/``https`` is accepted: a ``file:`` or ``ftp:`` URL is not
    something an HTTP client can speak, and accepting it defers the failure to a
    confusing error later. The path is kept (some deployments sit behind a
    prefix) but a query or fragment is refused, because they cannot be combined
    with a webhook path without producing a different address than intended.
    """
    text = (raw or "").strip()
    if not text:
        raise ConfigError("No n8n URL is configured.")
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in ("http", "https"):
        raise ConfigError(
            f"The n8n URL must start with http:// or https:// (got {text!r}).")
    if not parsed.hostname:
        raise ConfigError(f"The n8n URL has no host ({text!r}).")
    if parsed.query or parsed.fragment:
        raise ConfigError("The n8n URL must not carry a query string or fragment.")
    if not allow_remote and parsed.hostname.lower() not in _LOOPBACK_HOSTS:
        raise ConfigError(
            f"The n8n URL points at {parsed.hostname!r}, which is not this machine. "
            "Set JARVIS_N8N_ALLOW_REMOTE=true only if that is deliberate — Vantage "
            "will not send a credential to a remote host by default.")
    # Strip a trailing slash so path joining is unambiguous, and never append a
    # second /v1 (n8n's webhooks do not live under /v1 at all).
    path = parsed.path.rstrip("/")
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.netloc.split('@')[-1]}{path}" if "@" in parsed.netloc \
        else f"{parsed.scheme}://{parsed.hostname}{port}{path}"

@dataclass
class N8NConfig:
    """Where n8n lives and what Vantage is allowed to ask it to do.

    ``api_key`` is not serialised by :meth:`public`; every status payload is
    built from that method.
    """

    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    #: Header shape for the credential. n8n webhooks may use Basic, a bearer
    #: token, or a custom header, depending on how the workflow authenticates.
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "
    timeout: float = DEFAULT_TIMEOUT
    allow_remote: bool = False
    #: Which registry keys are enabled. Empty means "all entries in REGISTRY",
    #: which is itself a closed list; this narrows it further.
    allowed_keys: List[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return bool((self.base_url or "").strip())

    def permits(self, key: str) -> bool:
        if not self.enabled:
            return False
        return key in self.allowed_keys if self.allowed_keys else key in _BY_KEY

    def public(self) -> Dict[str, Any]:
        """The safe description: never the key, never a credential in the URL."""
        return {
            "enabled": self.enabled,
            "base_url": redact((self.base_url or "").strip()),
            "configured": self.configured,
            "has_api_key": bool(self.api_key),
            "timeout": self.timeout,
            "allow_remote": self.allow_remote,
            "allowed_keys": list(self.allowed_keys) if self.allowed_keys else workflow_keys(),
        }

def parse_allowed_keys(raw: str) -> List[str]:
    """Comma-separated registry keys. Unknown keys are *kept* but reported.

    Keeping them is deliberate: an operator who typo'd a key should see the
    typo in the status payload rather than have it silently dropped and wonder
    why a workflow is unreachable. Execution still looks them up in the
    registry, so a typo can never widen access.
    """
    seen: List[str] = []
    for part in (raw or "").split(","):
        key = part.strip().lower()
        if key and key not in seen:
            seen.append(key)
    return seen

def config_from_settings(settings: Any) -> N8NConfig:
    """Build the config from ``Settings``, overlaid with the saved Connections file.

    The saved file wins over a shell export for the same reason MCP's does: the
    screen is the documented path, and a leftover environment variable must not
    silently override a choice the person made in the app.
    """
    saved: Dict[str, Any] = {}
    try:
        from ..gateway.selection import n8n_config

        saved = n8n_config(getattr(settings, "home", None))
    except Exception:
        saved = {}

    def pick(name: str, attr: str) -> Any:
        value = saved.get(name)
        if value in (None, ""):
            value = getattr(settings, attr, None)
        return value

    enabled = bool(pick("enabled", "n8n_enabled"))
    base_url = str(pick("base_url", "n8n_base_url") or "").strip()
    timeout = float(pick("timeout", "n8n_timeout") or DEFAULT_TIMEOUT)
    allow_remote = bool(pick("allow_remote", "n8n_allow_remote"))

    api_key = str(getattr(settings, "n8n_api_key", "") or "")
    if not api_key:
        try:
            from ..security.providers import KeychainStorage

            api_key = KeychainStorage.retrieve("n8n") or ""
        except Exception:
            api_key = ""

    return N8NConfig(
        enabled=enabled,
        base_url=base_url,
        api_key=api_key,
        auth_header=str(pick("auth_header", "n8n_auth_header") or "Authorization"),
        auth_prefix=str(pick("auth_prefix", "n8n_auth_prefix")
                        if saved.get("auth_prefix") is None else saved["auth_prefix"]),
        timeout=min(max(timeout, 1.0), MAX_TIMEOUT),
        allow_remote=allow_remote,
        allowed_keys=parse_allowed_keys(
            str(pick("allowed_keys", "n8n_allowed_workflows") or "")),
    )

def validate_config(config: N8NConfig) -> Dict[str, Any]:
    """Return a status payload describing whether this config can be used.

    Never raises: a status endpoint must answer even for a config that cannot
    work, because "what is wrong with it" is the answer the person needs.
    """
    state = ConnectionState.READY
    problems: List[str] = []
    if not config.enabled:
        state = ConnectionState.DISABLED
        problems.append("The n8n integration is turned off.")
    elif not config.configured:
        state = ConnectionState.NOT_CONFIGURED
        problems.append("No n8n URL is configured.")
    else:
        try:
            normalize_base_url(config.base_url, allow_remote=config.allow_remote)
        except ConfigError as exc:
            state = ConnectionState.MISCONFIGURED
            problems.append(str(exc))
    unknown = [k for k in config.allowed_keys if k not in _BY_KEY]
    if unknown:
        problems.append(
            "These allowlist entries are not approved workflows and will be ignored: "
            + ", ".join(unknown) + ".")
    return {
        "state": state.value,
        "config": config.public(),
        "problems": problems,
        "approved_keys": workflow_keys(),
    }

# ---------------------------------------------------------------------------
# Correlation ids
# ---------------------------------------------------------------------------
#
# Every execution carries one, so a log line in Vantage can be matched to an
# execution in n8n. It is generated here, never accepted from a request: a
# caller-supplied id is not identity, and accepting one lets a client claim a
# different execution's history.

_CORR = re.compile(r"^[a-z0-9]{6,40}$")

def new_correlation_id() -> str:
    import secrets

    return "v8n-" + secrets.token_hex(8)

def valid_correlation_id(value: str) -> bool:
    return bool(_CORR.match((value or "").strip()))