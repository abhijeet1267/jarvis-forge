"""FastAPI surface for Vantage (section 37, Phase 0).

Exposes the orchestrator, registries, audit log and memory over HTTP so the
dashboard, the CLI and (later) the phone/watch companions all talk to the same
permissioned core.
"""

from __future__ import annotations

import asyncio
import urllib.parse
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, field_validator

from .. import __version__
from ..config import get_settings, llm_source
from ..gateway.providers import (PROVIDERS, ProviderUnknown, provider_capabilities,
                                 select_provider)
from ..core.models import CommandRequest, CommandResponse, PlannedAction, PolicyDecision
from ..core.orchestrator import JarvisCore
from ..logging_setup import get_logger
from ..security.auth import (
    TOKEN_HEADER,
    describe_security,
    get_rate_limiter,
    get_token_store,
    host_allowed,
    lan_startup_problems,
)
from ..security.providers import (
    create_provider_table,
    get_credentials,
    list_providers,
    save_credential_ref,
    update_credential_status,
    KeychainStorage,
)
from ..security.privacy import PrivacyManager, describe_privacy
from ..permissions import describe_permissions
from ..devices.pairing import HostDeviceError, PairingManager
from ..registry.devices import HOST_DEVICE_ID
from ..transport.server import TransferServer
from ..transport.tls import describe_tls
from ..core.models import MemoryScope, MemoryType, RiskLevel, utcnow
from ..scheduler.store import ScheduleStore
from ..db.database import get_db
from ..integrations import describe_calendar, describe_email, get_calendar_provider, get_email_provider
from ..memory.embeddings import describe_embeddings
from ..memory.hybrid import describe_retrieval
from ..research.sources import describe_sources

log = get_logger("api")

# Install the access-log scrubber here, at import, not only in run.py/run().
# The macOS app launches `uvicorn jarvis.api.app:app` (BackendManager.swift),
# and the packaged .app uses app/launcher.py -> run.py; only the latter two call
# `install()`. Measured on that exact launch path: hitting /google/callback with
# a code wrote `code=SECRET...` verbatim to the log. Importing this module is
# the one point every launch path shares, and the filter is idempotent.
try:
    from ..log_safety import install as _install_log_safety

    _install_log_safety()
except Exception:  # a logging nicety must never stop the API from importing
    log.warning("access-log credential scrubbing could not be installed")

_settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Refuse an unsafe LAN configuration, then materialise the API token.

    LAN mode is the setting that lets another device run things on this laptop, so the
    configuration is checked here rather than trusted: a token-less or loopback-bound
    start would either open a remote shell or silently fail to connect anything.

    Without eager token generation the token would only be produced on the first
    *successful* verification -- which can never happen, because nobody can know the token
    yet. Generating it here breaks that deadlock and lets the user read it from disk.
    """
    settings = get_settings()
    problems = lan_startup_problems(settings)
    if problems:
        for problem in problems:
            log.error("refusing to start", extra_fields={"lan_mode_problem": problem})
        raise RuntimeError(" ".join(problems))

    if settings.auth_enabled:
        token_path = settings.token_path
        get_token_store().token()
        log.info("api token ready", extra_fields={"path": str(token_path)})
        # flush: stdout is a pipe for the desktop launcher, where it would otherwise
        # sit in a block buffer and never reach the log the user is told to read.
        print(f"Vantage API token -> {token_path}", flush=True)

    # A phone cannot guess the laptop's address, and `0.0.0.0` is not one.
    for url in settings.reachable_urls():
        print(f"Phone client           -> {url}", flush=True)

    # The documentation switch is opt-in because the pages take no credential, and with
    # lan_mode on this machine's own Wi-Fi address is an accepted Host header. Saying it at
    # start is the least the process can do: an open door should be visible from outside.
    if settings.api_docs:
        print("API docs               -> /openapi.json, unauthenticated, on every "
              "address listed above", flush=True)

    # v0.8: run scheduled jobs in the background while the server is up.
    core = get_core()
    # Before anything can start work here: a `running` job row from a previous process is a
    # statement about a process that no longer exists, and three readers (the panel's task card,
    # "what are you doing", and the emergency stop) take it at face value. See
    # TaskManager.reconcile_after_restart for what that cost when it was left stale.
    core.tasks.reconcile_after_restart()
    if settings.scheduler_enabled:
        await core.scheduler.start(core)

    # Warm the microphone probe now, on a background thread. Measuring can mean
    # compiling Swift and waiting out macOS's first-launch assessment, and the
    # first /voice/status of the session would otherwise report "still
    # measuring" -- which the app's own health check reads as a failure.
    try:
        from ..voice.audio import get_mic_probe

        get_mic_probe().read()
    except Exception as exc:  # never block startup on a peripheral probe
        log.warning("microphone warm-up probe failed", extra_fields={"error": str(exc)})

    # v0.9: backfill vectors for memories stored before this version. Without
    # it, semantic search would only ever see newly written memories.
    try:
        from ..memory.vector_store import VectorIndex

        index = VectorIndex()
        pending = index.stats()["pending"]
        if pending:
            indexed = index.index_all()
            log.info("memory vectors backfilled", extra_fields={"indexed": indexed})
    except Exception as exc:
        # Vector search is an enhancement; never block startup on it.
        log.info("could not backfill memory vectors", extra_fields={"error": str(exc)})

    # Companion gateway: a separate TLS process on :8766 for Android pairing,
    # owned by the backend so it cannot outlive it. Off unless LAN mode is on;
    # a failed start is logged + reported via /companion/status, never fatal to
    # the core (the backend is the product; the gateway is the bridge).
    try:
        from ..companion.server import get_companion_server
        gw = get_companion_server(settings)
        status = await asyncio.to_thread(gw.start)
        if status.running:
            print(f"Companion gateway  -> https://0.0.0.0:{status.port} (TLS)", flush=True)
            log.info("companion gateway started", extra_fields={"port": status.port})
        else:
            log.info("companion gateway not running", extra_fields={"reason": status.detail})
    except Exception as exc:  # optional bridge must not take down the core
        log.warning("companion gateway start error", extra_fields={"error": str(exc)})

    try:
        yield
    finally:
        await core.scheduler.stop()
        # Tear the gateway down with the backend: a stopped core must not leave a
        # 0.0.0.0:8766 listener proxying to a dead backend. Managed in the
        # background so a slow stop cannot delay the shutdown handlers.
        try:
            from ..companion.server import get_companion_server
            await asyncio.to_thread(get_companion_server(settings).stop)
        except Exception as exc:  # shutdown must not fail on the optional gateway
            log.warning("companion gateway shutdown error", extra_fields={"error": str(exc)})


def doc_routes(settings) -> Dict[str, Any]:
    """The kwargs that decide whether FastAPI publishes this API's blueprint.

    `docs_url`/`redoc_url`/`openapi_url` are read once, at construction, so the answer has to
    be a dict rather than a per-request check -- and the keyword names are part of the
    contract: a misspelled one is not an error, it is a default that still mounts the route.
    """
    if settings.api_docs:
        return {}
    return {"docs_url": None, "redoc_url": None, "openapi_url": None}


app = FastAPI(
    title="Vantage Core",
    version=__version__,
    description="Permissioned multi-agent personal AI operating system (v0.3 core).",
    lifespan=lifespan,
    **doc_routes(_settings),
)

# Only the dashboard's own origin may read responses. A wildcard here would let
# any page the user visits drive the API and read the results.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.allowed_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", TOKEN_HEADER, "Authorization"],
)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    """Host validation + rate limiting, applied before any handler runs."""
    settings = get_settings()

    # 1. Anti DNS-rebinding: the Host header must name a loopback address.
    if not host_allowed(request.headers.get("host"), settings):
        log.info("rejected non-loopback host", extra_fields={"host": request.headers.get("host")})
        return JSONResponse(
            status_code=400,
            content={"detail": "Host header must be a loopback address."},
        )

    # 2. Rate limit per client address.
    client = request.client.host if request.client else "unknown"
    allowed, retry_after = get_rate_limiter().check(client)
    if not allowed:
        log.info("rate limited", extra_fields={"client": client})
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests. Slow down."},
            headers={"Retry-After": str(int(retry_after) + 1)},
        )

    response = await call_next(request)

    # Baseline hardening headers.
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


# ---------------------------------------------------------------------------
# Dashboard credentials: bootstrap codes and session tokens
#
# The master API token is never sent over HTTP. It lives in
# ``$JARVIS_HOME/api_token`` (0600) and in this process, and nothing below
# reveals it. What the dashboard gets instead is a per-page-load credential:
# ``/dashboard`` issues a single-use bootstrap code with a short TTL, the page
# spends that code exactly once at ``POST /auth/session``, and is handed a
# session token with a longer TTL. ``require_auth`` accepts a live session token
# alongside the master token, so the dashboard works exactly as before from the
# user's point of view.
#
# Both tables are in-memory and bounded: they die with the process and can be
# revoked wholesale, which is more than can be said for the master token.
#
# A session also carries the **surface** that minted the code it was exchanged
# for (Phase 58). Until then a phone that completed ``/auth/session`` held a
# token reaching every protected route -- ``/privacy/delete``, ``/audit``,
# memory -- although the phone page calls ten paths and nothing else. The
# surface is fixed by the route that issues the code, not by the redeem request
# body, so a handset cannot ask for a wider one: it decides what it is allowed
# to do before it holds anything.
# ---------------------------------------------------------------------------
BOOTSTRAP_TTL_SECONDS = 60
SESSION_TTL_SECONDS = 8 * 60 * 60

#: Hard ceiling on live credentials, so nothing here grows without bound.
MAX_DASHBOARD_CREDENTIALS = 512

#: ``bootstrap code -> (expiry, surface)`` and ``session token -> (expiry, surface, ref)``.
# The ref is what a laptop uses to end one session; the token itself is never listed,
# echoed in an error, or written to the audit log (Phase 10's finding, applied to sessions).
_bootstraps: Dict[str, Tuple[float, str]] = {}
_sessions: Dict[str, Tuple[float, str, str]] = {}
# A session the operator *explicitly* ended (sign-out, revoke-one) is recorded here so a
# confirmation it had raised can no longer be approved. Popping it from _sessions alone is
# not enough: a pending confirmation still names the session, and an approver could otherwise
# resolve it after the session that raised it is gone. Token-keyed and bounded, like the
# credential stores; a token is never echoed, only ever matched.
_revoked_sessions: Dict[str, float] = {}
_credential_lock = threading.Lock()

#: What a phone session may reach: every path ``dashboard/phone.html`` actually
#: calls, read out of that file rather than remembered. Anything else on the API
#: is refused before its handler runs. ``/confirm/{id}`` is a prefix because the
#: id is part of the path.
PHONE_ALLOWED = frozenset({
    # Read-only build/process identity for the backend. The gateway already
    # forwards /health; identity lets the phone tell a stale backend from a
    # current one instead of treating any HTTP answer as healthy. It is
    # unauthenticated on the backend and exposes no secret, and nothing here can
    # change state -- so the surface is not widened, only made diagnosable.
    "/health/identity",
    "/command",
    "/confirmations",
    "/devices",
    "/devices/pair/request",
    "/emergency_stop",
    "/tools",
    "/voice/status",
    "/voice/transcribe",
    # Google reads only. The phone could already obtain this data by asking
    # /command ("what is on my calendar today?"), which is on this list and can
    # run arbitrary tool actions -- so a structured read route does not widen
    # what a phone can learn, it only makes it usable as a screen.
    #
    # Deliberately NOT here: /google/connect, /google/callback and
    # /google/disconnect. Those are SameOriginProtected and must stay
    # unreachable from the phone surface; a phone must never start or tear down
    # the Mac's Google authorisation.
    "/google/status",
    "/google/calendar/events",
    "/google/tasks",
})
PHONE_ALLOWED_PREFIXES = ("/confirm/",)

#: Only the phone surface is narrowed. The dashboard is the operator's console on
#: the machine itself, and widening or narrowing it is a decision with a reason
#: attached, not a detail to tidy away -- recorded as 58-A.
NARROW_SURFACES = frozenset({"phone"})


#: Reachable from any surface, because neither act can widen one. Exchanging a
#: bootstrap for a session, and ending the session you already hold, must stay
#: usable by the narrowest token: a phone that cannot sign out is a phone whose
#: owner has no way to stop it being authorised.
SURFACE_INDEPENDENT = frozenset({"/auth/session", "/auth/token"})


def _path_allowed(surface: str, path: str) -> bool:
    if surface not in NARROW_SURFACES:
        return True
    if path in SURFACE_INDEPENDENT:
        return True
    if path in PHONE_ALLOWED:
        return True
    return any(path.startswith(prefix) for prefix in PHONE_ALLOWED_PREFIXES)


def _issue_credential(table: Dict[str, Any], ttl: float, surface: str = "") -> str:
    """Create an unguessable credential that expires in ``ttl`` seconds."""
    code = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _credential_lock:
        for stale in [key for key, record in table.items() if _expiry_of(record) <= now]:
            table.pop(stale, None)
        while len(table) >= MAX_DASHBOARD_CREDENTIALS:
            # Dicts keep insertion order, so this drops the oldest live entry.
            table.pop(next(iter(table)))
        if table is _sessions:
            table[code] = (now + ttl, surface, secrets.token_urlsafe(8))
        else:
            table[code] = (now + ttl, surface)
    return code


def _expiry_of(record: Any) -> float:
    """Accept either a bare expiry or a ``(expiry, ...)`` record."""
    return record[0] if isinstance(record, tuple) else record


def _redeem_credential(table: Dict[str, Any], code: Optional[str]) -> bool:
    """True when ``code`` is live. It is removed either way: single use."""
    return _redeem_with_surface(table, code) is not None


def _redeem_with_surface(table: Dict[str, Any], code: Optional[str]) -> Optional[str]:
    """The surface the code was minted for, or None. Removed either way: single use."""
    if not code:
        return None
    with _credential_lock:
        record = table.pop(code, None)
    if record is None or _expiry_of(record) <= time.monotonic():
        return None
    return record[1] if isinstance(record, tuple) and len(record) > 1 else ""


def _session_is_live(code: Optional[str]) -> bool:
    """True for a session token that has not expired. Leaves it in place."""
    return _session_record(code) is not None


def _session_record(code: Optional[str]) -> Optional[Tuple[float, str, str]]:
    if not code:
        return None
    with _credential_lock:
        record = _sessions.get(code)
    if record is None or _expiry_of(record) <= time.monotonic():
        return None
    return record


def _surface_of(token: Optional[str]) -> Optional[str]:
    """Which surface a live session belongs to. None when there is no such session."""
    record = _session_record(token)
    return record[1] if record else None


def _record_session_revocation(token: str) -> None:
    """Remember that a session was explicitly ended, so a confirmation it raised can
    no longer be approved. Bounded like the credential stores: a long-lived server must
    not accumulate one entry per session forever. Tokens are unguessable and never
    echoed, so keying by token leaks nothing."""
    with _credential_lock:
        _revoked_sessions[token] = time.monotonic()
        while len(_revoked_sessions) >= MAX_DASHBOARD_CREDENTIALS:
            _revoked_sessions.pop(next(iter(_revoked_sessions)))


def _is_session_revoked(token: Optional[str]) -> bool:
    """True if the session was explicitly ended by sign-out / revoke-one."""
    if not token:
        return False
    with _credential_lock:
        return token in _revoked_sessions


def _revoke_session(token: Optional[str]) -> bool:
    """End a session and remember it was ended, so a confirmation it raised can no
    longer be approved.

    Returns whether the session was *live* (and is now gone). The revocation is
    recorded even for a token that has already expired: "this session is over" is an
    assertion about that session id, and recording it is only ever restrictive -- it
    can only block a dangling approval, never grant one. This function is internal
    (master-token-protected sign-out / revoke routes only), so an attacker cannot
    reach it to poison the set.
    """
    if not token:
        return False
    with _credential_lock:
        revoked = _sessions.pop(token, None) is not None
    _record_session_revocation(token)
    return revoked


def _revoke_session_by_ref(ref: str) -> bool:
    """End one session by the reference a laptop was given -- never by its token.

    Like sign-out, this records the revocation: the laptop revoking a phone is exactly
    the case where that phone's pending confirmations must stop being approvable, so
    the two revoke paths must behave the same or the property holds only for
    whichever one a person happens to reach.
    """
    with _credential_lock:
        revoked_token = None
        for token, record in list(_sessions.items()):
            if len(record) > 2 and record[2] == ref:
                revoked_token = token
                _sessions.pop(token, None)
                break
    if revoked_token is not None:
        # Recorded outside the lock: _record_session_revocation takes it itself, and
        # _credential_lock is a plain (non-reentrant) Lock.
        _record_session_revocation(revoked_token)
        return True
    return False


def _session_list() -> list:
    """Live sessions, identified without disclosing anything usable.

    A person deciding which handset to cut off needs the surface, the age and a
    reference they can act on. The token would let the list itself be a
    credential, so it is not in here.
    """
    now = time.monotonic()
    rows = []
    with _credential_lock:
        for token, record in _sessions.items():
            expiry = _expiry_of(record)
            if expiry <= now:
                continue
            rows.append({
                "session_ref": record[2] if len(record) > 2 else "",
                "surface": record[1] if len(record) > 1 else "",
                "expires_in": max(0, int(expiry - now)),
            })
    return rows


def clear_dashboard_credentials() -> None:
    """Revoke every outstanding bootstrap and session token."""
    with _credential_lock:
        _bootstraps.clear()
        _sessions.clear()


#: The cookie Phase 71's auth flow sets: an HttpOnly session credential the phone page
#: sends back via `credentials: "include"`. The session token lives here (not in a JS
#: global) so a page script cannot read it; this is the field the server must read to
#: honour that credential.
SESSION_COOKIE = "jarvis_session"


def _presented_credential(request: Request) -> str:
    presented = request.headers.get(TOKEN_HEADER)
    if not presented:
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            presented = authorization[7:]
    if not presented:
        # Phase 71: the phone page sends the session in an HttpOnly cookie rather than a
        # JS-readable variable, so a request that carries no header is not necessarily
        # unauthorised -- it may be carrying the credential the browser attached for it.
        # The cookie holds a session token exactly like the header path does, and is
        # validated by the same `_session_record` lookup downstream, so no new trust is
        # introduced, only a second (HttpOnly) way to present the same credential.
        presented = request.cookies.get(SESSION_COOKIE) or ""
    return presented or ""


def require_master_token(request: Request) -> None:
    """Laptop-only routes: the master token, and never a session.

    Managing sessions is a console act. A phone session is refused here rather
    than by remembering to add this path to a table, so the two rules cannot
    drift apart. Defined before the routes that use it, because Python evaluates
    a default argument when the ``def`` runs.
    """
    settings = get_settings()
    if not settings.auth_enabled:
        return
    presented = _presented_credential(request)
    if _session_record(presented) is not None:
        raise HTTPException(status_code=403, detail="A session cannot manage sessions.")
    if not get_token_store().verify(presented):
        raise HTTPException(status_code=401, detail="Missing or invalid API token.")


async def require_auth(request: Request) -> None:
    """Dependency that enforces the API token on protected routes."""
    settings = get_settings()
    if not settings.auth_enabled:
        return

    presented = _presented_credential(request)

    # A dashboard session token is as good as the master token here -- it is
    # shorter-lived, revocable, and never touches disk. Good for the surface it
    # was issued to, which is the part Phase 58 adds: a phone's token is not a
    # laptop's token, and the difference is enforced at this one gate rather than
    # remembered by every route behind it.
    record = _session_record(presented)
    if record is not None:
        surface = record[1] if len(record) > 1 else ""
        if not _path_allowed(surface, request.url.path):
            _refuse_outside_surface(surface, request.url.path, presented)
        return

    if not get_token_store().verify(presented):
        log.info("rejected unauthenticated request", extra_fields={"path": request.url.path})
        raise HTTPException(status_code=401, detail="Missing or invalid API token.")


def _refuse_outside_surface(surface: str, path: str, token: str) -> None:
    """Refuse, record, and name the surface -- without repeating the credential."""
    log.info("rejected out-of-surface call",
            extra_fields={"path": path, "surface": surface})
    try:
        get_core().audit.record(
            intent="scope refusal",
            tool="",
            target=path,
            risk="read",
            result="denied",
            detail={"surface": surface,
                    "reason": "outside what this surface can reach"},
        )
    except Exception:  # a refusal is never made less safe by a failing ledger
        log.warning("scope refusal could not be audited", extra_fields={"path": path})
    raise HTTPException(
        status_code=403,
        detail=(f"This {surface} is not allowed to reach {path}. The laptop can; "
                f"this session cannot."),
    )


#: Shorthand used on every route that can read state or cause an action.
Protected = Depends(require_auth)


def _refuse_unless_same_origin(request: Request, settings: Any) -> None:
    """Refuse anything that is not a same-origin browser request.

    The check used to be conditional -- ``if origin and origin not in allowed``
    -- which meant a request that simply omitted ``Origin`` was never checked at
    all, and a header-less ``curl`` was handed the master token. A missing
    header is now refused like a foreign one.

    This is one layer, not the boundary: a local process *can* set the header.
    The boundary is that no route discloses the master token, and that a
    scraped bootstrap buys only a short-lived, revocable session.
    """
    origin = request.headers.get("origin")
    if origin is None:
        # Older WebKit omits ``Origin`` on some same-origin requests. It still
        # sets ``Sec-Fetch-Site`` itself, which a web page cannot forge.
        if request.headers.get("sec-fetch-site", "").lower() != "same-origin":
            raise HTTPException(status_code=403, detail="Same-origin request required.")
    elif origin.lower() not in {o.lower() for o in settings.allowed_origins()}:
        # Case-insensitive because the host part of a URL is: a browser lowers the name
        # it was given before sending `Origin`, so an exact string match refused this
        # laptop's own page -- 403 on /auth/session, then 401 on everything after it.
        raise HTTPException(status_code=403, detail="Origin not allowed.")


async def require_same_origin_auth(request: Request) -> None:
    """Same-origin first, then credentials.

    The order is deliberate: were ``require_auth`` to run first, a foreign page
    could read the difference between 401 and 403 as an oracle for whether a
    token it guessed is live.
    """
    _refuse_unless_same_origin(request, get_settings())
    await require_auth(request)


#: Routes that hand out dashboard credentials.
SameOriginProtected = Depends(require_same_origin_auth)

# Mission routes live in their own module so this file stops growing. The auth
# dependency is passed in rather than imported there, which keeps the import
# acyclic and makes "every mission route is protected" a fact of construction.
from .missions_routes import build_router as _build_missions_router  # noqa: E402

app.include_router(_build_missions_router(require_auth))

# ParallelWorld's preview/apply surface is not mission-scoped: it is the general
# "show me the change before you make it" capability, so it has its own router.
from .files_routes import build_router as _build_files_router  # noqa: E402

app.include_router(_build_files_router(require_auth))

# IntentShield / skills / journal: read-mostly inspection surfaces.
from .intelligence_routes import build_router as _build_intelligence_router  # noqa: E402

app.include_router(_build_intelligence_router(require_auth))

# Project Doctor: read-only inspection, repairs proposed for ParallelWorld.
from .doctor_routes import build_router as _build_doctor_router  # noqa: E402

app.include_router(_build_doctor_router(require_auth))

# Mac Control: launch applications and report what was actually observed.
from .mac_routes import build_router as _build_mac_router  # noqa: E402

app.include_router(_build_mac_router(require_auth))

# Saved Workspaces: a named set of apps, files and URLs, launched and observed.
from .workspace_routes import build_router as _build_workspace_router  # noqa: E402

app.include_router(_build_workspace_router(require_auth))

# Agent Control Tower: what the workers can do, what they are doing, and a stop
# that is enforced at the next dispatch boundary.
from .agent_routes import build_router as _build_agent_router  # noqa: E402

app.include_router(_build_agent_router(require_auth))

# Recovery Tournament: rank the safe repairs for a failure, read-only.
from .recovery_routes import build_router as _build_recovery_router  # noqa: E402

app.include_router(_build_recovery_router(require_auth))

# Privacy Egress Firewall: which destinations are declared, read-only.
from .egress_routes import build_router as _build_egress_router  # noqa: E402

app.include_router(_build_egress_router(require_auth))

# Adaptive Autonomy Control: the dial, its ceiling, and a read-only preview.
from .autonomy_routes import build_router as _build_autonomy_router  # noqa: E402

app.include_router(_build_autonomy_router(require_auth))

# Outcome Assurance: what can be verified beyond the filesystem, read-only.
from .assurance_routes import build_router as _build_assurance_router  # noqa: E402

app.include_router(_build_assurance_router(require_auth))

# Temporal Undo Map: the timeline of effects and a real, single undo.
from .undo_routes import build_router as _build_undo_router  # noqa: E402

app.include_router(_build_undo_router(require_auth))

_core: Optional[JarvisCore] = None
_voice = None


def get_core() -> JarvisCore:
    """Lazily build the single core instance."""
    global _core
    if _core is None:
        _core = JarvisCore()
    return _core


def get_voice():
    """Lazily build the single voice session (Phase 2)."""
    global _voice
    if _voice is None:
        from ..voice import VoiceSession

        _voice = VoiceSession(get_core())
    return _voice


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------
# The body models live in ``jarvis/api/schemas.py`` so the API layer owns its request
# contract in one place, and so a route module can import a model without importing this
# whole application (which would be a circular import). They are re-exported here so
# ``from jarvis.api.app import CommandBody`` keeps working.
from .schemas import (  # noqa: E402  (re-export; see jarvis/api/schemas.py)
    BootstrapBody,
    CommandBody,
    MemoryBody,
    VoiceTextBody,
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
async def root() -> Dict[str, Any]:
    index: Dict[str, Any] = {
        "name": "Vantage Core",
        "version": __version__,
        "dashboard": "/dashboard",
        "tools": len(get_core().tools.names()),
    }
    # A signpost on an unauthenticated route to a page that is only there because someone
    # asked for it. When they did not, the key is absent rather than set to null: the index
    # describing a closed door would still be describing a door.
    if get_settings().api_docs:
        index["docs"] = "/docs"
    return index


def describe_peripherals() -> Dict[str, Any]:
    """How each hardware-facing agent stands, without touching the hardware.

    Every gateway reports its own reachability, and the whole point of those
    reports is that they distinguish "not configured" from "configured but not
    answering" from "answered and here is the value". A flat online/offline flag
    on the devices page would throw that away, so the dashboard reads this
    instead. Each entry is independently guarded: one unreachable adapter must
    never take /health down with it.
    """
    out: Dict[str, Any] = {}

    def safe(name: str, produce) -> None:
        try:
            out[name] = produce()
        except Exception as exc:  # pragma: no cover - a peripheral is broken, not the API
            out[name] = {"error": str(exc)[:160]}

    from ..car.gateway import get_car_gateway
    from ..home.gateway import get_home_gateway
    from ..robotics.gateway import get_robot_gateway
    from ..watch.gateway import get_watch_gateway

    safe("car", get_car_gateway().posture)

    def _home() -> Dict[str, Any]:
        s = get_home_gateway().status()
        return {"online": s.get("online"), "mode": s.get("mode"),
                "devices": s.get("device_count"), "simulated": s.get("simulated"),
                #: Present only when something was actually tried and failed.
                #: Without it, "offline" cannot be told apart from "not answering".
                "bridge_error": s.get("bridge_error") or ""}

    def _robots() -> Dict[str, Any]:
        s = get_robot_gateway().status()
        return {"online": s.get("online"), "mode": s.get("mode"),
                "simulated": s.get("simulated"), "robots": s.get("robot_count"),
                "faults": s.get("faults"), "bridge_error": s.get("bridge_error") or ""}

    safe("home", _home)
    safe("robotics", _robots)

    def _watch() -> Dict[str, Any]:
        s = get_watch_gateway().status()
        return {key: s[key] for key in
                ("paired", "auth_state", "mode", "online", "battery", "counts", "awaiting_answer")}

    safe("watch", _watch)
    return out


@app.get("/companion/status")
async def companion_status(_: None = Protected) -> Dict[str, Any]:
    """The real state of the companion gateway, from the backend that owns it.

    The macOS panel asks here rather than guessing a port is up: `running` reflects
    the managed subprocess, `lan_mode` explains why it is off, and `last_error`
    carries a failed start instead of a silent no-op.
    """
    from ..companion.server import get_companion_server
    settings = get_settings()
    status = await asyncio.to_thread(get_companion_server(settings).status)
    return {
        "running": status.running,
        "port": status.port,
        "lan_mode": settings.lan_mode,
        "detail": status.detail,
    }


@app.get("/health/identity")
async def health_identity() -> Dict[str, Any]:
    """Who is answering, before any credential is checked.

    Deliberately **unauthenticated**: the one question a client must be able to
    ask before it holds a valid token is "is the process on this port actually
    Vantage?". Answering 401 to that leaves a stale backend and a wrong token
    looking identical -- which is how a stale process went unnoticed for a day.

    It discloses only build/runtime facts (version, revision, start time, pid,
    source root): no settings, no environment values, no secrets. The full
    report stays on the authenticated ``/health``.
    """
    from ..runtime.identity import identity, staleness, fingerprint

    return {"identity": identity(), "code": staleness(), "fingerprint": fingerprint()}

@app.get("/health/code")
async def code_freshness(_: None = Protected) -> Dict[str, Any]:
    """Whether the files on disk are newer than the process serving them.

    A running Vantage does not reload its modules, so a planner rule edited after
    startup is silently absent. Reporting that explicitly turns a confusing
    "still broken" into "restart needed".
    """
    from ..runtime.identity import staleness

    return staleness()


@app.get("/health")
async def health(_: None = Protected) -> Dict[str, Any]:
    core = get_core()
    settings = core.settings
    paths = provider_capabilities(settings)
    # The desktop panel polls /health, not a separate endpoint, so the
    # gateway's real state rides along: `running` is the managed subprocess's
    # actual liveness, `detail` says why when it is off.
    from ..companion.server import get_companion_server
    companion = (await asyncio.to_thread(
        get_companion_server(settings).status)).as_dict()
    return {
        "status": "ok",
        "tools": len(core.tools.names()),
        "agents": list(core.agents.keys()),
        "llm_enabled": core.settings.llm_enabled,
        # "configured" and "reachable" are not the same claim, and neither is
        # "set in the shell" and "set in a file that failed to load".
        "llm_source": llm_source(),
        # Which vendor the gateway will actually call, and which model-shaped reads go through
        # it. A badge that says "a model is configured" without naming the door would repeat
        # exactly the mistake Phase 60 found: the status page being the thing that is wrong.
        "ai_provider": paths["chat"],
        "provider_paths": paths,
        "pending_confirmations": len(core.policy.pending()),
        # The addresses another device can actually reach. The desktop panel asks the
        # server rather than deriving them itself, so "what to type" and "what the Host
        # check will accept" cannot drift apart -- that drift was a Phase 35 defect.
        "phone_urls": core.settings.reachable_urls(),
        "security": describe_security(),
        "privacy": describe_privacy(),
        "devices": {
            "pairing": PairingManager().describe(),
            "transport": describe_tls(),
            "paired_devices": len(get_core().devices.paired()),
        },
        "peripherals": describe_peripherals(),
        "integrations": {
            "email": describe_email()["active"],
            "calendar": describe_calendar()["active"],
        },
        "scheduler": get_core().scheduler.describe(),
        "memory_retrieval": describe_retrieval(),
        "embeddings": describe_embeddings(),
        "research": {"sources": [s["name"] for s in describe_sources()["approved"]]},
        "companion": companion,
    }


@app.get("/auth/token")
async def auth_token(request: Request, _: None = SameOriginProtected) -> Dict[str, Any]:
    """Issue a fresh session to an already-authenticated client.

    This route used to hand the **master API token** to any caller: its
    ``Origin`` check was conditional, so a header-less ``curl`` -- no
    ``Origin``, no token -- was never tested at all and walked away with the
    secret that authorises every tool, which also made the 0600 on
    ``$JARVIS_HOME/api_token`` moot.

    It now refuses a foreign or header-less caller *before* it will even say
    whether a credential is good, and what it returns is a session token. The
    master token never leaves the process.

    A re-issued session inherits the surface of the one that asked. Without that
    rule a phone could launder a wider token through its own narrow one, and the
    scoping would be a speed bump rather than a gate.
    """
    settings = get_settings()
    if not settings.auth_enabled:
        return {"auth_enabled": False, "token": None, "expires_in": 0}
    record = _session_record(_presented_credential(request))
    surface = record[1] if record and len(record) > 1 else "console"
    return {
        "auth_enabled": True,
        "token": _issue_credential(_sessions, SESSION_TTL_SECONDS, surface),
        "expires_in": SESSION_TTL_SECONDS,
    }


@app.post("/auth/session")
async def auth_session(request: Request, body: BootstrapBody) -> Response:
    """Spend a single-use bootstrap code for a dashboard session.

    ``/dashboard`` is still public -- it is only a shell now -- but what it
    carries is a 256-bit code that works once, for sixty seconds, and buys
    nothing but the session below. The origin gate runs *before* the code is
    consumed, so a refused caller cannot spend a page load's bootstrap out from
    under the browser that was served it; a request that clears the gate burns
    the code whether or not it matches.
    """
    settings = get_settings()
    _refuse_unless_same_origin(request, settings)
    if not settings.auth_enabled:
        return JSONResponse({
            "auth_enabled": False,
            "session_token": None,
            "expires_in": 0,
        })

    # The surface was chosen when the code was minted, by the route that minted
    # it. Whatever the body asks for is ignored, which is the point: a handset
    # decides what it is allowed to reach before it holds a credential, not
    # after. See the module's ``PHONE_ALLOWED``.
    surface = _redeem_with_surface(_bootstraps, body.bootstrap.strip())
    if surface is None:
        # Expired, already spent, or invented. The response deliberately says
        # nothing about which, so it cannot be used to probe live codes.
        log.info("rejected dashboard bootstrap", extra_fields={"path": request.url.path})
        raise HTTPException(status_code=403, detail="Bootstrap refused.")

    session_token = _issue_credential(_sessions, SESSION_TTL_SECONDS, surface)
    
    response = JSONResponse({
        "auth_enabled": True,
        "session_token": session_token,
        "expires_in": SESSION_TTL_SECONDS,
        "surface": surface,
    })
    
    # Set session as HttpOnly cookie — prevents JavaScript access via window.token
    # This closes Phase 71 credential exposure gap
    response.set_cookie(
        key="jarvis_session",
        value=session_token,
        httponly=True,
        samesite="lax",  # CSRF protection without breaking simple GET requests
        max_age=SESSION_TTL_SECONDS,
        path="/",
        secure=False,  # False for localhost/dev; production should use HTTPS
    )
    
    return response


@app.delete("/auth/session")
async def auth_signout(request: Request, _: None = Protected) -> Dict[str, Any]:
    """End this caller's own session.

    A phone being wiped, sold or handed to a child should stop being authorised,
    and doing that should not require revoking every credential on the laptop.
    The answer is the store re-read after the call rather than the call's own
    report, because that is the rule this project keeps having to enforce.
    """
    token = _presented_credential(request)
    if _session_record(token) is None:
        raise HTTPException(status_code=401,
                            detail="No live session to end. The master token is not a session.")
    _revoke_session(token)
    return {"revoked": _session_record(token) is None}


@app.get("/auth/sessions")
async def auth_sessions(_: None = Depends(require_master_token)) -> list:
    """Live sessions with their surface and remaining time -- and no token.

    ``/auth/token`` is this file's cautionary tale: a route that returned the
    credential itself. A list handed to a person so they can decide which handset
    to cut off must identify sessions without becoming a credential store.
    """
    return _session_list()


@app.delete("/auth/sessions/{session_ref}")
async def auth_revoke_one(session_ref: str,
                          _: None = Depends(require_master_token)) -> Dict[str, Any]:
    """End exactly one session, by the reference the list publishes."""
    if not _revoke_session_by_ref(session_ref):
        raise HTTPException(status_code=404, detail="No such session reference.")
    return {"revoked": session_ref not in [row["session_ref"] for row in _session_list()]}


@app.post("/command", response_model=CommandResponse)
async def command(body: CommandBody, _: None = Protected,
                  request: Request = None) -> CommandResponse:  # type: ignore[assignment]
    """Run one natural-language command through the full pipeline.

    The device id may also arrive in the ``X-Vantage-Device-Id`` header, which
    is how the companion gateway forwards it -- the gateway replaces the
    session token with the master token, so the body is the only thing the
    backend can still read it from. The header is preferred when both are
    present: the body field is the client's own claim about which device it
    is, and the header is the one the gateway saw for itself.
    """
    core = get_core()
    device_id = body.device_id
    if request is not None:
        forwarded = request.headers.get("X-Vantage-Device-Id", "")
        if forwarded:
            device_id = forwarded
    request_model = CommandRequest(
        text=body.text,
        device_id=device_id,
        source=body.source,
        context=body.context,
    )
    if body.session_id:
        request_model.session_id = body.session_id
    return await core.handle(request_model)


@app.post("/confirm/{confirmation_id}", response_model=CommandResponse)
async def confirm(
    confirmation_id: str,
    approve: Optional[bool] = Query(None),
    session_id: str = Query(""),
    _: None = Protected,
    request: Request = None,  # type: ignore[assignment]
) -> CommandResponse:
    """Approve or deny a pending confirmation.

    ``approve`` defaults to **neither**: a decision has to be stated. It used to
    default to ``True``, and it was a query parameter only — so a JSON client that
    sent ``{"approve": false}`` in the body had the field silently ignored and the
    confirmation **approved**. Measured: a body-deny cancelled a real task.
    """
    core = get_core()

    # A decision may arrive in the query string or in the JSON body. The body is
    # what a client naturally writes, and ignoring it is what turned a deny into an
    # approval; a stated query value still wins, because callers that pass it mean
    # it explicitly.
    if approve is None and request is not None:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if isinstance(payload, dict) and isinstance(payload.get("approve"), bool):
            approve = payload["approve"]
    if approve is None:
        raise HTTPException(
            status_code=422,
            detail="Say whether to approve: approve=true or approve=false (query or body).")

    # Which surface the approver is on.
    #
    # Three sources, in this order, because a phone can arrive three ways:
    #   1. `session_id` in the query -- what a local caller sends.
    #   2. `X-Vantage-Surface` -- what the companion gateway forwards, having
    #      replaced the session token with the master token so the backend can
    #      no longer look the session up.
    #   3. the presented credential itself. A client that sends neither of the
    #      first two still authenticated *with a session token*, and that token
    #      already says which surface it belongs to. Without this the guard saw
    #      "unknown" and let a phone approve its own request -- measured.
    approver_surface = _surface_of(session_id) or ""
    if not approver_surface and request is not None:
        approver_surface = _surface_of(_presented_credential(request)) or ""
    if not approver_surface and request is not None:
        forwarded = request.headers.get("X-Vantage-Surface", "")
        if forwarded:
            approver_surface = forwarded

    # 4. (Considered and rejected.) Treating a loopback peer as "the operator"
    #    would let the local /phone page approve its own requests, which it
    #    otherwise cannot. It cannot be done safely at this layer: over HTTP the
    #    local page and a remote handset present *identical* requests -- the
    #    gateway proxies from 127.0.0.1 and swaps the session token for the
    #    master token, so when no X-Vantage-Surface is present the two are
    #    indistinguishable. Measured both ways. Between a convenience for the
    #    in-browser page and the invariant that a device may not approve its own
    #    consequential request, the invariant wins; the page's request stays
    #    pending and the dashboard or native app answers it.
    if not approver_surface:
        approver_surface = "unknown"

    return await core.resolve_confirmation(confirmation_id, approve, session_id,
                                           request_id="",
                                           approver_session=session_id,
                                           approver_surface=approver_surface)


@app.get("/confirmations")
async def confirmations(_: None = Protected) -> List[Dict[str, Any]]:
    return get_core().policy.pending()


@app.get("/devices")
async def devices(_: None = Protected) -> List[Dict[str, Any]]:
    rows = []
    for device in get_core().devices.all():
        row = device.model_dump()
        # The client is the one that must decide which row may be forgotten, and it has no
        # way to know what machine it is talking to. Say so, instead of letting the page
        # guess from a device id it would have to invent.
        row["is_host"] = device.device_id == HOST_DEVICE_ID
        rows.append(row)
    return rows


@app.get("/permissions")
async def permissions(_: None = Protected) -> Dict[str, Any]:
    """macOS permission state, with the process that answered attached to every row.

    Authenticated because it is a description of this machine's security posture; and
    read-only because a status endpoint that prompts for permissions is not a status
    endpoint. Granting anything happens in System Settings or on the feature's own path.
    """
    return describe_permissions()


@app.get("/persistence")
async def persistence_status(_: None = Protected) -> Dict[str, Any]:
    """Whether the laptop starts at login and is kept running -- read from launchd, now.

    Read-only for the same reason ``/permissions`` is: the page polls it. Installing or removing
    the login item goes through the gated capability, so it arrives with a confirmation card
    rather than as a side effect of a refresh.
    """
    from ..persistence import HostPersistenceService

    return HostPersistenceService().status()


# ---------------------------------------------------------------------------
# AI Providers (Phase 72)
# ---------------------------------------------------------------------------

from typing import List, Dict, Any
from fastapi import HTTPException, Query
from ..logging_setup import get_logger

log = get_logger("api.providers")

#: Test-status states surfaced to the UI. "untested" covers both "never tested" and
#: "tested before the config changed" (a stale result must not read as a green check).
_PROVIDER_TEST_STATES = ("not_configured", "untested", "passed", "failed")


def _provider_test_inference(cfg: Dict[str, str], *, base_url: str, model: str) -> Dict[str, Any]:
    """Turn a provider's persisted test record into a truthful state.

    A recorded result is only trusted while the resolved base URL and model still
    match what was tested. Any change folds it back to ``untested``, so a green
    check can never survive the configuration it was earned against.
    """
    if "last_test_ok" not in cfg:
        return {"state": "untested", "error": "", "at": "", "model": ""}
    ok = cfg.get("last_test_ok") == "true"
    tested_url = cfg.get("last_test_base_url", "")
    tested_model = cfg.get("last_test_model", "")
    if tested_url != base_url or tested_model != model:
        return {"state": "untested", "error": "", "at": "", "model": ""}
    return {
        "state": "passed" if ok else "failed",
        "error": cfg.get("last_test_error", ""),
        "at": cfg.get("last_test_at", ""),
        "model": tested_model,
    }


def _provider_usable(provider_cls, provider_id: str) -> bool:
    """Whether this provider has what it needs to be called.

    Usually that is a stored key. A connector that authenticates nobody (a
    loopback gateway with auth disabled) is usable without one -- its adapter
    says so with ``REQUIRES_KEY = False`` -- and waiting for a key nobody can
    provide would leave a working endpoint permanently "not configured".
    """
    return bool(KeychainStorage.retrieve(provider_id)) or not getattr(
        provider_cls, "REQUIRES_KEY", True)


def _provider_card(provider_id: str) -> Dict[str, Any]:
    """One provider's config + test state, read without doing a live call.

    ``configured`` means the provider has what it needs to be called -- a stored key,
    or (for a keyless connector) a resolvable endpoint; ``verified``/``test_status``
    come from the *persisted* last test result, so the list never fabricates a green
    state by firing a network call on every load.
    """
    from ..gateway.selection import provider_base_url, provider_model, get_provider_config

    spec = next((s for s in list_providers() if s.id == provider_id), None)
    if not spec:
        return {}
    provider_cls = PROVIDERS.get(provider_id)
    if not provider_cls:
        return {}

    settings = get_settings()
    usable = _provider_usable(provider_cls, provider_id)
    base_url = provider_base_url(settings, provider_id)
    model = provider_model(settings, provider_id)
    cfg = get_provider_config(provider_id, settings.home)
    test = _provider_test_inference(cfg, base_url=base_url, model=model)

    state = test["state"] if usable else "not_configured"
    return {
        "provider_id": spec.id,
        "display_name": spec.display_name,
        "type": spec.type.value,
        "enabled": spec.enabled,
        "configured": usable,
        "verified": state == "passed",
        "test_status": state if usable else "not_configured",
        "last_verified_at": test["at"] if state == "passed" else "",
        "last_error": test["error"] if state == "failed" else "",
        "base_url": base_url,
        "model": model,
        "models": spec.models if hasattr(spec, 'models') else [],
        "capabilities": spec.capabilities if hasattr(spec, 'capabilities') else {},
    }


def _persist_test_result(provider_id: str, *, ok: bool, error: str, model: str, base_url: str) -> None:
    """Record one Test outcome against the exact config it exercised."""
    from datetime import datetime

    from ..gateway.selection import save_provider_config

    settings = get_settings()
    # Save the base URL/model that were actually tested so a later change to either
    # invalidates the result; only persist a model/base URL value when it is real.
    save_provider_config(
        provider_id,
        home=settings.home,
        base_url=(base_url or None),
        model=(model or None),
        last_test_ok="true" if ok else "false",
        last_test_error=error,
        last_test_at=datetime.now().isoformat(),
        last_test_model=model,
        last_test_base_url=base_url,
    )


@app.get("/ai/providers")
async def list_ai_providers(_: None = Protected) -> List[Dict[str, Any]]:
    """List all available AI providers with their persisted status (no live call)."""
    create_provider_table()
    return [_provider_card(spec.id) for spec in list_providers()]


@app.get("/ai/providers/{provider_id}")
async def get_provider_details(provider_id: str, _: None = Protected) -> Dict[str, Any]:
    """Get detailed information about a specific provider."""
    create_provider_table()
    if not next((s for s in list_providers() if s.id == provider_id), None):
        raise HTTPException(status_code=404, detail=f"Provider {provider_id} not found")
    if not PROVIDERS.get(provider_id):
        raise HTTPException(status_code=404, detail=f"Implementation missing for {provider_id}")
    return _provider_card(provider_id)


@app.get("/ai/providers/{provider_id}/health")
async def get_provider_health(provider_id: str, _: None = Protected) -> Dict[str, Any]:
    """Evidence-based route health for one provider/model — cheap, side-effect-free.

    Reads the in-memory route-health ledger (populated by real chat requests and
    explicit tests) plus, for the local 9Router gateway, a cheap reachability probe
    of its own ``/api/health`` endpoint. It performs **no model completion**, so a
    GET here never costs a paid call and never changes the active selection.

    ``route_health`` states: ``unknown`` (no evidence), ``healthy`` (recent success),
    ``degraded`` (a few consecutive route failures), ``unhealthy`` (many consecutive
    failures), ``not_configured`` (no key saved).
    """
    from ..gateway.selection import provider_base_url, provider_model

    provider_cls = PROVIDERS.get(provider_id)
    if not provider_cls:
        raise HTTPException(status_code=404, detail=f"Provider {provider_id} not found")

    settings = get_settings()
    usable = _provider_usable(provider_cls, provider_id)
    model = provider_model(settings, provider_id)
    base_url = provider_base_url(settings, provider_id)

    from ..gateway.route_health import compute_state, get_route_health, human_state

    entry = get_route_health().snapshot(provider_id, model)
    state = compute_state(entry, configured=usable)

    # Capability evidence, reported from the same ledger (real completions),
    # not from a static profile that can drift or over-claim. Surfacing it here
    # is what makes the registry *read* in the live path: the dashboard's health
    # box already renders this response. It is a report, not a gate -- the
    # registry never decides whether a request may run.
    from ..security.providers import get_registry

    capability = get_registry().capability_report(provider_id, model)

    # Gateway reachability is only meaningful when there is a local-gateway health
    # probe; otherwise it is reported as not-applicable rather than guessed.
    gateway_reachable: Optional[bool] = None
    if usable and base_url and provider_cls.name == "9router":
        try:
            gateway_reachable = provider_cls(settings).gateway_reachable()
        except Exception:
            gateway_reachable = False

    return {
        "provider_id": provider_id,
        "model": model,
        "configured": usable,
        "gateway_reachable": gateway_reachable,
        "route_health": state,
        # The label is built from evidence: a probe result when one exists, and
        # else a recorded failure is named rather than called "reachable".
        "route_health_label": human_state(state, gateway_reachable=gateway_reachable),
        "last_success_at": entry.get("last_success_at"),
        "last_failure_at": entry.get("last_failure_at"),
        "last_latency_ms": entry.get("last_latency_ms"),
        "consecutive_failures": entry.get("consecutive_failures"),
        "last_error_category": entry.get("last_error_category"),
        "last_http_status": entry.get("last_http_status"),
        "last_check_source": entry.get("last_check_source"),
        "upstream_model": entry.get("upstream_model"),
        "upstream_route_id": entry.get("upstream_route_id"),
        "upstream_identity_known": entry.get("upstream_identity_known"),
        # Verified / declared / unknown per capability, from real completions
        # (see CapabilityRegistry.capability_report). A report, not a gate.
        "capability": capability["capabilities"],
        "capability_verified": [
            cap for cap, tier in capability["capabilities"].items()
            if tier == "verified"
        ],
        "message": human_state(state, gateway_reachable=gateway_reachable),
    }


@app.post("/ai/providers/{provider_id}/test")
async def test_provider_credentials(provider_id: str, body: Optional[dict] = Body(None), _: None = Protected) -> Dict[str, Any]:
    """Test the selected provider's stored configuration.

    Tests the credential *already stored* for this provider (the key lives in the
    keychain, never here), the provider's effective base URL, and the selected
    model. The optional body may carry a one-off ``api_key`` + ``llm_model`` for a
    "validate before save" flow, but the normal card Test button sends no body and
    tests what was saved — which is the only way a "Test" that follows "Save" can
    prove the saved configuration actually works.
    """
    from datetime import datetime

    from ..gateway.selection import provider_base_url, provider_model, effective_key

    provider_cls = PROVIDERS.get(provider_id)
    if not provider_cls:
        raise HTTPException(status_code=404, detail=f"Provider {provider_id} not found")

    body = body or {}
    # A key explicitly passed in the body (pre-save validation) wins; otherwise the
    # stored key is used. Neither is ever echoed or logged.
    api_key = str(body.get("api_key", "") or "") or KeychainStorage.retrieve(provider_id)
    keyless = not getattr(provider_cls, "REQUIRES_KEY", True)
    if not api_key and not keyless:
        return {
            "success": False,
            "message": "No API key is stored for this provider. Add one in AI Settings first.",
            "timestamp": datetime.now().isoformat(),
            "notes": {},
        }

    # Resolve the endpoint + model for *this* provider, never the global active
    # selection: prompting the Gemini card's Test must use Gemini's own saved model
    # and base URL, not whatever a custom provider left in the active selection.
    settings = get_settings()
    # The *saved* config is what a normal Test button proves. A body-supplied
    # ``llm_model``/``llm_base_url`` is a one-off "validate before save" override and
    # must NOT overwrite the saved config or the health ledger key — otherwise probing
    # a throwaway model id would silently re-write the provider's real saved model.
    saved_model = provider_model(settings, provider_id)
    saved_base_url = provider_base_url(settings, provider_id)
    model = str(body.get("llm_model", "") or "") or saved_model
    base_url = str(body.get("llm_base_url", "") or "") or saved_base_url
    if provider_id == "openai_compatible" and not base_url:
        return {
            "success": False,
            "message": "A base URL is required for a custom provider. Set it in AI Settings.",
            "timestamp": datetime.now().isoformat(),
            "notes": {},
        }

    # A bounded but realistic budget for a user-triggered connection test, matching
    # the real chat request's own budget (30s): a cold route on a gateway
    # (9Router, OpenRouter) can legitimately exceed a few seconds on the first
    # completion; 30s is short enough to fail fast but long enough to give a
    # real verdict rather than a false "timeout".
    timeout_s = float(body.get("timeout", 30.0)) if isinstance(body, dict) else 30.0
    try:
        temp_settings = type('Settings', (), {
            'llm_api_key': api_key,
            'llm_base_url': base_url,
            'ai_provider': provider_id,
        })()
        test_provider = provider_cls(temp_settings)
        result = test_provider.verify_connection(timeout=timeout_s, model=model)

        # Only persist a verdict when the test ran against an actually-resolved
        # config (not a one-off body key, which validates a draft and must not mark
        # the saved provider). Crucially, the persisted config records the *saved*
        # model/base_url, never a body-supplied override — so a throwaway probe
        # model id cannot re-write the provider's real saved selection.
        #
        # The in-memory route-health ledger is already updated by the provider
        # adapter itself (`_post` → `_record_health(source="test")`), so no
        # double-record happens here; that single record carries the tested model
        # and upstream metadata.
        if not str(body.get("api_key", "") or ""):
            _persist_test_result(
                provider_id,
                ok=result.ok,
                error=result.error if not result.ok else "",
                model=saved_model,
                base_url=saved_base_url,
            )

        return {
            "success": result.ok,
            "message": result.error if not result.ok else "Connection verified successfully",
            "category": (result.notes or {}).get("category") if not result.ok else None,
            "timestamp": datetime.now().isoformat(),
            "notes": result.notes or {},
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Testing failed: {str(exc)}")


@app.post("/ai/providers/{provider_id}/store")
async def store_provider_credentials(provider_id: str,
                                    body: dict = Body(...), _: None = Protected) -> Dict[str, Any]:
    """Store credentials securely in keychain, plus per-provider non-secret config.

    The key rides in the request body, never a query string (see the /test route
    for why). It is written to the keychain and never echoed back. An optional
    ``llm_base_url``/``llm_model`` are saved to *this* provider's own config block,
    not the global active selection, so configuring a custom endpoint never
    overwrites another provider's endpoint or model.
    """
    from datetime import datetime

    from ..gateway.selection import save_provider_config

    api_key = str(body.get("api_key", "") or "")

    provider_cls = PROVIDERS.get(provider_id)
    if not provider_cls:
        raise HTTPException(status_code=404, detail=f"Provider {provider_id} not found")

    # A keyless connector has no secret to store: this route then only records the
    # endpoint/model, so "Add" works in a UI that asks for a key everyone else needs.
    keyless = not getattr(provider_cls, "REQUIRES_KEY", True)
    if (not api_key or len(api_key) < 10) and not keyless:
        raise HTTPException(status_code=400, detail="Invalid API key format")

    # Ensure the tables exist before writing the credential reference: on a fresh
    # data directory nothing has created ai_credentials yet, and the FK to
    # ai_providers needs its rows seeded too. (The list/detail routes do this; the
    # write routes must too, or the first store on a clean install fails.)
    create_provider_table()

    if api_key:
        stored = KeychainStorage.store(provider_id, api_key)
        if not stored:
            raise HTTPException(status_code=500, detail="Failed to store credentials securely")

    # Per-provider non-secret config, only when the client supplied it. A blank
    # base URL/model clears the field; None leaves it untouched.
    base_url = body.get("llm_base_url", None)
    model = body.get("llm_model", None)
    if base_url is not None or model is not None:
        settings = get_settings()
        save_provider_config(
            provider_id,
            home=settings.home,
            base_url=(str(base_url) if base_url is not None else None),
            model=(str(model) if model is not None else None),
        )

    cred_id = save_credential_ref(provider_id, "active") if api_key else ""
    invalidated = 0

    return {
        "success": True,
        "credential_id": cred_id,
        "message": "Credentials stored securely",
        "timestamp": datetime.now().isoformat(),
        "old_credentials_revoked": invalidated,
    }


@app.delete("/ai/providers/{provider_id}/credentials")
async def delete_provider_credentials(provider_id: str, _: None = Protected) -> Dict[str, Any]:
    """Delete stored credentials."""
    from datetime import datetime

    # The table must exist to read the credential references below; on a fresh data
    # directory it may not, so ensure it (idempotent) before the delete path runs.
    create_provider_table()

    deleted = KeychainStorage.delete(provider_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Credential not found")

    db_creds = get_credentials(provider_id)
    for cred in db_creds:
        update_credential_status(cred.id, "revoked")
    
    return {
        "success": True,
        "message": "Credentials deleted successfully",
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/ai/selection")
async def get_ai_selection(_: None = Protected) -> Dict[str, Any]:
    """The persisted provider selection -- the provider/model the assistant will use.

    Returns the *effective* values (what a request would actually resolve): the
    provider, model and base URL, plus whether a credential is stored for that provider.
    It never returns the credential itself.
    """
    from ..gateway.selection import (ai_enabled, effective_base_url, effective_key,
                                     provider_model, load_selection)

    settings = get_settings()
    selection = load_selection(settings.home)
    provider = settings.ai_provider
    return {
        "ai_provider": provider,
        "llm_model": settings.llm_model,
        "llm_base_url": effective_base_url(settings, provider),
        "effective_model": provider_model(settings, provider),
        "persisted": selection,
        "ai_enabled": ai_enabled(settings.home),
        "llm_enabled": settings.llm_enabled,
        "credential_configured": bool(effective_key(settings, provider)),
        "known_providers": sorted(PROVIDERS),
    }


@app.post("/ai/selection")
async def set_ai_selection(body: dict = Body(...), _: None = Protected) -> Dict[str, Any]:
    """Persist the assistant's provider/model selection.

    The body may carry ``ai_provider``, ``llm_model`` and ``llm_base_url``; an unknown
    provider is refused rather than silently accepted. The secret is deliberately not
    here -- it lives only in the keychain and is written by ``/ai/providers/{id}/store``.
    Persisting updates the live settings singleton in place, so the very next assistant
    request resolves the new selection without a restart.
    """
    from ..gateway.selection import get_provider_config, save_selection

    provider = str(body.get("ai_provider", "") or "").strip().lower()
    model = str(body.get("llm_model", "") or "").strip()
    base_url = str(body.get("llm_base_url", "") or "").strip()
    # The on/off switch is optional: only touch it when the body carries it, so a save
    # that edits the model does not silently re-enable an assistant the user turned off.
    enabled = body.get("ai_enabled", None)
    if enabled is not None:
        enabled = bool(enabled)

    if provider and provider not in PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider {provider!r}; known: {', '.join(sorted(PROVIDERS))}",
        )

    settings = get_settings()
    # Switching provider without an explicit model/base URL in this request restores
    # that provider's *own* saved config, so selecting Gemini does not reuse the model
    # or endpoint the active custom provider configured (and vice versa).
    if provider and (not model or not base_url):
        own = get_provider_config(provider, settings.home)
        if not model and own.get("model"):
            model = own["model"]
        if not base_url and own.get("base_url"):
            base_url = own["base_url"]

    saved = save_selection(settings.home, provider=provider, model=model,
                           base_url=base_url, enabled=enabled)

    # Reflect the change into the live singleton (env still wins on the next explicit
    # reload; here we honour what the operator just chose).
    if provider:
        settings.ai_provider = provider
    if model:
        settings.llm_model = model
    if base_url:
        settings.llm_base_url = base_url

    from ..gateway.selection import ai_enabled as _ai_enabled
    return {"success": True, "selection": saved,
            "effective": {"ai_provider": settings.ai_provider,
                          "llm_model": settings.llm_model,
                          "ai_enabled": _ai_enabled(settings.home),
                          "llm_enabled": settings.llm_enabled}}


@app.get("/ai/capabilities")
async def get_ai_capabilities(_: None = Protected) -> Dict[str, Any]:
    """Report which tasks go through which provider."""
    return provider_capabilities()


# ---------------------------------------------------------------------------
# Capability routing (speech, images, vision, video)
#
# The text assistant has one provider. These tasks do not: a speech model is
# not a text model, and a text model answering once is no evidence it can
# speak. So each has its own assignment, and an assignment is *declared* until
# a real request has succeeded for it — the status payload says which.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Provider categories — the seven tabs
#
# One payload drives the screen (`/ai/provider-categories`), and one route writes
# exactly one category's block (`PUT /ai/provider-categories/{category}`). The
# isolation guarantee is structural: the writer never touches another category,
# so the Image tab cannot change the Text tab.
#
# Keys are accepted on the write and go straight to the Keychain. They are never
# returned by any route, and `public()` cannot emit one because it never holds
# one.
# ---------------------------------------------------------------------------

@app.get("/ai/provider-categories")
async def get_provider_categories(_: None = Protected) -> Dict[str, Any]:
    """The seven categories, their providers, and each one's saved settings."""
    from ..gateway import provider_categories as pc

    return {"categories": pc.catalogue(),
            "order": list(pc.CATEGORIES)}


@app.put("/ai/provider-categories/{category}")
async def put_provider_category(category: str, body: dict = Body(None),
                                _: None = SameOriginProtected) -> Dict[str, Any]:
    """Save one category. Only that category's block is written.

    The body may carry ``provider``, ``model``, ``base_url``, ``enabled``,
    ``timeout``, ``capability`` and ``options``. ``api_key`` is written to the
    Keychain and is never echoed back — the response says only whether a key is
    stored.
    """
    from ..gateway import provider_categories as pc

    body = body or {}
    if category not in pc.CATEGORIES:
        raise HTTPException(status_code=404,
                            detail=f"{category!r} is not a provider category. "
                                   f"Known: {', '.join(pc.CATEGORIES)}.")
    provider = body.get("provider")
    if provider is not None:
        provider = str(provider).strip()
        if provider and provider not in pc.CATEGORY_PROVIDERS[category]:
            raise HTTPException(
                status_code=422,
                detail=(f"{provider!r} cannot perform {category!r}. "
                        f"It accepts: {', '.join(pc.CATEGORY_PROVIDERS[category])}."))
    base_url = body.get("base_url")
    if base_url is not None:
        base_url = str(base_url).strip()
        if base_url and not base_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=422,
                                detail="The base URL must start with http:// or https://.")
    options = body.get("options")
    if options is not None and not isinstance(options, dict):
        raise HTTPException(status_code=422, detail="options must be an object.")

    try:
        settings = pc.set_category(
            category,
            provider=provider,
            model=body.get("model") if "model" in body else None,
            base_url=base_url,
            enabled=body.get("enabled") if "enabled" in body else None,
            timeout=body.get("timeout") if "timeout" in body else None,
            capability=body.get("capability") if "capability" in body else None,
            options=options)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    stored = None
    if "api_key" in body:
        result = await asyncio.to_thread(pc.store_key, category,
                                         str(body.get("api_key") or ""))
        stored = bool(result)
        settings = pc.load(category)
    return {"saved": True, "key_stored": stored, "category": settings.public()}


@app.post("/ai/provider-categories/{category}/test")
async def test_provider_category(category: str, _: None = SameOriginProtected) -> Dict[str, Any]:
    """Perform this category's **real** operation and record what happened.

    The point of the test is that it exercises the job the tab is for — a small
    chat completion, a short synthesis, a tiny image, a one-pixel vision
    question. A provider whose key merely exists is not proven by it.
    """
    from ..gateway import provider_categories as pc
    from ..gateway import category_checks

    if category not in pc.CATEGORIES:
        raise HTTPException(status_code=404,
                            detail=f"{category!r} is not a provider category.")
    settings = pc.load(category)
    if not settings.provider or not settings.base_url:
        raise HTTPException(status_code=409,
                            detail=f"Nothing is configured for {category!r} yet.")
    result = await asyncio.to_thread(category_checks.run, category, settings)
    pc.record_test(category, ok=bool(result.get("ok")),
                   note=str(result.get("detail") or result.get("error") or "")[:300])
    payload = dict(result)
    payload["category"] = pc.load(category).public()
    return payload


@app.post("/ai/provider-categories/{category}/discover")
async def discover_provider_category(category: str, _: None = SameOriginProtected) -> Dict[str, Any]:
    """``GET {base_url}/models`` for this category's configured provider.

    The list is the provider's own statement; entries carry
    ``output_modalities``/``supported_endpoints`` when published, so the UI can
    narrow models to ones that can do this tab's job instead of offering a chat
    model for an image slot.
    """
    from ..gateway import provider_categories as pc
    from ..gateway.providers import discover_models

    settings = pc.load(category)
    if not settings.base_url:
        raise HTTPException(status_code=409,
                            detail=f"Set a base URL for {category!r} first.")
    key = pc.retrieve_key(category) or ""
    auth_header = "x-goog-api-key" if settings.provider == "gemini" else ""
    found = await asyncio.to_thread(discover_models, settings.base_url, key,
                                    auth_header=auth_header)
    wanted = _modality_for(category)
    if found.get("ok") and wanted:
        models = found.get("models") or []
        matching = [m for m in models if _model_fits(m, wanted)]
        found["matching"] = matching
        found["matching_count"] = len(matching)
    return found


def _modality_for(category: str) -> str:
    return {
        "text": "text", "documents": "text", "custom": "text",
        "speech": "", "image": "image", "vision": "text", "video": "video",
    }.get(category, "")


def _model_fits(model: dict, wanted: str) -> bool:
    outputs = [o.lower() for o in (model.get("output_modalities") or [])]
    if not outputs:
        return True     # a provider that publishes nothing is not narrowed here
    return wanted in outputs


@app.get("/ai/capability-routing")
async def get_capability_routing(_: None = Protected) -> Dict[str, Any]:
    """Every routed task, what it is assigned, and whether that is verified.

    Deliberately separate from ``/ai/capabilities``, which answers the older
    question ("which vendor handles chat, vision, stt"). This one is the
    per-task assignment the Providers screen edits, and it reports
    ``verified`` / ``declared`` / ``unknown`` per model rather than a bool.
    """
    from ..gateway import routing

    assignments = routing.load_assignments()
    return {
        "tasks": list(routing.TASKS),
        "routed": list(routing.ROUTED),
        "assignments": {task: a.public() for task, a in assignments.items()},
    }

@app.post("/ai/capability-routing/{task}")
async def set_capability_routing(task: str, body: dict = Body(None),
                                 _: None = SameOriginProtected) -> Dict[str, Any]:
    """Assign a provider/model to one capability.

    The provider must already be a known provider (the gateway's registry): an
    unknown id is refused rather than stored and left to fail later. The model
    is a string because a provider's model ids are its own business.
    """
    from ..gateway import providers as gateway
    from ..gateway import routing

    body = body or {}
    provider_id = str(body.get("provider_id") or "").strip()
    if provider_id not in gateway.PROVIDERS:
        raise HTTPException(
            status_code=422,
            detail=(f"{provider_id!r} is not a provider this build can talk to. "
                    f"Known: {', '.join(sorted(gateway.PROVIDERS))}."))
    model = str(body.get("model") or "").strip()
    base_url = str(body.get("base_url") or "").strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=422,
                            detail="The base URL must start with http:// or https://.")
    fallbacks = body.get("fallbacks")
    if fallbacks is not None and not isinstance(fallbacks, list):
        raise HTTPException(status_code=422, detail="fallbacks must be a list.")
    try:
        assignment = routing.set_assignment(
            task, provider_id=provider_id, model=model, base_url=base_url,
            fallbacks=fallbacks or [])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"saved": True, "assignment": assignment.public()}

@app.post("/ai/capability-routing/{task}/test")
async def test_capability_routing(task: str, _: None = SameOriginProtected) -> Dict[str, Any]:
    """Perform the real bounded request for this task's assigned model.

    This is the only thing that turns ``declared`` into ``verified``. Each
    capability gets a request that actually exercises it — a 200 alone is not
    enough for any of them: the response's *shape* decides.
    """
    from ..gateway import media, routing

    assignment = routing.load_assignments().get(task)
    if assignment is None or assignment.primary is None:
        raise HTTPException(status_code=409,
                            detail=f"No provider is assigned to {task.replace('_', ' ')}.")

    def perform(adapter):
        if task == "text_to_speech":
            return adapter.verify()
        if task == "speech_to_text":
            return media.MediaResult(
                False, task, error="Testing speech-to-text needs a short recording; "
                                   "there is no sample audio on this machine to send, "
                                   "so the endpoint was not exercised.")
        if task == "image_generation":
            return adapter.generate(media.ImageRequest(prompt="a single red square",
                                                       size="256x256"))
        if task == "image_editing":
            return media.MediaResult(
                False, task, error="Testing image editing needs a source image; "
                                   "supply one from the Images screen.")
        if task == "vision":
            import base64

            # A valid 1x1 PNG: enough for the model to accept the input, and the
            # point of the test is that the endpoint accepts image input at all.
            pixel = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
                "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
            return adapter.verify(pixel)
        if task == "video_generation":
            return adapter.submit("a two-second test pattern", size="720x480", seconds=2)
        return media.MediaResult(False, task, error="No test for this capability.")

    result = await asyncio.to_thread(routing.route, task, perform,
                                     allow_fallback=False)
    ok = bool(result.ok)
    note = "verified by a real request" if ok else (result.error or "the test failed")
    routing.record_verification(task,
                                provider_id=assignment.primary.provider_id,
                                model=assignment.primary.model, ok=ok, note=note)
    payload = result.public()
    payload["result"] = (result.result.as_dict()
                         if hasattr(result.result, "as_dict") else None)
    payload["assignments"] = {t: a.public()
                              for t, a in routing.load_assignments().items()}
    return payload


@app.post("/ai/verify-all")
async def verify_all_providers(_: None = Protected) -> Dict[str, Any]:
    """Verify connection for all configured providers (live, user-triggered).

    This is a *completion* check, not a cheap liveness probe: it runs the same
    one-small-turn chat path the assistant uses, because ``GET /models`` does not
    prove a model can answer, and many compatible endpoints don't expose model
    discovery. The 8s budget below is deliberately short for that reason — it is a
    quick "is anything here and is it the right key", not a full chat verdict; a
    provider that needs a longer cold-start completion must be exercised with the
    provider Test button, which uses the 30s chat budget. A timeout is therefore
    reported distinctly (`category: "timeout"`), never folded into an invalid-key
    or provider-error classification.
    """
    from datetime import datetime

    from ..gateway.selection import provider_model

    settings = get_settings()
    results = {}

    for provider_id, provider_cls in PROVIDERS.items():
        try:
            provider = provider_cls(settings)
            if provider.configured():
                result = provider.verify_connection(
                    timeout=8.0, model=provider_model(settings, provider_id)
                )
                results[provider_id] = {
                    "verified": result.ok,
                    "error": result.error if not result.ok else None,
                    "category": (result.notes or {}).get("category")
                    if not result.ok else None,
                    "notes": result.notes or {},
                }
            else:
                results[provider_id] = {
                    "verified": False,
                    "error": "not configured",
                    "category": "not_configured",
                }
        except Exception as e:
            results[provider_id] = {
                "verified": False,
                "error": str(e),
                "category": "internal_error",
            }

    return {
        "timestamp": datetime.now().isoformat(),
        "results": results,
    }


# ---------------------------------------------------------------------------
# Tools (Phase 0)
# ---------------------------------------------------------------------------
@app.get("/tools")
async def tools(_: None = Protected) -> List[Dict[str, Any]]:
    return [
        {
            "name": s.name,
            "agent": s.agent,
            "risk": int(s.risk),
            "risk_label": s.risk_label(),
            "requires_confirmation": s.requires_confirmation,
            "reversible": s.reversible,
            "description": s.description,
            # The older spellings this capability still answers to, so an audit row or a
            # stored job step naming `open_app` can be read against today's registry
            # without guessing that the two are the same tool.
            "aliases": list(s.aliases),
            "parameters": s.parameters,
        }
        for s in get_core().tools.all()
    ]


@app.get("/audit")
async def audit(limit: int = 50, _: None = Protected) -> List[Dict[str, Any]]:
    return get_core().audit.recent(limit)


@app.get("/coding/availability")
async def coding_availability(_: None = Protected) -> Dict[str, Any]:
    """Whether a coding agent is actually installed, and where it would work.

    The supervisor screen must know this before offering to start work: an
    interface that offers to run an agent that is not present is the dead-end
    control this project keeps removing.
    """
    from ..tools.coding_agent import agent_cli_path

    cli = agent_cli_path()
    return {
        "available": bool(cli),
        "agent": "Claude Code" if cli else "",
        "detail": ("Claude Code is installed." if cli else
                   "Claude Code is not installed on this laptop, so no coding task "
                   "can be started."),
        "workspaces": [
            {"workspace_id": w.get("workspace_id"), "name": w.get("name"), "root": w.get("root")}
            for w in get_core().workspaces.list()
        ] if hasattr(get_core(), "workspaces") else [],
        "requires_approval": True,
    }


@app.post("/coding/tasks")
async def create_coding_task(body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    """Hand a development goal to the coding agent, as a job Vantage tracks.

    Real work, not a simulation: the agent is an actual program on this machine,
    it runs in the named folder, and what comes back is its own answer. The job
    is created *before* the work, so an interrupted run is visible rather than
    lost, and the outcome carries the agent's words plus which files changed on
    disk. Confirmation-gated, because the person starting it is starting a
    process with file access.
    """
    from ..core.models import ActionStatus
    from ..tools.base import ToolContext
    from ..tools.coding_agent import run_coding_agent

    body = body or {}
    goal = str(body.get("goal") or "").strip()
    if not goal:
        raise HTTPException(status_code=422, detail="a coding task needs a goal")
    path = str(body.get("path") or "").strip()

    core = get_core()
    job = core.tasks.create(goal=goal, steps=["Run the coding agent"])
    job_id = job.job_id

    before = _snapshot_tree(path) if path else {}
    try:
        core.tasks.update_step(job_id, "Run the coding agent", ActionStatus.RUNNING,
                               detail=f"working in {path or 'the current folder'}", seq=0)
        result = await run_coding_agent(ToolContext(), prompt=goal, path=path,
                                        resume=bool(body.get("resume")))
    except Exception as exc:
        core.tasks.update_step(job_id, "Run the coding agent", ActionStatus.FAILED,
                               detail=str(exc)[:200], seq=0)
        core.tasks.set_status(job_id, "failed")
        return {"ok": False, "job_id": job_id, "error": str(exc)[:200]}

    output = result.output or {}
    changed = _diff_tree(before, _snapshot_tree(path)) if path else []
    # `fail()` reports through `.error`, `ok()` through `.output["message"]`.
    # Reading only the success shape left a failed task with an empty detail --
    # the one moment the reason matters most.
    detail_text = str(getattr(result, "error", "") or "") or str(
        output.get("result") or output.get("message") or "")
    # The step's `detail` is the only free text that survives the request, so the
    # things a person needs *after* the fact — the changed files, the turn count,
    # the workspace — go in it too. Returning them without storing them meant a
    # finished coding job showed a 400-character message and lost its file list the
    # moment the page reloaded.
    stored = detail_text[:400]
    if changed:
        stored += "\n\nChanged files:\n" + "\n".join(changed[:20])
    if output.get("workspace") or path:
        stored += f"\n\nWorkspace: {output.get('workspace') or path}"
    core.tasks.update_step(
        job_id, "Run the coding agent",
        ActionStatus.SUCCEEDED if result.ok else ActionStatus.FAILED,
        detail=stored, seq=0)
    core.tasks.refresh_status(job_id)

    return {
        "ok": bool(result.ok),
        "job_id": job_id,
        "message": detail_text,
        "verified": bool(getattr(result, "verified", False)),
        "turns": output.get("turns"),
        "cost_usd": output.get("cost_usd"),
        "workspace": output.get("workspace") or path,
        "changed_files": changed,
    }


def _snapshot_tree(root: str) -> Dict[str, float]:
    """Modification times under a workspace, for a before/after diff."""
    from pathlib import Path as _P

    base = _P(root).expanduser()
    out: Dict[str, float] = {}
    if not root or not base.is_dir():
        return out
    skip = {".git", "node_modules", ".venv", "__pycache__", ".build", "dist", ".pytest_cache"}
    for item in base.rglob("*"):
        if any(part in skip for part in item.parts):
            continue
        try:
            if item.is_file():
                out[str(item)] = item.stat().st_mtime
        except OSError:
            continue
    return out


def _diff_tree(before: Dict[str, float], after: Dict[str, float]) -> List[str]:
    """Files created or changed between the two snapshots."""
    return sorted(p for p, t in after.items() if before.get(p) != t)


@app.get("/tasks")
async def tasks(limit: int = 25, _: None = Protected) -> List[Dict[str, Any]]:
    return get_core().tasks.list(limit)


@app.get("/tasks/{job_id}")
async def task(job_id: str, _: None = Protected) -> Dict[str, Any]:
    """One job with its steps. A workflow's progress is only honest per step."""
    job = get_core().tasks.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id!r}")
    return job


@app.get("/memory")
async def memory(limit: int = 50, _: None = Protected) -> Dict[str, Any]:
    core = get_core()
    return {
        "stats": core.memory.stats(),
        "items": [m.model_dump(mode="json") for m in core.memory.all(limit=limit)],
    }


@app.post("/memory")
async def add_memory(body: MemoryBody, _: None = Protected) -> Dict[str, Any]:
    core = get_core()
    from ..core.models import MemoryScope

    try:
        scope = MemoryScope(body.scope)
    except ValueError:
        # Silently substituting long_term stored the item somewhere other than
        # where it was asked to go, and the caller had no way to notice -- the
        # response looked like an ordinary success. A bad scope is a client
        # error and says so, naming the scopes that exist.
        valid = ", ".join(s.value for s in MemoryScope)
        raise HTTPException(
            status_code=422,
            detail=f"Unknown memory scope {body.scope!r}. Use one of: {valid}.",
        )
    project_id = core.graph.get_or_create_project(body.project) if body.project else None
    item = core.memory.remember(body.content, scope=scope, project_id=project_id)
    return item.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Daily planner (Epic 1) — the user's own tasks and the plan for a day.
# ---------------------------------------------------------------------------
def _parse_dt(value: Any) -> Optional[datetime]:
    from datetime import datetime as _dt
    if value in (None, ""):
        return None
    if isinstance(value, _dt):
        return value
    try:
        return _dt.fromisoformat(str(value))
    except ValueError:
        return None


@app.post("/planner/begin-work")
async def begin_work(body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    """Start the day *and* act on it: open the project, then offer the agent.

    The planner has always been able to say what to do first; nothing acted on
    that answer. This composes the existing pieces rather than adding a fifth
    briefing:

      1. assemble the day (the same `start_day` route);
      2. if the next action names a project that resolves to a real folder, open
         it in the editor — a probe driven by the task, not a guess;
      3. report the exact prompt to hand the coding agent, and whether the agent
         is actually available. It does **not** start the agent: that is a
         confirmation-gated action on a process with file access, and the person
         asked to be *started*, not to have work done behind them.

    Each step is reported with what really happened, and a step that could not run
    says why instead of being skipped silently.
    """
    from ..tools.base import ToolContext

    body = body or {}
    steps: List[str] = []

    plan = await start_day(body, None)
    steps.append(f"planned the day: {plan.get('headline', '')[:80]}")
    next_action = plan.get("next_action") or {}
    task = next_action.get("task") or {}
    title = str(task.get("title") or "")
    project = str(task.get("project") or "")

    opened: Dict[str, Any] = {"attempted": False}
    if project:
        steps.append(f"task names the project {project!r}")
        try:
            from ..tools.apps import open_project_folder

            result = await open_project_folder(ToolContext(), project=project)
            opened = {
                "attempted": True,
                "ok": bool(result.ok),
                "message": getattr(result, "message", "") or getattr(result, "error", ""),
            }
            steps.append("opened the project" if result.ok else "could not open the project")
        except Exception as exc:
            opened = {"attempted": True, "ok": False, "message": str(exc)[:160]}
            steps.append("opening the project failed")
    else:
        steps.append("the next task names no project, so nothing was opened")

    from ..tools.coding_agent import agent_cli_path

    cli = agent_cli_path()
    agent = {
        "available": bool(cli),
        "agent": "Claude Code" if cli else "",
        "prompt": f"Continue working on: {title}" if title else "",
        "note": ("Start it from the Work page (it asks first) — I did not start it here."
                 if cli else
                 "No coding agent is installed, so there is nothing to start."),
    }
    steps.append("checked for a coding agent" if cli else "no coding agent installed")

    return {
        "headline": plan.get("headline", ""),
        "plan": plan,
        "opened_project": opened,
        "agent": agent,
        "workflow": steps,
        "note": "Composed from the existing pieces: the plan, the editor, and the "
                "agent check. Nothing was started on your behalf.",
    }


@app.post("/planner/reschedule-unfinished")
async def reschedule_unfinished(body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    """Move unfinished work into real free time on the coming days.

    Only tasks that already have a start time and are not done are moved; a task
    with no scheduled time was never "unfinished work in a slot", it was a
    backlog item, and inventing a time for it is not what was asked. Each new slot
    comes from the calendar actually being read — if it cannot be read, nothing is
    moved and the reply says so rather than shuffling work onto occupied time.
    """
    from datetime import datetime as _dt, timedelta as _td

    from ..tools.base import ToolContext
    from ..tools.calendar import DAY_END_HOUR, DAY_START_HOUR

    body = body or {}
    days_ahead = int(body.get("days") or 5)
    if not 1 <= days_ahead <= 14:
        raise HTTPException(status_code=422, detail="days must be between 1 and 14")

    planner = get_core().planner_service
    now = _dt.now().astimezone()
    today = now.date()

    candidates = []
    for task in planner.list_tasks():
        if not task.is_open or task.start_at is None:
            continue
        start = task.start_at
        if start.tzinfo is None:
            start = start.astimezone()
        # Only work whose slot has passed or is today's remainder.
        if start.date() <= today:
            candidates.append(task)
    if not candidates:
        return {"ok": True, "moved": [], "moved_count": 0,
                "note": "No timed, unfinished work to move."}

    # Slots that are already taken, with two sources rather than one:
    #
    #  * work this pass has handed out, and
    #  * timed planner tasks that already hold a slot.
    #
    # The second matters because `claimed` lives only for one request: two separate
    # "schedule a coding block" calls each saw an empty calendar and were both
    # given 08:00. A planner task's own start time *is* an occupied slot, and the
    # calendar knows nothing about it.
    claimed: List[tuple] = []
    for other in planner.list_tasks():
        if not other.is_open or other.start_at is None:
            continue
        other_start = other.start_at
        if other_start.tzinfo is None:
            other_start = other_start.astimezone()
        if other_start.date() < today:
            continue  # a stale slot is what this pass is here to fix
        claimed.append((other_start,
                        other_start + _td(minutes=max(15, int(other.estimate_minutes or 30)))))

    def _next_free(after: Optional[_dt], events: List[Any], window_end: _dt,
                   minutes: int) -> Optional[_dt]:
        """First free start >= `after` that does not overlap a claim."""
        cursor = after
        for busy_start, busy_end in events:
            if cursor + _td(minutes=minutes) <= busy_start:
                break
            if cursor < busy_end:
                cursor = busy_end
        while claimed and any(
                cursor < end and start < cursor + _td(minutes=minutes)
                for start, end in claimed):
            cursor = max(end for start, end in claimed
                         if cursor < end and start < cursor + _td(minutes=minutes))
        if cursor + _td(minutes=minutes) > window_end:
            return None
        return cursor

    moved, problems = [], []
    for offset in range(days_ahead):
        day = today + _td(days=offset)
        label = "today" if offset == 0 else day.isoformat()
        day_start = _dt.combine(day, _dt.min.time(), tzinfo=now.tzinfo)
        from ..integrations.calendar_provider import get_calendar_provider, read_status

        window_start = day_start.replace(hour=DAY_START_HOUR)
        window_end = day_start.replace(hour=DAY_END_HOUR)
        read = get_calendar_provider(get_settings()).list_events(
            window_start, window_end, limit=100)
        if not read_status(read).read_succeeded:
            problems.append(f"calendar could not be read for {label}")
            continue
        busy = sorted((max(e.start.astimezone(), window_start),
                       min(e.end.astimezone(), window_end))
                      for e in read if e.end.astimezone() > window_start
                      and e.start.astimezone() < window_end)
        earliest = max(window_start, now) if offset == 0 else window_start
        for task in list(candidates):
            minutes = max(15, int(task.estimate_minutes or 30))
            new_start = _next_free(earliest, busy, window_end, minutes)
            if new_start is None:
                problems.append(f"no free {minutes}-minute slot on {label}")
                continue
            claimed.append((new_start, new_start + _td(minutes=minutes)))
            planner.update_task(task.task_ref, start_at=new_start)
            moved.append({"task_ref": task.task_ref, "title": task.title,
                          "start_at": new_start.isoformat()})
            candidates.remove(task)
        if not candidates:
            break

    return {
        "ok": True,
        "moved": moved,
        "moved_count": len(moved),
        "remaining": [t.title for t in candidates],
        "problems": problems[:5],
        "note": ("Moved into free time; nothing was added to your calendar."
                 if moved else "Nothing was moved."),
    }


@app.post("/planner/schedule-coding")
async def schedule_coding(body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    """Put a coding block into a real free slot, as a planner task.

    The slot comes from `find_free_time`, which refuses rather than guesses when
    the calendar cannot be read — so a block is never proposed on top of a meeting
    Vantage could not see. The task carries the slot as its start time, with the
    duration, and appears in the day's plan like any other.
    """
    from ..tools.calendar import find_free_time
    from ..tools.base import ToolContext

    body = body or {}
    goal = str(body.get("goal") or "").strip()
    if not goal:
        raise HTTPException(status_code=422, detail="what should the coding block be for?")
    minutes = int(body.get("duration_minutes") or 90)
    if not 15 <= minutes <= 480:
        raise HTTPException(status_code=422, detail="duration must be 15 to 480 minutes")
    day = str(body.get("date") or "today")

    found = await find_free_time(ToolContext(), date=day, duration_minutes=minutes)
    slot = (found.output or {}).get("slot") if found.ok else None
    if not slot:
        return {"ok": False, "scheduled": False,
                "reason": getattr(found, "error", "") or
                          (found.output or {}).get("message", "")
                          or "no free slot of that length",
                "note": "No block was created."}

    from datetime import datetime as _dt

    start = _dt.fromisoformat(slot["start"])
    task = get_core().planner_service.create_task(
        goal, detail=f"Focused coding block ({minutes} min)", priority="high",
        start_at=start, estimate_minutes=minutes, source="coding")
    return {
        "ok": True,
        "scheduled": True,
        "task": task.as_dict(),
        "slot": slot,
        "note": "Booked as a planner task. Nothing was added to your calendar.",
    }


@app.get("/reports/weekly")
async def weekly_report(days: int = Query(7), _: None = Protected) -> Dict[str, Any]:
    """A weekly report built only from work Vantage actually recorded.

    Every line traces to a completed planner task or a finished job, with its
    date. Nothing is padded: if a section did not happen it is empty and says so,
    because a report that invents progress is worse than a short one.
    """
    from datetime import datetime as _dt, timedelta as _td

    if not 1 <= days <= 90:
        raise HTTPException(status_code=422, detail="days must be between 1 and 90")

    core = get_core()
    now = _dt.now().astimezone()
    since = now - _td(days=days)

    done = []
    for task in core.planner_service.list_tasks(status="done"):
        when = task.done_at
        if when is None:
            continue
        if when.tzinfo is None:
            when = when.astimezone()
        if when >= since:
            done.append({"title": task.title, "project": task.project,
                         "done_at": when.isoformat()})

    open_tasks = core.planner_service.list_tasks()
    overdue = [t.title for t in open_tasks if t.is_overdue(now)]

    jobs = []
    for job in core.tasks.list(limit=100):
        created = str(job.get("created_at") or "")
        status = str(job.get("status") or "")
        if status in ("completed", "failed") and created >= since.replace(tzinfo=None).isoformat():
            jobs.append({"goal": job.get("goal"), "status": status,
                         "job_id": job.get("job_id")})

    by_project: Dict[str, List[str]] = {}
    for item in done:
        by_project.setdefault(item["project"] or "General", []).append(item["title"])

    lines = [f"Work completed in the last {days} day(s): {len(done)} task(s)."]
    for project in sorted(by_project):
        lines.append(f"  {project}: " + "; ".join(by_project[project]))
    lines.append(f"Still open: {len(open_tasks)} task(s)"
                 + (f", {len(overdue)} overdue." if overdue else "."))
    if jobs:
        lines.append(f"Jobs finished: {len(jobs)}.")

    return {
        "since": since.isoformat(),
        "days": days,
        "completed": done,
        "completed_count": len(done),
        "by_project": by_project,
        "open_count": len(open_tasks),
        "overdue": overdue,
        "jobs": jobs,
        "summary": "\n".join(lines),
        "note": "Built only from recorded work; nothing here is estimated.",
    }


@app.get("/planner/tasks")
async def list_planner_tasks(_: None = Protected, include_done: bool = False) -> List[Dict[str, Any]]:
    """The person's own tasks (not workflow runs)."""
    tasks = get_core().planner_service.list_tasks(include_done=include_done)
    open_refs = {t.task_ref for t in get_core().planner_service.list_tasks()}
    now = __import__("datetime").datetime.now().astimezone()
    return [t.as_dict(now=now, open_refs=open_refs) for t in tasks]


@app.post("/planner/tasks")
async def create_planner_task(body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    """Create a task. A task needs a title; everything else is optional."""
    if not body or not str(body.get("title") or "").strip():
        raise HTTPException(status_code=422, detail="a task needs a title")
    service = get_core().planner_service
    task = service.create_task(
        str(body["title"]),
        detail=str(body.get("detail") or ""),
        priority=str(body.get("priority") or "normal"),
        due_at=_parse_dt(body.get("due_at")),
        start_at=_parse_dt(body.get("start_at")),
        estimate_minutes=int(body.get("estimate_minutes") or 0),
        depends_on=list(body.get("depends_on") or []),
        project=str(body.get("project") or ""),
    )
    open_refs = {t.task_ref for t in service.list_tasks()}
    now = __import__("datetime").datetime.now().astimezone()
    return task.as_dict(now=now, open_refs=open_refs)


@app.patch("/planner/tasks/{task_ref}")
async def update_planner_task(task_ref: str, body: dict = Body(None),
                              _: None = Protected) -> Dict[str, Any]:
    """Edit a task: title, detail, priority, dates, estimate, dependencies, project."""
    body = body or {}
    changes: Dict[str, Any] = {}
    for key in ("title", "detail", "priority", "project"):
        if key in body:
            changes[key] = str(body[key])
    for key in ("due_at", "start_at"):
        if key in body:
            changes[key] = _parse_dt(body[key])
    if "estimate_minutes" in body:
        changes["estimate_minutes"] = int(body["estimate_minutes"] or 0)
    if "depends_on" in body:
        changes["depends_on"] = list(body["depends_on"] or [])
    service = get_core().planner_service
    task = service.update_task(task_ref, **changes)
    if task is None:
        raise HTTPException(status_code=404, detail=f"no such task: {task_ref}")
    open_refs = {t.task_ref for t in service.list_tasks()}
    now = __import__("datetime").datetime.now().astimezone()
    return task.as_dict(now=now, open_refs=open_refs)


@app.post("/planner/tasks/{task_ref}/complete")
async def complete_planner_task(task_ref: str, body: dict = Body(None),
                                _: None = Protected) -> Dict[str, Any]:
    """Mark done, recording the evidence the person gave (optional)."""
    task = get_core().planner_service.complete_task(
        task_ref, evidence=str((body or {}).get("evidence") or ""))
    if task is None:
        raise HTTPException(status_code=404, detail=f"no such task: {task_ref}")
    now = __import__("datetime").datetime.now().astimezone()
    return task.as_dict(now=now)


@app.delete("/planner/tasks/{task_ref}")
async def cancel_planner_task(task_ref: str, _: None = Protected) -> Dict[str, Any]:
    task = get_core().planner_service.cancel_task(task_ref)
    if task is None:
        raise HTTPException(status_code=404, detail=f"no such task: {task_ref}")
    return {"task_ref": task_ref, "status": task.status}


@app.get("/planner/today")
async def planner_today(_: None = Protected, day: str = "") -> Dict[str, Any]:
    """The day's agenda: events (really read), tasks, conflicts, next action.

    The calendar is read here and handed to the planner service, so a failed
    read is reported as such rather than rendered as an empty day.
    """
    from datetime import date as _date, datetime, timedelta as _td

    core = get_core()
    target = _date.fromisoformat(day) if day else datetime.now().astimezone().date()
    now = datetime.now().astimezone()
    start = datetime.combine(target, datetime.min.time(), tzinfo=now.tzinfo)
    end = start + _td(days=1)

    events: List[Any] = []
    failed = False
    try:
        from ..integrations.calendar_provider import get_calendar_provider, read_status
        result = get_calendar_provider(get_settings()).list_events(start, end, limit=100)
        status = read_status(result)
        if status.read_succeeded:
            events = list(result)
        else:
            failed = True
    except Exception as exc:
        log.warning("planner could not read the calendar", extra_fields={"error": str(exc)[:120]})
        failed = True

    return core.planner_service.todays_agenda(
        day=target, now=now, calendar_events=events, calendar_read_failed=failed)


@app.get("/projects")
async def projects(_: None = Protected) -> List[Dict[str, Any]]:
    return get_core().graph.list_projects()


# ---------------------------------------------------------------------------
# Voice (Phase 2)
# ---------------------------------------------------------------------------
@app.get("/voice/status")
async def voice_status(_: None = Protected) -> Dict[str, Any]:
    """Report every voice subsystem and whether it can actually run here.

    ``status()`` is not pure: on a cold process it compiles and runs the microphone
    consent helper and imports the audio backends, which is tens of milliseconds of
    disk + a ``shutil.which`` sweep. Called on the loop it stalls every other request
    while it does that -- the same defect the Google routes had. Off the loop.
    """
    return await asyncio.to_thread(get_voice().status)


@app.post("/voice/command")
async def voice_command(body: VoiceTextBody, _: None = Protected) -> Dict[str, Any]:
    """Run text through the *voice* pipeline: wake word, follow-ups, optional TTS."""
    session = get_voice()
    previous = session.settings.require_wake_word
    if body.require_wake is not None:
        session.settings.require_wake_word = body.require_wake
    try:
        turn = await session.process_text(body.text, speak=body.speak)
    finally:
        session.settings.require_wake_word = previous
    return turn.as_dict()


@app.post("/voice/speak")
async def voice_speak(body: VoiceTextBody, _: None = Protected) -> Dict[str, Any]:
    """Synthesise text to speech and report where the audio landed.

    ``ok`` is the backend's answer to *did this work*. A backend that records text
    and plays nothing answers ok -- that is correct for tests, but a person reading
    "Spoken via null" above silence is being told something false, so the reply also
    carries ``can_speak`` and the reason when it is False.
    """
    session = get_voice()
    result = await session.speak(body.text)
    backend = session.tts
    real = bool(getattr(backend, "real", True))
    return {
        "ok": result.ok,
        "can_speak": real,
        "backend": result.backend,
        "played": result.played,
        "audio_path": str(result.audio_path) if result.audio_path else None,
        "error": result.error,
        "duration_ms": result.duration_ms,
        "reason": "" if real else "this backend records text and plays nothing",
    }


@app.post("/voice/transcribe")
async def voice_transcribe(file: UploadFile = File(...), _: None = Protected) -> Dict[str, Any]:
    """Transcribe an uploaded audio file."""
    session = get_voice()
    suffix = Path(file.filename or "clip.wav").suffix or ".wav"
    target = session.settings.voice_path / "captures" / f"upload_{int(asyncio.get_event_loop().time() * 1000)}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(await file.read())

    result = await session.stt.transcribe(target)
    if getattr(session.stt, "real", True) is False:
        from ..voice.stt import ears_advice

        # A stand-in that echoes text is not a microphone. Answering `ok` here is how a
        # laptop with no working speech backend told a caller it had heard a recording.
        result = type(result)(
            False, result.backend, confidence=0.0, error=ears_advice(),
            meta={"can_listen": False, "saved": str(target)})
    return {
        "ok": result.ok,
        "backend": result.backend,
        "text": result.text,
        "confidence": result.confidence,
        "error": result.error,
        "duration_ms": result.duration_ms,
        # Read back from the backend's own state, after the attempt. This used to be
        # `bool(stt.real)` -- a statement about which class answered, which is why a
        # response carrying "transcription timed out" also carried `can_listen: true`.
        "state": getattr(session.stt, "state", "untested"),
        "can_listen": getattr(session.stt, "state", "untested") == "proven",
    }


@app.post("/voice/interrupt")
async def voice_interrupt(_: None = Protected) -> Dict[str, Any]:
    """Stop the current utterance (barge-in)."""
    await get_voice().stop_speaking()
    return {"ok": True, "interrupted": True}


@app.post("/voice/reset")
async def voice_reset(_: None = Protected) -> Dict[str, Any]:
    """Clear conversational state."""
    get_voice().reset()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Streaming (section 3: streaming responses)
# ---------------------------------------------------------------------------
async def _event_stream(text: str, device_id: str, session_id: str) -> AsyncIterator[str]:
    """Server-sent events: staged progress, then the final reply."""

    def frame(event: str, payload: Dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"

    yield frame("status", {"stage": "received", "text": text})
    await asyncio.sleep(0)

    core = get_core()
    request = CommandRequest(text=text, device_id=device_id, source="stream")
    if session_id:
        request.session_id = session_id

    task = asyncio.create_task(core.handle(request))

    # Emit a heartbeat while the pipeline works so the client stays responsive.
    while not task.done():
        await asyncio.sleep(0.05)
        yield frame("status", {"stage": "processing"})

    try:
        response = await task
    except Exception as exc:  # pragma: no cover - defensive
        yield frame("error", {"detail": str(exc)})
        return

    yield frame("status", {"stage": "planned", "intent": response.intent})

    for result in response.actions:
        yield frame(
            "action",
            {
                "tool": result.tool,
                "ok": result.ok,
                "verified": result.verified,
                "summary": result.summary,
                "duration_ms": result.duration_ms,
            },
        )

    # Stream the reply in word-sized chunks so TTS can start early.
    words = response.reply.split(" ")
    for index, word in enumerate(words):
        suffix = " " if index < len(words) - 1 else ""
        yield frame("token", {"text": word + suffix})
        await asyncio.sleep(0)

    yield frame(
        "done",
        {
            "reply": response.reply,
            "intent": response.intent,
            "decision": response.decision.value if response.decision else None,
            "confirmation_id": response.confirmation_id,
            "session_id": response.session_id,
            "duration_ms": response.duration_ms,
        },
    )


@app.get("/command/stream")
async def command_stream(
    text: str = Query(..., min_length=1),
    device_id: str = Query("laptop_main"),
    session_id: str = Query(""),
    _: None = Protected,
) -> StreamingResponse:
    """Stream a command's progress and reply as server-sent events."""
    return StreamingResponse(
        _event_stream(text, device_id, session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Task lifecycle (section 31)
# ---------------------------------------------------------------------------
@app.post("/tasks/{job_id}/{action}")
async def task_lifecycle(job_id: str, action: str, _: None = Protected) -> Dict[str, Any]:
    """Pause, resume, retry or cancel a job.

    ``retry`` is the one verb that runs anything: it re-issues the job's failed
    steps through the permission gate (Phase 27), so ``executed`` says how many
    actions this call actually put in front of an agent and a gated step answers
    with a fresh ``confirmation_id`` instead of running. The other three only move
    state, and ``executed`` is 0 for them -- the lesson from Phase 26, where a
    retry reported a job ``running`` that nothing had dispatched.
    """
    core = get_core()
    handlers = {
        "pause": core.tasks.pause,
        "resume": core.tasks.resume,
        "cancel": core.tasks.cancel,
    }
    if action == "retry":
        outcome = await core.retry_job(job_id)
        report = outcome.report
        confirmation = outcome.response.confirmation_id if outcome.response else ""
    else:
        handler = handlers.get(action)
        if handler is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown action {action!r}; expected one of "
                       f"{sorted([*handlers, 'retry'])}",
            )
        report = handler(job_id)
        confirmation = ""
    if not report.accepted:
        reason = report.reason or "the job cannot be " + action + "ed"
        raise HTTPException(
            status_code=404 if report.reason == "no such job" else 409, detail=reason
        )

    job = core.tasks.get(job_id)
    return {
        "ok": True,
        "action": action,
        "executed": report.executed,
        "steps_touched": report.steps_touched,
        "note": report.message,
        "confirmation_id": confirmation,
        "job": job,
    }


# ---------------------------------------------------------------------------
# Metrics and failures (section 46)
# ---------------------------------------------------------------------------
@app.get("/metrics")
async def metrics(_: None = Protected) -> Dict[str, Any]:
    """Operational counters: failures, recovery rate, latency, tool usage."""
    core = get_core()
    stats = core.recovery.failure_stats()
    try:
        recent = core.recovery.recent_failures(limit=10)
    except Exception as exc:  # pragma: no cover - DB unavailable
        # The health page has to survive the condition it exists to report: an
        # unreadable failure ledger used to raise straight through this
        # endpoint, so the operator lost the page at the moment it mattered.
        recent = []
        stats["ledger_error"] = stats.get("ledger_error") or str(exc)
    return {
        "failures": stats,
        "recent_failures": recent,
        "audit": _audit_summary(core),
        "tasks": {
            "failed": len(core.tasks.failed_jobs()),
            "recent": len(core.tasks.list(limit=25)),
        },
    }


#: The tokens this project writes into the ledger's `result` column. A value outside the set is
#: either an early build that stored a response message there, or an edit -- and the surface has to
#: tell those two apart rather than bucket both as "something".
KNOWN_RESULT_VALUES = frozenset({
    "success", "failure", "denied", "blocked", "awaiting_confirmation", "unverified",
    "no_action", "pending", "recovered", "clarification", "expired", "cancelled", "unknown",
    "",
})


def _audit_summary(core) -> Dict[str, Any]:
    """The ledger's own numbers, counted over the whole table.

    This used to read the newest 500 rows while `verify_chain` walked the *oldest* 500 of the same
    table, so on this machine's 525-row ledger the two surfaces looked at different halves: the 25
    newest rows were never chain-checked and the oldest were never counted, and neither blind spot
    was stated anywhere. Both halves now cover everything, and what the verifier could not reach is
    published as a number instead of being assumed.
    """
    counts = core.audit.result_value_counts()
    by_result: Dict[str, int] = {}
    unexpected = 0
    for value, n in counts.items():
        # A row whose result column holds a sentence is labelled honestly as a legacy message,
        # not renamed into a status that was never recorded and not dropped from the count.
        label = "legacy_message" if len(value) > 32 else (value or "unknown")
        by_result[label] = by_result.get(label, 0) + n
        if value not in KNOWN_RESULT_VALUES and len(value) <= 32:
            unexpected += n
        elif len(value) > 32:
            unexpected += n

    rows = core.audit.recent(limit=100000)
    by_tool: Dict[str, int] = {}
    for row in rows:
        tool = row["tool"] or row["intent"] or "unknown"
        by_tool[tool] = by_tool.get(tool, 0) + 1

    chain = core.audit.verify_chain()
    return {
        "sampled": sum(counts.values()),
        "ledger_rows": sum(counts.values()),
        "by_result": by_result,
        "unexpected_result_values": unexpected,
        "top_tools": dict(sorted(by_tool.items(), key=lambda kv: kv[1], reverse=True)[:10]),
        "chain_intact": chain["ok"],
        "chain_checked": chain["checked"],
        "chain_verified_of": chain["verified_of"],
        "chain_unverified": chain["unverified"],
        "chain_breaks": chain["breaks"],
        "chain_reason": chain["reason"],
        "chain_first_bad": chain.get("first_bad_log_id", ""),
    }


@app.get("/failures")
async def failures(limit: int = 20, _: None = Protected) -> List[Dict[str, Any]]:
    """The most recent recorded failures.

    An unreadable ledger answers 503 with the reason. A bare 500 gave the
    dashboard nothing, and the empty list that Phase 26 replaced this with would
    have looked exactly like a system that had never failed.
    """
    try:
        return get_core().recovery.recent_failures(limit=limit)
    except Exception as exc:  # pragma: no cover - DB unavailable
        raise HTTPException(
            status_code=503, detail=f"the failure ledger cannot be read: {exc}"
        )


# ---------------------------------------------------------------------------
# Privacy (section 49)
# ---------------------------------------------------------------------------
class DeleteBody(BaseModel):
    scope: str
    confirm: bool = False


@app.get("/privacy")
async def privacy_status(_: None = Protected) -> Dict[str, Any]:
    return describe_privacy()


@app.post("/privacy/mode")
async def privacy_mode(enabled: bool = Query(...), _: None = Protected) -> Dict[str, Any]:
    """Turn privacy mode on or off for this process."""
    get_settings().privacy_mode = bool(enabled)
    return {"privacy_mode": get_settings().privacy_mode}


@app.get("/privacy/export")
async def privacy_export(redact_output: bool = Query(True), _: None = Protected) -> Dict[str, Any]:
    """Export everything Vantage stores. Redacted unless you opt out."""
    return PrivacyManager().export(redact_output=redact_output)


@app.post("/privacy/preview")
async def privacy_preview(body: DeleteBody, _: None = Protected) -> Dict[str, Any]:
    """Show what a deletion would remove, without removing anything."""
    try:
        return {"scope": body.scope, "would_remove": PrivacyManager().preview_delete(body.scope)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/privacy/delete")
async def privacy_delete(body: DeleteBody, _: None = Protected) -> Dict[str, Any]:
    """Delete user data for a scope. Requires confirm=true -- it is irreversible."""
    try:
        return PrivacyManager().delete(body.scope, confirm=body.confirm)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ---------------------------------------------------------------------------
# Devices: pairing and transfers (v0.6)
# ---------------------------------------------------------------------------
class PairRequestBody(BaseModel):
    device_id: str
    device_name: str = ""
    public_key: str = ""


class EndpointBody(BaseModel):
    host: str = "127.0.0.1"
    port: int


# -- Companion gateway: pairing-code issuance ------------------------------

@app.post("/companion/pairing-code")
async def companion_pairing_code(_: None = Protected) -> Dict[str, Any]:
    """Issue a one-shot pairing code for the Android companion app.

    The dashboard presents this as a QR code. The Android app scans it,
    connects to the companion gateway on the advertised port (TLS-secured),
    and redeems the code for a session token.

    The companion gateway runs as a separate process serving only the narrow
    PHONE_ALLOWED surface. The main backend stays on loopback.

    **The code has to be minted by the gateway, not here.** The pairing codes
    live in the gateway's memory, because the gateway is what redeems them.
    This route used to mint one in *this* process -- so the code it handed
    out was one the gateway had never heard of, and redeeming it failed with
    "Invalid or expired pairing code". The flow this docstring describes
    could not work, and nothing called the route, so it went unnoticed.

    The code is therefore requested from the gateway's own route, over the
    loopback interface, with the master token -- the gateway accepts a
    pairing-code request only from the Mac.
    """
    from ..companion.cert import fingerprint as cert_fp
    from ..config import get_settings

    s = get_settings()
    code = _request_pairing_code_from_gateway(s)
    if not code:
        raise HTTPException(
            status_code=503,
            detail=(
                "The companion gateway is not running, so a pairing code cannot "
                "be issued. Start it (LAN mode on) and try again."
            ),
        )
    return {
        "code": code,
        "certificate_fingerprint": cert_fp(s),
        "companion_port": s.companion_port,
        "expires_in_seconds": 120,
    }


def _request_pairing_code_from_gateway(settings) -> str:
    """Ask the running gateway for a pairing code, and return it.

    The gateway holds the codes, so the gateway must issue them. Talking to it
    over loopback with the master token is the same way the rest of the
    backend reaches the companion surface.

    The gateway's certificate is the one the backend generated, so the chain is
    verified against that exact certificate. The hostname is not checked,
    because the certificate is issued for the laptop's *name* and this call
    dials it by loopback address — the same distinction the gateway's own
    client makes, and the same guarantee: a MITM presenting any *other*
    certificate still fails, and verification is never disabled.
    """
    import ssl

    import httpx

    from ..companion.cert import cert_path, ensure

    try:
        ensure(settings)
        cert = cert_path(settings)
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(str(cert))
    except Exception:
        return ""

    url = f"https://127.0.0.1:{settings.companion_port}/companion/pairing-code"
    try:
        with httpx.Client(timeout=10.0, verify=context) as client:
            response = client.post(
                url,
                headers={"X-JARVIS-Token": settings.api_token},
            )
    except Exception:
        return ""
    if response.status_code != 200:
        return ""
    try:
        return str(response.json().get("code") or "")
    except Exception:
        return ""


@app.post("/devices/pair/request")
async def pair_request(body: PairRequestBody) -> Dict[str, Any]:
    """A device asks to pair.

    This is the **only unauthenticated write path**. A device cannot hold the
    API token before it is trusted, so it must be able to knock. That is safe
    because a request only ever creates a *pending* record: it grants no access,
    performs no action, and is useless until an authenticated user approves it.
    Pending requests are also capped to prevent flooding.
    """
    manager = PairingManager()
    try:
        request = manager.request(
            device_id=body.device_id.strip(),
            device_name=body.device_name.strip() or body.device_id.strip(),
            public_key=body.public_key,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc))

    get_core().audit.record(
        intent="device.pair.request",
        tool="pair_request",
        target=request.device_id,
        risk="external",
        result="pending",
        detail={"pairing_id": request.pairing_id},
    )
    return {
        "pairing_id": request.pairing_id,
        "device_id": request.device_id,
        "state": request.state,
        "message": "Waiting for the user to approve this pairing request.",
    }


@app.get("/devices/pair/pending")
async def pair_pending(_: None = Protected) -> List[Dict[str, Any]]:
    return PairingManager().pending()


@app.post("/devices/pair/{pairing_id}/approve")
async def pair_approve(pairing_id: str, _: None = Protected) -> Dict[str, Any]:
    """Trust a device. The one place trust is granted."""
    manager = PairingManager()
    try:
        result = manager.approve(pairing_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    get_core().audit.record(
        intent="device.pair.approve",
        tool="approve_pairing",
        target=result["device_id"],
        risk="high",
        confirmation=True,
        result="success",
        detail={"fingerprint": result["fingerprint"]},
    )
    return result


@app.post("/devices/pair/{pairing_id}/deny")
async def pair_deny(pairing_id: str, _: None = Protected) -> Dict[str, Any]:
    manager = PairingManager()
    try:
        result = manager.deny(pairing_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    get_core().audit.record(
        intent="device.pair.deny",
        tool="deny_pairing",
        target=pairing_id,
        risk="medium",
        result="denied",
    )
    return result


@app.post("/devices/{device_id}/revoke")
async def device_revoke(device_id: str, _: None = Protected) -> Dict[str, Any]:
    """Remove trust and destroy the device's key."""
    manager = PairingManager()
    try:
        result = manager.revoke(device_id)
    except HostDeviceError as exc:
        get_core().audit.record(
            intent="device.revoke",
            tool="remove_phone",
            target=device_id,
            risk="high",
            confirmation=True,
            result="denied",
            detail={"reason": str(exc)},
        )
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    get_core().audit.record(
        intent="device.revoke",
        tool="remove_phone",
        target=device_id,
        risk="high",
        confirmation=True,
        result="success",
    )
    return result


@app.post("/emergency_stop")
async def emergency_stop(_: None = Protected) -> Dict[str, Any]:
    """Pull the existing emergency stop from a client, without typing its sentence.

    §30 of the build prompt names the phone, the menu bar and the task screen as places a stop
    must be reachable from. There is no second stop implementation here: the route calls the
    registered PHYSICAL-tier tool and returns what it measured, including the part it cannot
    stop. It asks for no confirmation -- a stop that needs approval first is not an emergency
    stop -- and it is written to the ledger like any other action.
    """
    core = get_core()
    handler = core.tools.handler("emergency_stop")
    if handler is None:
        raise HTTPException(status_code=503, detail="the emergency stop is not available")

    result = await handler(_tool_context())
    detail = {
        "message": result.message,
        # Carried inside the result, not only at the top: the caller must be able to see that
        # "verified" here means the job rows were re-read, and not infer it from HTTP 200.
        "verified": result.verified,
        "jobs_cancelled": result.output.get("jobs_cancelled", 0),
        "cancelled_job_ids": result.output.get("cancelled_job_ids", []),
        "jobs_stuck": result.output.get("jobs_stuck", []),
        "physical_stop": result.output.get("physical_stop", ""),
    }
    core.audit.record(
        intent="emergency.stop",
        tool="emergency_stop",
        target="laptop_main",
        risk="critical",
        confirmation=False,
        result="success" if result.verified else "unverified",
        detail=detail,
    )
    return {"ok": result.ok, "verified": result.verified, "result": detail}


class RevokeBody(BaseModel):
    """A button posts no body at all, so the whole thing is optional -- a scope is a detail
    the caller may name, not a form the caller must fill to be refused."""
    device: Optional[str] = None


def _loopback_only_or_refuse(request: Request) -> None:
    """Refuse a permission request that did not come from the laptop itself.

    A system dialog appears in front of the machine, over the whole screen, and only someone
    sitting there can answer it honestly. A handset on the wifi -- or anything that has a
    stolen session token -- must not be able to raise prompts on the owner's display, so this
    action is limited to the loopback interface regardless of authorisation.
    """
    client = (request.client.host if request.client else "") or ""
    if client not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(
            status_code=403,
            detail="permissions are granted on the laptop itself; a remote device cannot ask "
                   "for them on your behalf.",
        )


@app.post("/permissions/{capability}/request")
async def permission_request(capability: str, request: Request,
                             _: None = Protected) -> Dict[str, Any]:
    """Ask macOS for one permission, because a person at the laptop pressed the button.

    The status endpoint stays read-only -- it is polled, and a poll that raises a dialog is a
    consent attack. This is the deliberate half: one capability, one click, on this machine.
    """
    _loopback_only_or_refuse(request)
    from ..permissions.service import PermissionStatusService

    service = PermissionStatusService(settings=get_settings())
    try:
        return service.request(capability)
    except ValueError as exc:
        # An unknown capability is a 404; a known one that cannot be asked about is a refusal
        # with the reason, which is a different answer to a different question.
        known = any(c.name == capability for c in service.capabilities())
        status = 409 if known else 404
        get_core().audit.record(
            intent="permission.request", tool="request_permission", target=capability,
            risk="high", confirmation=False, result="denied",
            detail={"reason": str(exc), "known_capability": known},
        )
        raise HTTPException(status_code=status, detail=str(exc))


@app.get("/permissions/policy")
async def permission_policy(_: None = Protected) -> List[Dict[str, Any]]:
    """The explicit grants and denials the engine currently honours."""
    rows = get_core().db.query(
        "SELECT tool, device, granted, granted_at, revoked_at FROM permissions "
        "ORDER BY rowid DESC LIMIT 200")
    return [dict(r) for r in rows]


@app.post("/permissions/{tool}/revoke")
async def permission_revoke(
    tool: str, body: RevokeBody = RevokeBody(), _: None = Protected
) -> Dict[str, Any]:
    """Take a capability away. The logic existed; the handle did not."""
    core = get_core()
    spec = core.tools.get(tool)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"no capability named {tool!r}")

    canonical = core.tools.canonical(tool)
    if canonical == "emergency_stop":
        core.audit.record(
            intent="tool.revoke", tool="revoke_tool", target=canonical, risk="high",
            confirmation=False, result="denied",
            detail={"reason": "the emergency stop is a safety interlock and cannot be revoked"},
        )
        raise HTTPException(
            status_code=409,
            detail="emergency_stop is the stop itself; revoking it would remove a safety door.",
        )

    core.policy.revoke(canonical, device=body.device)
    blocked = core.policy.evaluate(PlannedAction(
        step_id=0, tool=canonical, params={}, device=body.device or "laptop_main",
    )).decision is PolicyDecision.BLOCK
    core.audit.record(
        intent="tool.revoke", tool="revoke_tool", target=canonical, risk="high",
        confirmation=False, result="success",
        detail={"device": body.device, "blocked_after": blocked},
    )
    return {
        "revoked": True,
        "tool": canonical,
        "device": body.device,
        "aliases": list(spec.aliases),
        "state": {"blocked": blocked, "standing_grant": core.policy.is_granted(canonical)},
    }


@app.post("/permissions/{tool}/restore")
async def permission_restore(
    tool: str, body: RevokeBody = RevokeBody(), _: None = Protected
) -> Dict[str, Any]:
    """Lift an explicit denial, and nothing more.

    ``grant`` would write granted=1, which also downgrades a CONFIRM-tier tool to EXECUTE for
    reversible actions. Un-revoking must return a capability to its *declared* policy, not hand
    it authority the user never offered -- so this removes the denial rows instead.
    """
    core = get_core()
    spec = core.tools.get(tool)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"no capability named {tool!r}")

    canonical = core.tools.canonical(tool)
    spellings = core.tools.spellings(canonical)
    placeholders = ", ".join("?" for _ in spellings)
    rows = core.db.query(
        f"SELECT rowid FROM permissions WHERE tool IN ({placeholders}) AND granted = 0",
        tuple(spellings))
    if not rows:
        return {"restored": False, "tool": canonical,
                "reason": "there was no denial to remove; the tool already follows its "
                          "declared policy"}

    core.db.execute(
        f"DELETE FROM permissions WHERE tool IN ({placeholders}) AND granted = 0",
        tuple(spellings))
    core.audit.record(
        intent="tool.revoke", tool="restore_tool", target=canonical, risk="high",
        confirmation=False, result="success",
        detail={"denials_removed": len(rows),
                "standing_grant": core.policy.is_granted(canonical)},
    )
    return {
        "restored": True,
        "tool": canonical,
        "denials_removed": len(rows),
        "state": {
            "revoked": core.policy.is_revoked(canonical),
            "standing_grant": core.policy.is_granted(canonical),
        },
    }


@app.post("/devices/{device_id}/endpoint")
async def device_endpoint(
    device_id: str, body: EndpointBody, _: None = Protected
) -> Dict[str, Any]:
    """Record where a device's transfer listener can be reached.

    Kept separate from pairing so the address is configuration, not identity.
    """
    core = get_core()
    device = core.devices.get(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail=f"unknown device {device_id!r}")

    device.metadata["endpoint_host"] = body.host
    device.metadata["endpoint_port"] = int(body.port)
    core.devices.register(device)
    return {
        "device_id": device_id,
        "endpoint_host": body.host,
        "endpoint_port": body.port,
    }


@app.get("/transfers")
async def transfers(limit: int = 20, _: None = Protected) -> Dict[str, Any]:
    server = TransferServer()
    return {"stats": server.stats(), "transfers": server.recent(limit=limit)}


@app.get("/transfers/{transfer_id}")
async def transfer_detail(transfer_id: str, _: None = Protected) -> Dict[str, Any]:
    record = TransferServer().get_transfer(transfer_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no transfer {transfer_id!r}")
    return record


@app.post("/transfers/{transfer_id}/cancel")
async def transfer_cancel(transfer_id: str, _: None = Protected) -> Dict[str, Any]:
    """Cancel a transfer that has not finished."""
    from ..transport.protocol import TransferState, can_transition

    server = TransferServer()
    record = server.get_transfer(transfer_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no transfer {transfer_id!r}")

    current = TransferState(record["state"])
    if not can_transition(current, TransferState.CANCELLED):
        raise HTTPException(
            status_code=400,
            detail=f"a transfer in state {current.value} can no longer be cancelled",
        )

    server.db.execute(
        "UPDATE transfers SET state = ?, completed_at = ? WHERE transfer_id = ?",
        (TransferState.CANCELLED.value, utcnow().isoformat(), transfer_id),
    )
    server._record(transfer_id, TransferState.CANCELLED, "cancelled via API")
    get_core().audit.record(
        intent="transfer.cancel",
        tool="cancel_transfer",
        target=transfer_id,
        risk="low",
        result="success",
    )
    return {"transfer_id": transfer_id, "state": "cancelled"}


# ---------------------------------------------------------------------------
# Email + Calendar (v0.7)
# ---------------------------------------------------------------------------
class ContactBody(BaseModel):
    name: str
    address: str
    aliases: List[str] = []


@app.get("/integrations")
async def integrations(_: None = Protected) -> Dict[str, Any]:
    """Which backend each integration is using, and whether it needs an account."""
    return {"email": describe_email(), "calendar": describe_calendar()}


@app.get("/email/search")
async def email_search(
    query: str = Query(""),
    unread_only: bool = Query(False),
    limit: int = Query(10),
    _: None = Protected,
) -> Dict[str, Any]:
    provider = get_email_provider()
    messages = provider.search(query=query, limit=max(1, min(limit, 50)), unread_only=unread_only)
    return {"provider": provider.name, "count": len(messages), "results": [m.as_dict() for m in messages]}


@app.get("/email/{uid}")
async def email_read(uid: str, _: None = Protected) -> Dict[str, Any]:
    provider = get_email_provider()
    message = provider.read(uid)
    if message is None:
        raise HTTPException(status_code=404, detail=f"no message {uid!r}")
    return {"provider": provider.name, "message": message}


@app.get("/email-drafts")
async def email_drafts(_: None = Protected) -> List[Dict[str, Any]]:
    """Unsent drafts. Sending one still requires an explicit confirmation."""
    rows = get_db().query(
        "SELECT draft_id, to_addresses, subject, state, created_at FROM email_drafts "
        "ORDER BY created_at DESC LIMIT 25"
    )
    return [dict(r) for r in rows]


@app.post("/email/{uid}/task")
async def task_from_email_route(uid: str, body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    """Turn a message into a planner task linked back to it.

    The workflow existed only as a chat sentence, so the inbox could read a message
    and do nothing with it. The task's title comes from the person or the message's
    subject — never the body — and the link is recorded in the task's detail.
    """
    from ..tools.base import ToolContext
    from ..tools.email import task_from_email

    body = body or {}
    result = await task_from_email(
        ToolContext(), uid=uid, title=str(body.get("title") or ""),
        due=str(body.get("due") or ""), priority=str(body.get("priority") or "normal"))
    ok_flag = bool(getattr(result, "ok", False))
    if not ok_flag:
        raise HTTPException(
            status_code=422,
            detail=getattr(result, "error", "") or "The task could not be created.")
    output = getattr(result, "output", {}) or {}
    return {"ok": True, "task": output.get("task"), "email": output.get("email"),
            "message": getattr(result, "message", "")}


@app.post("/email-drafts")
async def create_email_draft(body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    """Compose and store a draft. Sends nothing.

    Drafts previously existed only if the chat route created one, so the operator
    page could list and send them but never write one -- a workflow with a missing
    middle. This is the same tool the chat route uses, so an ambiguous recipient is
    refused here too rather than guessed.
    """
    from ..tools.base import ToolContext
    from ..tools.email import draft_email

    body = body or {}
    to = str(body.get("to") or "").strip()
    if not to:
        raise HTTPException(status_code=422, detail="Who should the email go to?")

    result = await draft_email(
        ToolContext(), to=to,
        subject=str(body.get("subject") or ""),
        body=str(body.get("body") or ""),
        attachments=str(body.get("attachments") or ""),
    )
    if not getattr(result, "ok", False):
        raise HTTPException(
            status_code=422,
            detail=getattr(result, "error", "") or "The draft could not be saved.")
    output = getattr(result, "output", {}) or {}
    return {"ok": True, "draft": output, "message": getattr(result, "message", "")}


@app.post("/email-drafts/{draft_id}/send")
async def send_email_draft(draft_id: str, body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    """Send a stored draft. Sending is irreversible, so it needs `confirm: true`.

    A draft that already left (or was discarded) is refused rather than re-sent:
    `send_email` checks the record's state, and this route does not bypass it. The
    reply reports what the *provider* said, so "sent" here means an SMTP/maildir
    write actually happened -- not that a button was pressed.
    """
    from ..tools.base import ToolContext
    from ..tools.email import send_email

    body = body or {}
    if not body.get("confirm"):
        raise HTTPException(
            status_code=409,
            detail="Sending an email needs explicit confirmation (confirm: true).")

    result = await send_email(ToolContext(), draft_id=draft_id)
    sent = bool(getattr(result, "ok", False))
    return {
        "ok": sent,
        "draft_id": draft_id,
        "message": (getattr(result, "message", "") or getattr(result, "error", "")
                    or ("Sent." if sent else "The provider did not accept the message.")),
        "verified": bool(getattr(result, "verified", False)),
    }


@app.post("/contacts")
async def add_contact_endpoint(body: ContactBody, _: None = Protected) -> Dict[str, Any]:
    """Register a contact so a name can be resolved to an address."""
    from ..tools.email import add_contact

    contact_id = add_contact(body.name, body.address, body.aliases)
    get_core().audit.record(
        intent="contact.add",
        tool="add_contact",
        target=body.address,
        risk="low",
        result="success",
    )
    return {"contact_id": contact_id, "name": body.name, "address": body.address}


# ---------------------------------------------------------------------------
# Professional posts (Feature 6) — draft / schedule / remind, never auto-publish.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Feature 7 — turn a goal into a plan of real tasks (connected workflow).
# ---------------------------------------------------------------------------
@app.post("/planner/plan-goal")
async def plan_goal(body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    """Break a goal into concrete planner tasks.

    A plan is a set of *proposals*: this creates real tasks with titles, a stated
    order (dependencies), and an optional project tag — nothing external is
    scheduled and no calendar event is created. The person then works, reschedules
    or deletes them; the calendar write, if any, still goes through the permission
    gate.

    Decomposition is deterministic and honest: the goal's own wording becomes the
    first task, followed by the standard steps a piece of work needs (clarify its
    scope, do it, verify it). A model, when configured, may refine the titles —
    but its output is validated, and a proposal is not an execution.
    """
    body = body or {}
    goal = str(body.get("goal") or "").strip()
    if not goal:
        raise HTTPException(status_code=422, detail="a goal needs text")
    project = str(body.get("project") or "").strip()
    service = get_core().planner_service

    goal_clean = goal.rstrip(".")
    seed_titles = [
        f"Define what “{goal_clean}” must include",
        goal_clean,
        f"Verify “{goal_clean}” is done",
    ]
    created = []
    prev_ref = ""
    for title in seed_titles:
        task = service.create_task(
            title, priority="high", project=project,
            source="plan",
            depends_on=[prev_ref] if prev_ref else [],
        )
        created.append(task.as_dict())
        prev_ref = task.task_ref

    get_core().audit.record(
        intent="planner.plan_goal", tool="plan_goal", target=goal_clean,
        risk="low", result="success", detail={"tasks": len(created), "project": project})

    return {
        "goal": goal_clean,
        "project": project,
        "tasks": created,
        "note": (
            "These are saved tasks, in order. Nothing was scheduled on your calendar; "
            "schedule a task separately when you want a time for it."
        ),
    }


@app.post("/planner/start-day")
async def start_day(body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    """The "start my day" workflow: one call that assembles a real, actionable plan.

    It composes state Vantage actually holds — today's calendar (read now, an
    unreadable one reported as such), the planner's tasks with their blocking and
    overdue state, existing calendar conflicts, and anything that needs attention
    — then states the first thing to do and *why*. It creates nothing: no events,
    no tasks. Scheduling remains a separate, authorised action.

    ``workflow`` in the reply is the ordered set of steps that were run, so the
    caller can see the composition rather than trusting a single number.
    """
    from datetime import datetime as _dt, timedelta as _td

    from ..integrations.calendar_provider import get_calendar_provider, read_status

    core = get_core()
    now = _dt.now().astimezone()
    day = now.date()
    start = _dt.combine(day, _dt.min.time(), tzinfo=now.tzinfo)
    end = start + _td(days=1)
    steps: List[str] = []

    events: List[Any] = []
    failed = False
    try:
        result = get_calendar_provider(get_settings()).list_events(start, end, limit=100)
        if read_status(result).read_succeeded:
            events = list(result)
            steps.append(f"read {len(events)} calendar event(s)")
        else:
            failed = True
            steps.append("calendar could not be read")
    except Exception as exc:
        failed = True
        steps.append(f"calendar read failed ({str(exc)[:60]})")

    agenda = core.planner_service.todays_agenda(
        day=day, now=now, calendar_events=events, calendar_read_failed=failed)
    steps.append(f"evaluated {len(agenda['tasks'])} open task(s)")

    attention: List[Dict[str, Any]] = []
    try:
        attention = (await attention_endpoint(_)).get("items", [])
        steps.append(f"collected {len(attention)} attention item(s)")
    except Exception:
        steps.append("attention check failed")

    nxt = agenda.get("next_action")
    headline = (
        f"Start with: {nxt['task']['title']} ({nxt['rule']})"
        if nxt else "Nothing is pending — the day is clear of open tasks."
    )

    return {
        "day": day.isoformat(),
        "headline": headline,
        "next_action": nxt,
        "calendar_read_failed": failed,
        "events": agenda["events"],
        "conflicts": agenda["conflicts"],
        "overdue": agenda["overdue"],
        "blocked": agenda["blocked"],
        "attention": attention,
        "task_count": len(agenda["tasks"]),
        "workflow": steps,
        "note": "This assembled your plan from real data and changed nothing.",
    }


def _as_local(value: Any) -> Any:
    """A stored datetime as aware local time, so comparisons never mix tz-naive and aware."""
    if value is None or getattr(value, "tzinfo", None) is not None:
        return value
    return value.astimezone()


def build_meeting_briefing(wanted: str = "") -> Dict[str, Any]:
    """Assemble a meeting briefing from real calendar, planner and mail data.

    Extracted from the route so the chat path and the HTTP path produce the same
    answer: "prepare me for the next meeting" used to fall to `next_event` and
    report only the time, while the same words on the planner page produced a
    full brief.
    """
    from datetime import datetime as _dt, timedelta as _td

    from ..integrations.calendar_provider import get_calendar_provider, read_status

    now = _dt.now().astimezone()
    horizon = now + _td(days=14)
    steps: List[str] = []
    empty = {"meeting": None, "related_email": [], "related_tasks": [],
             "talking_points": [], "workflow": steps}

    try:
        result = get_calendar_provider(get_settings()).list_events(
            now - _td(hours=1), horizon, limit=200)
    except Exception as exc:
        steps.append(f"calendar read failed ({str(exc)[:60]})")
        return {"ok": False, "reason": "calendar could not be read", **empty}
    if not read_status(result).read_succeeded:
        steps.append("calendar could not be read")
        return {"ok": False, "reason": "calendar could not be read", **empty}

    events = list(result)
    steps.append(f"read {len(events)} calendar event(s) in the next two weeks")

    if wanted:
        needle = wanted.lower()
        matches = [e for e in events if needle in (e.title or "").lower()]
        if not matches:
            return {"ok": False,
                    "reason": f"no meeting matching {wanted!r} in the next two weeks", **empty}
        if len(matches) > 1:
            return {"ok": False, "reason": "more than one meeting matches",
                    "candidates": [e.as_dict() for e in matches[:5]], **empty}
        meeting = matches[0]
    else:
        upcoming = [e for e in events if _as_local(e.start) >= now]
        if not upcoming:
            return {"ok": False, "reason": "no upcoming meeting in the next two weeks", **empty}
        meeting = min(upcoming, key=lambda e: _as_local(e.start))

    steps.append(f"selected {meeting.title or '(untitled event)'}")

    related_tasks: List[Dict[str, Any]] = []
    try:
        for task in get_core().planner_service.list_tasks():
            hay = f"{task.title} {task.detail}".lower()
            if meeting.title and meeting.title.lower() in hay:
                related_tasks.append({"task_ref": task.task_ref, "title": task.title,
                                      "due_at": task.due_at.isoformat() if task.due_at else None})
        steps.append(f"matched {len(related_tasks)} open task(s)")
    except Exception as exc:
        steps.append(f"planner read failed ({str(exc)[:60]})")

    import re as _re

    queries = [meeting.title] if meeting.title else []
    queries += _re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", meeting.description or "")

    related: List[Dict[str, Any]] = []
    mail_error = ""
    mail_read = False
    try:
        from ..integrations.email_provider import get_email_provider

        mail = get_email_provider(get_settings())
        seen: set = set()
        for q in queries[:3]:
            if not q:
                continue
            found = mail.search(query=q, limit=5)
            if not read_status(found).read_succeeded:
                mail_error = "mailbox could not be read"
                continue
            mail_read = True
            for m in found:
                if m.uid in seen:
                    continue
                seen.add(m.uid)
                related.append({"uid": m.uid, "subject": m.subject, "from": m.sender,
                                "date": m.date, "snippet": m.snippet})
        steps.append(
            f"searched the mailbox ({len(related)} related message(s))" if mail_read
            else "mailbox could not be read")
    except Exception as exc:
        mail_error = f"mailbox read failed: {str(exc)[:80]}"
        steps.append("mailbox read failed")

    points: List[str] = []
    if related:
        points.append(f"{len(related)} related message(s): "
                      + "; ".join(m["subject"] or "(no subject)" for m in related[:3]))
    if related_tasks:
        points.append(f"{len(related_tasks)} open task(s) mention this meeting.")
    if meeting.description:
        points.append("Agenda/notes: " + meeting.description[:200])
    if meeting.location:
        points.append(f"Location: {meeting.location}")
    if not points:
        points.append("No related mail, tasks or agenda found for this meeting.")

    where = " ".join(filter(None, [meeting.location, meeting.description]))

    return {
        "ok": True,
        "meeting": meeting.as_dict(),
        "related_email": related,
        "related_tasks": related_tasks,
        "talking_points": points,
        "email_error": mail_error,
        "conflict": _meeting_conflict(events, meeting),
        "prep_actions": [
            {"action": "search_events", "params": {"query": meeting.title or ""}},
            {"action": "open_meeting", "params": {"meeting": meeting.title or "",
                                                  "join_url": _join_url(where)}},
        ],
        "workflow": steps,
        "note": "A briefing assembled from real data \u2014 Vantage prepared it and does not attend the meeting. Nothing was sent or changed.",
    }


@app.post("/meetings/prepare")
async def prepare_meeting(body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    """Assemble a briefing for a meeting, from real calendar, planner and mail.

    Finds the event (by name, or the next one if none is named), then gathers the
    open planner tasks that mention it and searches the mailbox for messages
    naming the meeting or an address in its description. An unreadable calendar
    or mailbox is reported, never rendered as "no meeting" or "no related mail",
    and an ambiguous name asks which meeting rather than picking one.

    This *prepares*; it does not attend, join or record anything. Nothing is sent
    or changed, and ``workflow`` lists the steps that actually ran.
    """
    body = body or {}
    return build_meeting_briefing(str(body.get("meeting") or "").strip())


def _as_local(value: Any) -> Any:
    """A stored datetime as aware local time, so comparisons never mix tz-naive and aware."""
    if value is None or getattr(value, "tzinfo", None) is not None:
        return value
    return value.astimezone()


def build_meeting_briefing(wanted: str = "") -> Dict[str, Any]:
    """Assemble a meeting briefing from real calendar, planner and mail data.

    Extracted from the route so the chat path and the HTTP path produce the same
    answer: "prepare me for the next meeting" used to fall to `next_event` and
    report only the time, while the same words on the planner page produced a
    full brief.
    """
    from datetime import datetime as _dt, timedelta as _td

    from ..integrations.calendar_provider import get_calendar_provider, read_status

    now = _dt.now().astimezone()
    horizon = now + _td(days=14)
    steps: List[str] = []
    empty = {"meeting": None, "related_email": [], "related_tasks": [],
             "talking_points": [], "workflow": steps}

    try:
        result = get_calendar_provider(get_settings()).list_events(
            now - _td(hours=1), horizon, limit=200)
    except Exception as exc:
        steps.append(f"calendar read failed ({str(exc)[:60]})")
        return {"ok": False, "reason": "calendar could not be read", **empty}
    if not read_status(result).read_succeeded:
        steps.append("calendar could not be read")
        return {"ok": False, "reason": "calendar could not be read", **empty}

    events = list(result)
    steps.append(f"read {len(events)} calendar event(s) in the next two weeks")

    if wanted:
        needle = wanted.lower()
        matches = [e for e in events if needle in (e.title or "").lower()]
        if not matches:
            return {"ok": False,
                    "reason": f"no meeting matching {wanted!r} in the next two weeks", **empty}
        if len(matches) > 1:
            return {"ok": False, "reason": "more than one meeting matches",
                    "candidates": [e.as_dict() for e in matches[:5]], **empty}
        meeting = matches[0]
    else:
        upcoming = [e for e in events if _as_local(e.start) >= now]
        if not upcoming:
            return {"ok": False, "reason": "no upcoming meeting in the next two weeks", **empty}
        meeting = min(upcoming, key=lambda e: _as_local(e.start))

    steps.append(f"selected {meeting.title or '(untitled event)'}")

    related_tasks: List[Dict[str, Any]] = []
    try:
        for task in get_core().planner_service.list_tasks():
            hay = f"{task.title} {task.detail}".lower()
            if meeting.title and meeting.title.lower() in hay:
                related_tasks.append({"task_ref": task.task_ref, "title": task.title,
                                      "due_at": task.due_at.isoformat() if task.due_at else None})
        steps.append(f"matched {len(related_tasks)} open task(s)")
    except Exception as exc:
        steps.append(f"planner read failed ({str(exc)[:60]})")

    import re as _re

    queries = [meeting.title] if meeting.title else []
    queries += _re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", meeting.description or "")

    related: List[Dict[str, Any]] = []
    mail_error = ""
    mail_read = False
    try:
        from ..integrations.email_provider import get_email_provider

        mail = get_email_provider(get_settings())
        seen: set = set()
        for q in queries[:3]:
            if not q:
                continue
            found = mail.search(query=q, limit=5)
            if not read_status(found).read_succeeded:
                mail_error = "mailbox could not be read"
                continue
            mail_read = True
            for m in found:
                if m.uid in seen:
                    continue
                seen.add(m.uid)
                related.append({"uid": m.uid, "subject": m.subject, "from": m.sender,
                                "date": m.date, "snippet": m.snippet})
        steps.append(
            f"searched the mailbox ({len(related)} related message(s))" if mail_read
            else "mailbox could not be read")
    except Exception as exc:
        mail_error = f"mailbox read failed: {str(exc)[:80]}"
        steps.append("mailbox read failed")

    points: List[str] = []
    if related:
        points.append(f"{len(related)} related message(s): "
                      + "; ".join(m["subject"] or "(no subject)" for m in related[:3]))
    if related_tasks:
        points.append(f"{len(related_tasks)} open task(s) mention this meeting.")
    if meeting.description:
        points.append("Agenda/notes: " + meeting.description[:200])
    if meeting.location:
        points.append(f"Location: {meeting.location}")
    if not points:
        points.append("No related mail, tasks or agenda found for this meeting.")

    where = " ".join(filter(None, [meeting.location, meeting.description]))

    return {
        "ok": True,
        "meeting": meeting.as_dict(),
        "related_email": related,
        "related_tasks": related_tasks,
        "talking_points": points,
        "email_error": mail_error,
        "conflict": _meeting_conflict(events, meeting),
        "prep_actions": [
            {"action": "search_events", "params": {"query": meeting.title or ""}},
            {"action": "open_meeting", "params": {"meeting": meeting.title or "",
                                                  "join_url": _join_url(where)}},
        ],
        "workflow": steps,
        "note": "A briefing assembled from real data \u2014 Vantage prepared it and does not attend the meeting. Nothing was sent or changed.",
    }


def _calling_available() -> bool:
    """Whether this Mac has the calling app the call route hands a number to."""
    from pathlib import Path as _P

    return _P("/System/Applications/FaceTime.app").exists()


@app.get("/calls/availability")
async def call_availability(_: None = Protected) -> Dict[str, Any]:
    """Whether this Mac has a real, scriptable calling surface.

    FaceTime's ``facetime://`` URL scheme is the whole authority here: if the app
    is absent there is no call path, and the page must say so rather than offer a
    button that cannot work.
    """
    from pathlib import Path as _P

    facetime = _P("/System/Applications/FaceTime.app").exists()
    return {
        "available": facetime,
        "provider": "FaceTime" if facetime else "",
        "detail": ("Calls go through FaceTime, which is installed."
                   if facetime else
                   "FaceTime is not installed, so there is no calling app to hand a number to."),
        "requires_confirmation": True,
        "can_observe_call": False,
    }


@app.post("/contacts/{name}/call")
async def place_call(name: str, body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    """Hand a contact's number to FaceTime. Explicitly, and only on request.

    Two boundaries are kept deliberately:

    * this **places** a call, it does not observe one. Vantage has no access to
      the audio, so it never reports that a conversation happened, who joined, or
      what was said;
    * it only runs when the caller passes ``confirm: true``. The prepare screen
      offers the call; dialling is a second, deliberate act, because ringing
      somebody is not reversible.

    The reply says what was actually done — handed to FaceTime — and no more.
    """
    from ..tools.email import resolve_recipient

    body = body or {}
    if not body.get("confirm"):
        raise HTTPException(
            status_code=409,
            detail="Placing a call needs explicit confirmation (confirm: true).")

    from pathlib import Path as _P

    if not _P("/System/Applications/FaceTime.app").exists():
        raise HTTPException(status_code=503,
                            detail="FaceTime is not installed, so there is no app to call from.")

    resolution = resolve_recipient(name)
    if not resolution.get("ok"):
        raise HTTPException(status_code=404, detail=resolution.get("reason", "no such contact"))
    address = str(resolution.get("address") or "")
    display = str(resolution.get("name") or name)

    number = str(body.get("number") or "").strip()
    target = number or address
    if not target:
        raise HTTPException(status_code=422, detail="that contact has no address to call")

    import asyncio
    import subprocess

    # LaunchServices resolves the `facetime://` scheme to FaceTime. `open -a
    # FaceTime <url>` is used rather than bare `open <url>` so the app is named
    # explicitly instead of leaving the handler to whatever registered last.
    def _dial() -> tuple:
        try:
            done = subprocess.run(["open", "-a", "FaceTime", f"facetime://{target}"],
                                  capture_output=True, text=True, timeout=20)
            return done.returncode, (done.stderr or "").strip()
        except Exception as exc:  # a launch that failed must not read as a call
            return 1, str(exc)[:160]

    code, err = await asyncio.to_thread(_dial)
    return {
        "ok": code == 0,
        "contact": {"name": display, "address": address},
        "dialled": target,
        "provider": "FaceTime",
        "message": (f"Asked FaceTime to call {display}." if code == 0
                    else f"Could not hand {display} to FaceTime: {err}"),
        "observed": False,
        "note": "Handed to FaceTime to ring. Vantage cannot see or hear the call, so it "
                "will never claim the conversation happened — record what was said when "
                "you are done.",
    }


@app.post("/contacts/{name}/call-notes")
async def call_notes(name: str, body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    """Record what was said on a call and turn the agreed actions into tasks.

    This is the half Vantage *can* do honestly. It never heard the call, so
    nothing here is inferred — the notes are the person's own words, and only the
    action lines they wrote become tasks, prefixed with the contact so the work is
    traceable back to the conversation.
    """
    import re as _re

    from ..tools.email import resolve_recipient

    body = body or {}
    notes = str(body.get("notes") or "").strip()
    raw_items = body.get("action_items") or []
    if isinstance(raw_items, str):
        raw_items = raw_items.splitlines()

    resolution = resolve_recipient(name)
    display = str(resolution.get("name") or name)

    items = [str(i).strip() for i in raw_items if str(i).strip()]
    if not items and notes:
        items = [line.strip(" -•*\t") for line in notes.splitlines()
                 if _re.match(r"^\s*[-•*]\s+\S", line)]

    created = []
    planner = get_core().planner_service
    for item in items:
        task = planner.create_task(f"[Call with {display}] {item}"[:200],
                                   detail="Agreed on a call.", source="call")
        created.append({"task_ref": task.task_ref, "title": task.title})

    return {
        "ok": True,
        "contact": display,
        "created": created,
        "created_count": len(created),
        "notes_saved": bool(notes),
        "note": ("Only the action lines you wrote became tasks; nothing was inferred "
                 "from the call itself." if items
                 else "No action items were written, so no tasks were created."),
    }


@app.post("/contacts/{name}/outreach")
async def prepare_outreach(name: str, body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    """Draft a personalised outreach email to a contact, and save it as a draft.

    Built from what Vantage actually knows about the person: open tasks that name
    them and recent mail from them, so the message can reference something real
    instead of a generic template. It is written to the mailbox's **drafts** folder
    and never sent — sending is a separate, confirmed action on this project.
    """
    from ..integrations.email_provider import EmailDraft, get_email_provider, read_status
    from ..tools.email import resolve_recipient

    resolution = resolve_recipient(name)
    if not resolution.get("ok"):
        raise HTTPException(status_code=404, detail=resolution.get("reason", "no such contact"))
    address = str(resolution.get("address") or "")
    display = str(resolution.get("name") or name)

    body = body or {}
    purpose = str(body.get("purpose") or "").strip()
    save = bool(body.get("save_draft", True))

    open_tasks = []
    for task in get_core().planner_service.list_tasks():
        hay = f"{task.title} {task.detail}".lower()
        if display.lower() in hay or address.lower() in hay:
            open_tasks.append(task.title)

    recent: List[str] = []
    mail_error = ""
    try:
        provider = get_email_provider(get_settings())
        found = provider.search(query=display, limit=5)
        if read_status(found).read_succeeded:
            recent = [m.subject for m in found if address.lower() in (m.sender or "").lower()]
        else:
            mail_error = "mailbox could not be read"
    except Exception as exc:
        mail_error = str(exc)[:120]

    greeting = f"Hi {display.split()[0]}," if display.split() else "Hi,"
    lines = [greeting, ""]
    if purpose:
        lines.append(purpose)
    elif open_tasks:
        lines.append(f"I wanted to follow up on {open_tasks[0]}.")
    elif recent:
        lines.append(f"Thanks for your note about \u201c{recent[0]}\u201d.")
    else:
        lines.append("I wanted to reach out and stay in touch.")
    lines.append("")
    lines.append("Best,")
    draft_body = "\n".join(lines)

    saved = None
    if save:
        draft = EmailDraft(to=[address], subject=purpose or f"Hello from {display.split()[0] if display.split() else 'me'}",
                           body=draft_body)
        result = get_email_provider(get_settings()).save_draft(draft)
        saved = bool(result.get("ok", True))

    return {
        "contact": {"name": display, "address": address},
        "draft": {"to": address, "body": draft_body,
                  "subject": purpose or f"Hello from {display.split()[0] if display.split() else 'me'}"},
        "context": {"open_tasks": open_tasks, "recent_email": recent},
        "saved": saved,
        "sending": {"supported": False,
                    "reason": "Vantage saves this as a draft. Sending is a separate, "
                              "confirmed action."},
        "email_error": mail_error,
    }


@app.post("/meetings/{uid}/follow-up")
async def meeting_follow_up(uid: str, body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    """Record what happened in a meeting and turn agreed actions into work.

    The notes are the person's own account — Vantage did not attend and has no
    audio — and they are **stored**, so they can be read back later instead of
    disappearing with the page. Only the action lines the person wrote become
    planner tasks, prefixed with the meeting so the work traces back to it.
    """
    import json as _json
    import re

    from ..core.models import new_id

    body = body or {}
    notes = str(body.get("notes") or "").strip()
    raw_items = body.get("action_items") or []
    if isinstance(raw_items, str):
        raw_items = [line for line in raw_items.splitlines()]

    items = [str(i).strip() for i in raw_items if str(i).strip()]
    if not items and notes:
        items = [line.strip(" -•*\t") for line in notes.splitlines()
                 if re.match(r"^\s*[-•*]\s+\S", line)]

    meeting = None
    try:
        from ..integrations.calendar_provider import LocalCalendarProvider

        meeting = LocalCalendarProvider().get(uid)
    except Exception:
        meeting = None
    title = (meeting.title if meeting else "") or uid

    created = []
    planner = get_core().planner_service
    for item in items:
        task = planner.create_task(f"[{title}] {item}"[:200],
                                   detail=f"From the {title} meeting" if meeting else "",
                                   source="meeting")
        created.append({"task_ref": task.task_ref, "title": task.title})

    note_id = None
    if notes or items:
        from ..db.database import get_db

        note_id = new_id("MNOTE")
        get_db().execute(
            "INSERT INTO meeting_notes (note_id, event_uid, meeting, notes, action_items) "
            "VALUES (?, ?, ?, ?, ?)",
            (note_id, uid, title, notes, _json.dumps(items)))

    return {
        "ok": True,
        "note_id": note_id,
        "meeting": uid,
        "meeting_title": title,
        "created": created,
        "created_count": len(created),
        "notes_saved": bool(notes),
        "note": ("Saved, and the action lines you wrote became tasks."
                 if items else
                 "Saved. No action lines were written, so no tasks were created."),
    }


@app.get("/meetings/{uid}/notes")
async def meeting_notes(uid: str, _: None = Protected) -> Dict[str, Any]:
    """Everything recorded for a meeting, newest first."""
    import json as _json

    from ..db.database import get_db

    rows = get_db().query(
        "SELECT * FROM meeting_notes WHERE event_uid = ? ORDER BY created_at DESC", (uid,))
    notes = []
    for row in rows:
        try:
            items = _json.loads(row["action_items"] or "[]")
        except ValueError:
            items = []
        notes.append({"note_id": row["note_id"], "meeting": row["meeting"],
                      "notes": row["notes"], "action_items": items,
                      "created_at": row["created_at"]})
    return {"meeting": uid, "count": len(notes), "notes": notes}


def _join_url(text: str) -> str:
    """The first conferencing link in an event's location or description, if any."""
    import re as _re

    match = _re.search(r"https?://[^\s<>\"']+", text or "")
    return match.group(0) if match else ""


def _meeting_conflict(events: List[Any], meeting: Any) -> Optional[Dict[str, Any]]:
    """An event that overlaps the meeting, so back-to-back or doubled bookings surface."""
    from ..integrations.calendar_provider import events_overlap

    start, end = _as_local(meeting.start), _as_local(meeting.end)
    for other in events:
        if other.uid == meeting.uid:
            continue
        if events_overlap(start, end, _as_local(other.start), _as_local(other.end)):
            return other.as_dict()
    return None


@app.get("/attention")
async def attention_endpoint(_: None = Protected) -> Dict[str, Any]:
    """Everything that needs a person right now, gathered from real state.

    Four honest sources, each read from the thing that owns it:

    * scheduled automations whose **last run failed** (the scheduler's own
      ``schedule_runs`` ledger, not a guess);
    * workflow jobs that ended ``failed`` or are stuck ``paused``;
    * planner tasks that are **overdue**;
    * post drafts whose scheduled date has passed (a reminder — Vantage does not
      publish).

    Nothing is invented to fill the list: an empty list means none of these are
    true, and each item names the surface where it can be resolved.
    """
    from datetime import datetime as _dt
    from ..core.post_service import PostService
    from ..scheduler.store import ScheduleStore

    items: List[Dict[str, Any]] = []

    store = ScheduleStore()
    for job in store.list():
        if (job.failure_count or 0) > 0 and job.last_error:
            items.append({
                "kind": "schedule_failure",
                "surface": "planner",
                "title": f"{job.name} failed",
                "detail": job.last_error[:200],
                "ref": job.schedule_id,
            })

    tasks = get_core().tasks
    # The docstring above has promised "stuck `paused`" since this endpoint was
    # written, but the code only ever asked for `failed` -- so a job waiting on a
    # person, or a plan that stopped half-way, never reached the list that exists
    # to tell a person what needs them.
    for job in tasks.failed_jobs(limit=20):
        items.append({
            "kind": "workflow_failed",
            "surface": "work",
            "title": f"Workflow failed: {job.get('goal') or job.get('job_id')}",
            "detail": job.get("status") or "failed",
            "ref": job.get("job_id"),
        })
    for status, kind in (("paused", "workflow_paused"),
                         ("awaiting_confirmation", "workflow_awaiting_approval")):
        for job in tasks.jobs_with_status(status, limit=10):
            items.append({
                "kind": kind,
                "surface": "work",
                "title": (f"Workflow waiting on you: {job.get('goal') or job.get('job_id')}"
                          if status == "awaiting_confirmation"
                          else f"Workflow paused: {job.get('goal') or job.get('job_id')}"),
                "detail": f"status {status}",
                "ref": job.get("job_id"),
            })

    planner = get_core().planner_service
    now = _dt.now().astimezone()
    open_refs = {t.task_ref for t in planner.list_tasks()}
    for task in planner.list_tasks():
        d = task.as_dict(now=now, open_refs=open_refs)
        if d["overdue"]:
            items.append({
                "kind": "task_overdue",
                "surface": "planner",
                "title": f"Overdue: {task.title}",
                "detail": f"due {task.due_at.isoformat()}" if task.due_at else "",
                "ref": task.task_ref,
            })

    for draft in PostService().due_reminders(now):
        items.append({
            "kind": "post_reminder",
            "surface": "operator",
            "title": "Post scheduled for now",
            "detail": (draft.body or "")[:120],
            "ref": draft.draft_id,
        })

    return {"count": len(items), "items": items}


@app.get("/posts")
async def list_posts(_: None = Protected, channel: str = Query("")) -> Dict[str, Any]:
    from ..core.post_service import PostService

    service = PostService()
    return {
        "publishing": service.publishing_supported(),
        "drafts": [d.as_dict() for d in service.list(channel=channel)],
        "due": [d.as_dict() for d in service.due_reminders()],
    }


@app.post("/posts/recap")
async def create_recap_post(body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    """Draft a recap post from work that was actually completed.

    Only tasks the planner records as done inside the window are used. With
    nothing completed it creates no draft and says why, rather than inventing
    progress. The result is a *draft*: publishing has no approved integration and
    the reply says so.
    """
    from ..core.post_service import PostService

    body = body or {}
    raw_days = body.get("days")
    if raw_days is None:
        raw_days = 7
    try:
        days = int(raw_days)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="days must be a number")
    if not 1 <= days <= 90:
        raise HTTPException(status_code=422, detail="days must be between 1 and 90")
    return PostService().draft_from_completed_work(
        days=days, planner=get_core().planner_service)


@app.post("/posts")
async def create_post(body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    from datetime import datetime as _dt
    from ..core.post_service import PostService

    body = body or {}
    text = str(body.get("body") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="a post needs text")
    when = None
    if body.get("scheduled_for"):
        try:
            when = _dt.fromisoformat(str(body["scheduled_for"]))
        except ValueError:
            raise HTTPException(status_code=422, detail="scheduled_for must be ISO-8601")
    service = PostService()
    draft = service.create(text, channel=str(body.get("channel") or "linkedin"),
                           scheduled_for=when, contact=str(body.get("contact") or ""))
    return draft.as_dict()


@app.patch("/posts/{draft_id}")
async def edit_post(draft_id: str, body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    from datetime import datetime as _dt
    from ..core.post_service import PostService

    body = body or {}
    when = None
    if body.get("scheduled_for"):
        try:
            when = _dt.fromisoformat(str(body["scheduled_for"]))
        except ValueError:
            raise HTTPException(status_code=422, detail="scheduled_for must be ISO-8601")
        # A browser `datetime-local` sends "2026-10-25T09:00" with no offset. Stored
        # as-is, `due_reminders` would compare it to local time and fire the
        # reminder off by the UTC offset. The person picked a local wall-clock
        # time, so that is what it is taken to mean.
        if when.tzinfo is None:
            when = when.astimezone()
    draft = PostService().update(
        draft_id, body=body.get("body"), scheduled_for=when,
        clear_schedule=bool(body.get("clear_schedule")),
        contact=body.get("contact"))
    if draft is None:
        raise HTTPException(status_code=404, detail=f"no draft {draft_id!r}")
    return draft.as_dict()


@app.delete("/posts/{draft_id}")
async def delete_post(draft_id: str, _: None = Protected) -> Dict[str, Any]:
    from ..core.post_service import PostService

    if not PostService().delete(draft_id):
        raise HTTPException(status_code=404, detail=f"no draft {draft_id!r}")
    return {"draft_id": draft_id, "deleted": True}


@app.post("/posts/{draft_id}/publish")
async def publish_post(draft_id: str, _: None = Protected) -> Dict[str, Any]:
    """Publishing is deliberately not implemented — said plainly, not faked."""
    from ..core.post_service import PostService

    if PostService().get(draft_id) is None:
        raise HTTPException(status_code=404, detail=f"no draft {draft_id!r}")
    raise HTTPException(status_code=501, detail=PostService.publishing_supported()["reason"])


@app.get("/contacts")
async def list_contacts(_: None = Protected) -> List[Dict[str, Any]]:
    rows = get_db().query("SELECT contact_id, name, address FROM contacts ORDER BY name")
    return [dict(r) for r in rows]


@app.post("/contacts/{name}/prepare-call")
async def prepare_call(name: str, _: None = Protected) -> Dict[str, Any]:
    """Prepare a call: who it is, what is outstanding, and what to raise.

    This is *preparation* — it reads real data (the contact, open planner tasks
    that name them, recent email from them) and returns a briefing. It does not
    place a call and does not claim one happened: the actual call is a separate,
    user-authorised action.
    """
    from ..tools.email import resolve_recipient

    resolution = resolve_recipient(name)
    if not resolution.get("ok"):
        raise HTTPException(status_code=404, detail=resolution.get("reason", "no such contact"))
    address = str(resolution.get("address") or "")
    display = str(resolution.get("name") or name)

    brief: Dict[str, Any] = {
        "contact": {"name": display, "address": address},
        "open_tasks": [],
        "recent_email": [],
        "talking_points": [],
        # Calling became real when the FaceTime path landed (POST
        # /contacts/{name}/call, confirmation-gated). This response kept saying it
        # was impossible for several ticks afterwards, and the operator page
        # repeated the claim verbatim -- a limitation nobody had any more.
        "place_call": {
            "supported": _calling_available(),
            "reason": ("Vantage can hand this contact to FaceTime; it asks first and "
                       "cannot see or hear the call."
                       if _calling_available() else
                       "FaceTime is not installed, so there is no app to place the call."),
        },
    }

    # Real tasks that mention the person.
    try:
        for task in get_core().planner_service.list_tasks():
            hay = f"{task.title} {task.detail}".lower()
            if display.lower() in hay or address.lower() in hay:
                brief["open_tasks"].append(
                    {"task_ref": task.task_ref, "title": task.title,
                     "due_at": task.due_at.isoformat() if task.due_at else None})
    except Exception as exc:  # a planner read must not fail the briefing
        brief["tasks_error"] = str(exc)[:120]

    # Recent mail from them (a read; an unreadable mailbox is reported, not faked).
    try:
        from ..integrations.email_provider import get_email_provider, read_status

        results = get_email_provider(get_settings()).search(query=display, limit=5)
        if read_status(results).read_succeeded:
            for m in results:
                if address.lower() in (m.sender or "").lower():
                    brief["recent_email"].append(
                        {"subject": m.subject, "from": m.sender, "date": m.date,
                         "snippet": m.snippet})
        else:
            brief["email_error"] = "mailbox could not be read"
    except Exception as exc:
        brief["email_error"] = str(exc)[:120]

    if brief["open_tasks"]:
        brief["talking_points"].append(
            f"{len(brief['open_tasks'])} open task(s) involve {display}.")
    if brief["recent_email"]:
        brief["talking_points"].append(
            f"{len(brief['recent_email'])} recent message(s) from {display}.")
    if not brief["talking_points"]:
        brief["talking_points"].append(f"No open tasks or recent mail involving {display}.")

    return brief


@app.get("/calendar/events")
async def calendar_events(
    date: str = Query("today"),
    days: int = Query(1),
    _: None = Protected,
) -> Dict[str, Any]:
    from ..tools.calendar import _resolve_range

    provider = get_calendar_provider()
    start, end, problem = _resolve_range(date, days)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    events = provider.list_events(start, end, limit=200)
    return {"provider": provider.name, "count": len(events), "events": [e.as_dict() for e in events]}


@app.post("/calendar/events")
async def create_calendar_event(body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    """Create an event from the planner page.

    Creation is LOW and reversible (the event can be cancelled), so it does not
    need the confirmation gate that cancelling does — but it does need a real
    title and a time it can understand. A time it cannot parse is a 422 naming
    what it could not read, never a guessed slot: booking someone into the wrong
    hour is a worse outcome than being asked.
    """
    from ..integrations.calendar_provider import parse_datetime
    from ..tools.base import ToolContext
    from ..tools.calendar import create_event

    body = body or {}
    title = str(body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=422, detail="an event needs a title")
    when = str(body.get("start") or "").strip()
    if not when:
        raise HTTPException(status_code=422, detail="an event needs a start time")
    if parse_datetime(when) is None:
        raise HTTPException(status_code=422,
                            detail=f"I could not understand the start time {when!r}.")

    result = await create_event(
        ToolContext(), title=title, start=when,
        duration_minutes=int(body.get("duration_minutes") or 60),
        location=str(body.get("location") or ""))
    ok_flag = bool(getattr(result, "ok", False))
    output = getattr(result, "output", {}) or {}
    return {
        "ok": ok_flag,
        "event": output.get("event"),
        "message": (getattr(result, "message", "") or getattr(result, "error", "")
                    or ("Created." if ok_flag else "The event was not created.")),
        "verified": bool(getattr(result, "verified", False)),
    }


@app.post("/calendar/events/{uid}/cancel")
async def cancel_calendar_event(uid: str, body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    """Cancel an event. Irreversible, so it needs `confirm: true`.

    The calendar was readable and unchangeable from every surface -- the only way
    to cancel an event was a chat sentence, while the planner page listed the same
    events with no action beside them. Cancelling is gated for the same reason
    sending is: the person must mean it.
    """
    from ..tools.base import ToolContext
    from ..tools.calendar import cancel_event

    body = body or {}
    if not body.get("confirm"):
        raise HTTPException(
            status_code=409,
            detail="Cancelling an event needs explicit confirmation (confirm: true).")

    result = await cancel_event(ToolContext(), uid=uid)
    ok_flag = bool(getattr(result, "ok", False))
    return {
        "ok": ok_flag,
        "uid": uid,
        "message": (getattr(result, "message", "") or getattr(result, "error", "")
                    or ("Cancelled." if ok_flag else "The event was not cancelled.")),
        "verified": bool(getattr(result, "verified", False)),
    }


@app.get("/calendar/next")
async def calendar_next(_: None = Protected) -> Dict[str, Any]:
    from datetime import timedelta

    provider = get_calendar_provider()
    now = utcnow()
    events = provider.list_events(now, now + timedelta(days=30), limit=50)
    upcoming = [e for e in events if e.end > now]
    return {"event": upcoming[0].as_dict() if upcoming else None}


# ---------------------------------------------------------------------------
# Scheduler + briefing (v0.8)
# ---------------------------------------------------------------------------
class ScheduleBody(BaseModel):
    name: str = ""
    schedule: str
    action: str = "reminder"
    params: Dict[str, Any] = {}


@app.get("/schedules/actions")
async def schedulable_actions(_: None = Protected) -> Dict[str, Any]:
    """The actions a schedule may run, and the ceiling that decides it.

    The planner page offered only "briefing" and "reminder" in its datalist, so
    the schedulable tools the scheduler already supports were undiscoverable —
    a person could not find out that "every morning check the battery" is a thing
    they are allowed to ask for. This answers the question from the registry.
    """
    from ..scheduler.runner import BUILTIN_ACTIONS
    from ..scheduler.store import MAX_SCHEDULABLE_RISK

    core = get_core()
    actions = [{"action": name, "risk": "builtin", "description": ""}
               for name in sorted(BUILTIN_ACTIONS)]
    for name in sorted(core.tools.names()):
        spec = core.tools.get(name)
        if spec is None or int(spec.risk) > int(MAX_SCHEDULABLE_RISK):
            continue
        actions.append({
            "action": name,
            "risk": spec.risk_label(),
            "description": spec.description,
            "requires_confirmation": spec.requires_confirmation,
        })
    needs_approval = [a["action"] for a in actions if a.get("requires_confirmation")]
    unattended = [a["action"] for a in actions if not a.get("requires_confirmation")]
    return {
        "actions": actions,
        "count": len(actions),
        "unattended_count": len(unattended),
        "needs_approval_count": len(needs_approval),
        "needs_approval": needs_approval,
        "ceiling": RiskLevel(MAX_SCHEDULABLE_RISK).name.lower(),
        "note": (f"{len(unattended)} of these {len(actions)} actions run unattended. "
                 f"The other {len(needs_approval)} — including "
                 f"{', '.join(needs_approval[:3])} — can be scheduled, but every run "
                 "stops and waits for your approval rather than acting on its own. "
                 "That is the scheduler refusing to approve on your behalf, not a "
                 "limitation."),
    }


@app.get("/schedules")
async def schedules(include_cancelled: bool = Query(False), _: None = Protected) -> Dict[str, Any]:
    store = ScheduleStore()
    return {
        "stats": store.stats(),
        "schedules": [j.as_dict() for j in store.list(include_cancelled=include_cancelled)],
    }


@app.post("/schedules")
async def create_schedule(body: ScheduleBody, _: None = Protected) -> Dict[str, Any]:
    """Create a scheduled job.

    The job stores the risk ceiling of its action. It is re-evaluated through
    the permission engine on every run and can never exceed that ceiling.
    """
    from ..scheduler.store import MAX_SCHEDULABLE_RISK
    from ..scheduler.runner import BUILTIN_ACTIONS

    max_risk = int(RiskLevel.READ)
    if body.action not in BUILTIN_ACTIONS:
        spec = get_core().tools.get(body.action)
        if spec is None:
            raise HTTPException(status_code=400, detail=f"unknown action {body.action!r}")
        if int(spec.risk) > int(MAX_SCHEDULABLE_RISK):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{body.action} is a {spec.risk_label()} action, which is too "
                    "risky to run automatically."
                ),
            )
        max_risk = int(spec.risk)

    try:
        job = ScheduleStore().create(
            name=body.name or body.action,
            schedule_text=body.schedule,
            action=body.action,
            params=body.params,
            max_risk=max_risk,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    get_core().audit.record(
        intent="schedule.create",
        tool="schedule_task",
        target=job.schedule_id,
        risk="external",
        result="success",
        detail={"action": body.action, "schedule": body.schedule},
    )
    return job.as_dict()


@app.patch("/schedules/{schedule_id}")
async def edit_schedule(schedule_id: str, body: dict = Body(None),
                        _: None = Protected) -> Dict[str, Any]:
    """Edit a job's name, schedule, action or params — keeping its identity.

    Inserted **before** the `{schedule_id}` GET so FastAPI's route matching does
    not treat "edit" as an id. The action is re-checked against the same risk
    ceiling as creation, so an edit cannot smuggle in a riskier tool.
    """
    from ..scheduler.store import MAX_SCHEDULABLE_RISK, ScheduleStore
    from ..scheduler.runner import BUILTIN_ACTIONS

    store = ScheduleStore()
    if store.get(schedule_id) is None:
        raise HTTPException(status_code=404, detail=f"no schedule {schedule_id!r}")

    body = body or {}
    changes: Dict[str, Any] = {}
    if "name" in body:
        changes["name"] = str(body["name"])
    if "schedule" in body:
        changes["schedule_text"] = str(body["schedule"])
    if "params" in body:
        changes["params"] = body["params"] or {}
    if "action" in body:
        action = str(body["action"])
        if action not in BUILTIN_ACTIONS:
            spec = get_core().tools.get(action)
            if spec is None:
                raise HTTPException(status_code=400, detail=f"unknown action {action!r}")
            if int(spec.risk) > int(MAX_SCHEDULABLE_RISK):
                raise HTTPException(
                    status_code=400,
                    detail=(f"{action} is a {spec.risk_label()} action, which is too "
                            "risky to run automatically."))
            changes["max_risk"] = int(spec.risk)
        changes["action"] = action

    try:
        job = store.update(schedule_id, **changes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    get_core().audit.record(
        intent="schedule.edit", tool="schedule_task", target=schedule_id,
        risk="external", result="success", detail=changes)
    return job.as_dict()


@app.get("/schedules/{schedule_id}")
async def schedule_detail(schedule_id: str, _: None = Protected) -> Dict[str, Any]:
    store = ScheduleStore()
    job = store.get(schedule_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no schedule {schedule_id!r}")
    return {"schedule": job.as_dict(), "history": store.history(schedule_id)}


@app.post("/schedules/{schedule_id}/{action}")
async def schedule_lifecycle(
    schedule_id: str, action: str, _: None = Protected
) -> Dict[str, Any]:
    """Pause, resume, cancel or run a scheduled job."""
    store = ScheduleStore()
    job = store.get(schedule_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no schedule {schedule_id!r}")

    if action == "run":
        outcome = await get_core().scheduler.run_job(get_core(), job)
        return {"schedule_id": schedule_id, **outcome}

    handlers = {"pause": store.pause, "resume": store.resume, "cancel": store.cancel}
    handler = handlers.get(action)
    if handler is None:
        raise HTTPException(
            status_code=400,
            detail=f"unknown action {action!r}; expected pause, resume, cancel or run",
        )
    handler(schedule_id)
    return {"schedule_id": schedule_id, "status": store.get(schedule_id).status}


@app.get("/briefing")
async def briefing(sections: str = Query(""), _: None = Protected) -> Dict[str, Any]:
    """Compose the daily briefing."""
    from ..core.briefing import build_briefing

    requested = [s.strip() for s in sections.split(",") if s.strip()] or None
    return await build_briefing(get_core(), sections=requested)


# ---------------------------------------------------------------------------
# Advanced memory (v0.9)
# ---------------------------------------------------------------------------
@app.get("/memory/overview")
async def memory_overview_endpoint(_: None = Protected) -> Dict[str, Any]:
    from ..memory.store import MemoryStore
    from ..memory.vector_store import VectorIndex

    db = get_db()
    by_scope = {
        r["scope"]: r["n"]
        for r in db.query("SELECT scope, COUNT(*) AS n FROM memories GROUP BY scope")
    }
    by_type = {
        r["memory_type"]: r["n"]
        for r in db.query(
            "SELECT COALESCE(memory_type, 'general') AS memory_type, COUNT(*) AS n "
            "FROM memories GROUP BY memory_type"
        )
    }
    return {
        "total": sum(by_scope.values()),
        "by_scope": by_scope,
        "by_type": by_type,
        "types_available": MemoryType.values(),
        "vector_index": VectorIndex().stats(),
    }


@app.get("/memory/search")
async def memory_search(
    q: str = Query(...), limit: int = Query(5), _: None = Protected
) -> Dict[str, Any]:
    """Hybrid (keyword + vector) memory search."""
    from ..memory.hybrid import hybrid_search

    outcome = hybrid_search(q, limit=max(1, min(limit, 50)))
    return {
        "query": q,
        "retrieval": outcome["retrieval"],
        "results": [r.as_dict() for r in outcome["results"]],
    }


@app.get("/memory/export")
async def memory_export(
    scope: str = Query(""), include_sensitive: bool = Query(False), _: None = Protected
) -> Dict[str, Any]:
    """Export memories. Sensitive items are withheld unless asked for."""
    from ..memory.store import MemoryStore

    try:
        chosen = MemoryScope(scope) if scope else None
    except ValueError:
        raise HTTPException(status_code=400, detail=f"unknown scope {scope!r}")

    withheld = 0
    payload = []
    for item in MemoryStore().all(scope=chosen, limit=5000):
        if item.privacy_level.value in ("private", "secret") and not include_sensitive:
            withheld += 1
            continue
        payload.append(
            {
                "id": item.id,
                "content": item.content,
                "type": item.memory_type.value,
                "scope": item.scope.value,
                "importance": item.importance,
                "confidence": item.confidence,
                "privacy": item.privacy_level.value,
                "created_at": item.created_at.isoformat(),
                "expires_at": item.expires_at.isoformat() if item.expires_at else None,
            }
        )
    return {"count": len(payload), "withheld": withheld, "memories": payload}


@app.get("/memory/{memory_id}")
async def memory_detail(memory_id: str, _: None = Protected) -> Dict[str, Any]:
    from ..memory.store import MemoryStore

    item = MemoryStore().get(memory_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"no memory {memory_id!r}")
    return {
        "id": item.id,
        "content": item.content,
        "type": item.memory_type.value,
        "scope": item.scope.value,
        "importance": item.importance,
        "privacy": item.privacy_level.value,
        "created_at": item.created_at.isoformat(),
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        "expired": item.is_expired(),
        "tags": item.tags,
        "relationships": item.relationships,
    }


@app.post("/memory/{memory_id}/forget")
async def memory_forget(memory_id: str, _: None = Protected) -> Dict[str, Any]:
    """Delete one memory, answered by the same code the agent uses.

    This route used to call ``store.forget`` itself and return a hardcoded
    ``{'deleted': True}``, while the ``forget_memory`` tool beside it reports the
    copies a single deletion did *not* remove. One deletion, two surfaces, two
    different claims about the world -- so the HTTP path claimed completeness the
    tool refused to claim. The report is no longer duplicated here; it is delegated.
    """
    from ..tools.base import ToolContext
    from ..tools.memory_controls import forget_memory

    result = await forget_memory(ToolContext(), memory_id=memory_id)
    output = result.output or {}
    complete = bool(output.get("complete"))

    get_core().audit.record(
        intent="memory.forget",
        tool="forget_memory",
        target=memory_id,
        risk="high",
        confirmation=True,
        # A deletion that demonstrably left the same text in other rows is not a
        # plain success; the ledger carries the same distinction the reply does.
        result="success" if (result.ok and complete) else ("unverified" if result.ok else "failure"),
    )
    if not result.ok:
        raise HTTPException(status_code=404, detail=result.summary or f"no memory {memory_id!r}")

    return {
        "memory_id": memory_id,
        "deleted": True,
        "complete": complete,
        "report": result.message,
        "remaining_copies": output.get("remaining_copies", []),
        "retained": output.get("retained", {}),
    }


@app.post("/memory/clear")
async def memory_clear(scope: str = Query(...), _: None = Protected) -> Dict[str, Any]:
    """Delete every memory in a scope. Destructive, so it is audited."""
    try:
        chosen = MemoryScope(scope)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"unknown scope {scope!r}")

    db = get_db()
    rows = db.query("SELECT COUNT(*) AS n FROM memories WHERE scope = ?", (chosen.value,))
    removed = rows[0]["n"] if rows else 0
    db.execute("DELETE FROM memories WHERE scope = ?", (chosen.value,))

    get_core().audit.record(
        intent="memory.clear_scope",
        tool="clear_memory_scope",
        target=chosen.value,
        risk="high",
        confirmation=True,
        result="success",
        detail={"removed": removed},
    )
    return {"scope": chosen.value, "removed": removed}


@app.post("/memory/reindex")
async def memory_reindex(_: None = Protected) -> Dict[str, Any]:
    """Recompute every memory vector (used after changing the embedder)."""
    from ..memory.vector_store import VectorIndex

    index = VectorIndex()
    rebuilt = index.rebuild()
    return {"reindexed": rebuilt, "index": index.stats()}


# ---------------------------------------------------------------------------
# Research Agent (v1.0)
# ---------------------------------------------------------------------------
class PaperBody(BaseModel):
    title: str
    authors: str = ""
    year: int = 0
    url: str = ""
    project_id: str = ""


class PaperSearchBody(BaseModel):
    query: str
    source: str = "arxiv"
    limit: int = 5
    project_id: str = ""


class NoteBody(BaseModel):
    claim: str
    claim_type: str = "interpretation"
    topic: str = ""
    paper_id: str = ""
    evidence: str = ""
    project_id: str = ""


@app.get("/research/sources")
async def research_sources(_: None = Protected) -> Dict[str, Any]:
    """Which sources Vantage may search."""
    from ..research.sources import describe_sources

    return describe_sources()


@app.get("/papers")
async def papers(project_id: str = Query(""), _: None = Protected) -> Dict[str, Any]:
    from ..research.papers import PaperStore

    store = PaperStore()
    return {
        "stats": store.stats(),
        "papers": [p.as_dict() for p in store.all(project_id=project_id or None)],
    }


@app.get("/papers/{paper_id}")
async def paper_detail(paper_id: str, _: None = Protected) -> Dict[str, Any]:
    from ..research.papers import PaperStore

    paper = PaperStore().get(paper_id)
    if paper is None:
        raise HTTPException(status_code=404, detail=f"no paper {paper_id!r}")
    return paper.as_dict()


@app.get("/papers/{paper_id}/cite")
async def paper_cite(
    paper_id: str, style: str = Query("text"), _: None = Protected
) -> Dict[str, Any]:
    """Cite a stored paper.

    Returns 404 when the paper is not stored — it never invents a citation.
    """
    from ..research.papers import PaperStore

    citation = PaperStore().cite(paper_id, style=style)
    if citation is None:
        raise HTTPException(
            status_code=404,
            detail=f"no stored paper {paper_id!r}; I will not invent a citation for it",
        )
    return {"paper_id": paper_id, "style": style, "citation": citation}


@app.post("/papers/search")
async def papers_search(body: PaperSearchBody, _: None = Protected) -> Dict[str, Any]:
    """Search an approved source and store the references found."""
    from ..research.papers import PaperStore
    from ..research.sources import get_source

    backend = get_source(body.source)
    if backend is None:
        raise HTTPException(status_code=400, detail=f"unapproved source {body.source!r}")

    try:
        hits = backend.search(body.query, limit=max(1, min(body.limit, 25)))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    stored = PaperStore().add_many(hits, project_id=body.project_id or None)
    return {"source": body.source, "stored": len(stored), "papers": [p.as_dict() for p in stored]}


@app.post("/papers")
async def papers_add(body: PaperBody, _: None = Protected) -> Dict[str, Any]:
    """Add a reference explicitly. Stored as unverified."""
    from ..research.papers import PaperStore

    authors = [a.strip() for a in body.authors.split(",") if a.strip()]
    paper = PaperStore().add(
        body.title,
        authors=authors,
        year=body.year or None,
        url=body.url,
        source_backend="user",
        project_id=body.project_id or None,
    )
    return paper.as_dict()


@app.post("/papers/{paper_id}/forget")
async def papers_forget(paper_id: str, _: None = Protected) -> Dict[str, Any]:
    from ..research.papers import PaperStore

    if not PaperStore().forget(paper_id):
        raise HTTPException(status_code=404, detail=f"no paper {paper_id!r}")

    get_core().audit.record(
        intent="research.forget_paper",
        tool="forget_paper",
        target=paper_id,
        risk="high",
        confirmation=True,
        result="success",
    )
    return {"paper_id": paper_id, "deleted": True}


@app.get("/research/notes")
async def research_notes(
    project_id: str = Query(""),
    claim_type: str = Query(""),
    _: None = Protected,
) -> Dict[str, Any]:
    from ..research.notes import ResearchNoteStore

    store = ResearchNoteStore()
    notes = store.list(
        project_id=project_id or None, claim_type=claim_type or None
    )
    return {"stats": store.stats(project_id=project_id or None), "notes": [n.as_dict() for n in notes]}


@app.post("/research/notes")
async def research_notes_add(body: NoteBody, _: None = Protected) -> Dict[str, Any]:
    """Record a note with an explicit claim type."""
    from ..research.notes import CLAIM_TYPES, ResearchNoteStore

    if body.claim_type not in CLAIM_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown claim type {body.claim_type!r}; expected one of {CLAIM_TYPES}",
        )

    try:
        note = ResearchNoteStore().add(
            body.claim,
            claim_type=body.claim_type,
            topic=body.topic,
            paper_id=body.paper_id or None,
            evidence=body.evidence,
            project_id=body.project_id or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return note.as_dict()


@app.get("/research/gaps")
async def research_gaps(project_id: str = Query(""), _: None = Protected) -> Dict[str, Any]:
    """Recorded gaps and stated limitations. Never invents a gap."""
    from ..research.brief import identify_gaps
    from ..research.notes import ResearchNoteStore
    from ..research.papers import PaperStore

    return identify_gaps(
        PaperStore().all(project_id=project_id or None),
        ResearchNoteStore().list(project_id=project_id or None),
    )


@app.get("/research/brief")
async def research_brief(
    topic: str = Query(...), project_id: str = Query(""), _: None = Protected
) -> Dict[str, Any]:
    """Compose a research brief from stored papers and notes."""
    from ..research.brief import build_brief
    from ..research.notes import ResearchNoteStore
    from ..research.papers import PaperStore

    store = PaperStore()
    papers = store.search(topic)
    if project_id:
        papers = [p for p in papers if p.project_id == project_id] or papers
    notes = ResearchNoteStore().list(project_id=project_id or None)

    return await build_brief(topic, papers=papers, notes=notes, store=store)


# ---------------------------------------------------------------------------
# Coding Agent (v1.1)
# ---------------------------------------------------------------------------
class WorkspaceBody(BaseModel):
    path: str
    name: str = ""


class ChangeBody(BaseModel):
    #: NB: no ``workspace_id`` — it comes from the path. Requiring it in the body
    #: as well made every request fail validation.
    path: str
    old: str
    new: str


class CommitBody(BaseModel):
    message: str
    #: Must be true. Committing is never implicit.
    authorized: bool = False


@app.get("/workspaces")
async def workspaces(_: None = Protected) -> Dict[str, Any]:
    """Open workspaces."""
    from ..coding.workspace import get_workspace_registry

    return {
        "workspaces": [
            {"workspace_id": w.workspace_id, "name": w.display, "root": str(w.root)}
            for w in get_workspace_registry().all()
        ]
    }


@app.post("/workspaces")
async def workspaces_open(body: WorkspaceBody, _: None = Protected) -> Dict[str, Any]:
    """Open a directory as a controlled workspace."""
    from ..coding.workspace import get_workspace_registry

    workspace = get_workspace_registry().open(body.path, name=body.name)
    if workspace is None:
        raise HTTPException(
            status_code=400,
            detail=f"could not open {body.path!r} as a workspace (not a directory, or protected)",
        )
    return {"workspace_id": workspace.workspace_id, "name": workspace.display, "root": str(workspace.root)}


@app.post("/workspaces/{workspace_id}/close")
async def workspaces_close(workspace_id: str, _: None = Protected) -> Dict[str, Any]:
    from ..coding.workspace import get_workspace_registry

    if not get_workspace_registry().close(workspace_id):
        raise HTTPException(status_code=404, detail=f"no open workspace {workspace_id!r}")
    return {"workspace_id": workspace_id, "closed": True}


@app.get("/workspaces/{workspace_id}/inspect")
async def workspaces_inspect(workspace_id: str, _: None = Protected) -> Dict[str, Any]:
    from ..coding.repo import describe, inspect
    from ..coding.workspace import get_workspace_registry

    workspace = get_workspace_registry().resolve(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail=f"no open workspace {workspace_id!r}")
    return {"summary": describe(workspace), **inspect(workspace)}


@app.get("/workspaces/{workspace_id}/search")
async def workspaces_search(
    workspace_id: str, q: str = Query(...), limit: int = Query(20), _: None = Protected
) -> Dict[str, Any]:
    from ..coding.repo import search
    from ..coding.workspace import get_workspace_registry

    workspace = get_workspace_registry().resolve(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail=f"no open workspace {workspace_id!r}")
    return {"query": q, "results": search(workspace, q, limit=max(1, min(limit, 100)))}


@app.post("/workspaces/{workspace_id}/propose")
async def workspaces_propose(
    workspace_id: str, body: ChangeBody, _: None = Protected
) -> Dict[str, Any]:
    """Show a diff without writing anything."""
    from ..coding.changes import propose
    from ..coding.workspace import get_workspace_registry

    workspace = get_workspace_registry().resolve(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail=f"no open workspace {workspace_id!r}")

    change = propose(workspace, body.path, body.old, body.new)
    if not change.applies:
        raise HTTPException(status_code=400, detail=change.reason)
    return change.as_dict()


@app.post("/workspaces/{workspace_id}/apply")
async def workspaces_apply(
    workspace_id: str, body: ChangeBody, _: None = Protected
) -> Dict[str, Any]:
    """Apply a change with a backup. Audited, since it modifies files."""
    from ..coding.changes import apply, propose
    from ..coding.workspace import get_workspace_registry

    workspace = get_workspace_registry().resolve(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail=f"no open workspace {workspace_id!r}")

    change = propose(workspace, body.path, body.old, body.new)
    if not change.applies:
        raise HTTPException(status_code=400, detail=change.reason)

    outcome = apply(workspace, change)
    if not outcome.get("ok"):
        raise HTTPException(status_code=400, detail=outcome.get("error", "could not apply"))

    get_core().audit.record(
        intent="coding.apply_change",
        tool="apply_code_change",
        target=body.path,
        risk="external",
        result="success",
        detail={"workspace": workspace.display, "backup": outcome.get("backup")},
    )
    return outcome


@app.get("/workspaces/{workspace_id}/report")
async def workspaces_report(workspace_id: str, _: None = Protected) -> Dict[str, Any]:
    from ..coding.report import build_report
    from ..coding.workspace import get_workspace_registry

    workspace = get_workspace_registry().resolve(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail=f"no open workspace {workspace_id!r}")
    return build_report(workspace)


@app.post("/workspaces/{workspace_id}/commit")
async def workspaces_commit(
    workspace_id: str, body: CommitBody, _: None = Protected
) -> Dict[str, Any]:
    """Commit when explicitly authorised. Push is not implemented."""
    from ..coding.workspace import get_workspace_registry
    from ..tools.coding import commit_workspace

    workspace = get_workspace_registry().resolve(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail=f"no open workspace {workspace_id!r}")

    ctx = _tool_context()
    result = await commit_workspace(
        ctx, workspace_id=workspace_id, message=body.message, authorized=body.authorized
    )
    if not result.ok:
        raise HTTPException(status_code=400, detail=result.summary)

    get_core().audit.record(
        intent="coding.commit",
        tool="commit_workspace",
        target=workspace.display,
        risk="high",
        confirmation=True,
        result="success",
        detail={"message": body.message, "pushed": False},
    )
    return {"workspace_id": workspace_id, "message": body.message, "pushed": False}


def _tool_context():
    """A ToolContext bound to the running core, for direct tool invocation."""
    from ..tools.base import ToolContext

    core = get_core()
    return ToolContext(
        device_id="laptop_main",
        settings=core.settings,
        apps=core.apps,
        devices=core.devices,
        memory=core.memory,
        graph=core.graph,
        core=core,
    )


# ---------------------------------------------------------------------------
# Design Agent (v1.2)
# ---------------------------------------------------------------------------
class DiagramBody(BaseModel):
    title: str
    components: str = ""
    links: str = ""
    steps: str = ""
    decisions: str = ""


class DesignTextBody(BaseModel):
    title: str
    sections: str = ""
    notes: str = ""
    audience: str = ""
    slides: str = ""
    theme: str = "light"


def _design_store():
    """The design asset store (imported lazily, like the other subsystems)."""
    from ..design.assets import DesignAssetStore

    return DesignAssetStore()


@app.get("/design/assets")
async def design_assets(
    project_id: str = Query(""), kind: str = Query(""), _: None = Protected
) -> Dict[str, Any]:
    """List generated design assets."""
    store = _design_store()
    return {
        "stats": store.stats(),
        "assets": [a.as_dict() for a in store.all(project_id=project_id or None, kind=kind or None)],
    }


@app.get("/design/integrations")
async def design_integrations_endpoint(_: None = Protected) -> Dict[str, Any]:
    """Which external design tools are configured and enabled."""
    from ..design.integrations import describe_integrations

    return describe_integrations()


@app.get("/design/assets/{asset_id}")
async def design_asset_detail(asset_id: str, _: None = Protected) -> Dict[str, Any]:
    store = _design_store()
    asset = store.get(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"no asset {asset_id!r}")
    return {"asset": asset.as_dict(), "content": store.read(asset_id)}


@app.get("/design/assets/{asset_id}/analyze")
async def design_asset_analyze(asset_id: str, _: None = Protected) -> Dict[str, Any]:
    """Measurable properties of an asset."""
    from ..design.review import analyze

    store = _design_store()
    asset = store.get(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"no asset {asset_id!r}")

    content = store.read(asset_id)
    result = analyze(content or "", asset.format, asset.title)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/design/diagram")
async def design_diagram(body: DiagramBody, _: None = Protected) -> Dict[str, Any]:
    """Create an architecture diagram (or a flowchart, if given steps)."""
    store = _design_store()

    if body.steps:
        from ..design.diagrams import flowchart

        result = flowchart(body.title, body.steps, body.decisions)
        summary = f"{result['steps']} step(s)"
        extra = {"steps": result["steps"]}
    else:
        from ..design.diagrams import architecture_diagram

        result = architecture_diagram(body.title, body.components, body.links)
        summary = f"{result['components']} component(s), {result['links']} link(s)"
        extra = {"components": result["components"], "links": result["links"],
                 "problems": result["problems"]}

    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])

    asset = store.save(body.title, "diagram", result["svg"], format="svg", summary=summary)
    return {"asset": asset.as_dict(), **extra}


@app.post("/design/ui")
async def design_ui(body: DesignTextBody, _: None = Protected) -> Dict[str, Any]:
    """Create a UI wireframe as self-contained HTML."""
    from ..design.ui import ui_concept

    result = ui_concept(body.title, body.sections, body.notes)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])

    store = _design_store()
    asset = store.save(
        body.title, "ui_concept", result["html"], format="html",
        summary="Wireframe: " + ", ".join(result["sections"]),
    )
    return {"asset": asset.as_dict(), "sections": result["sections"], "unknown": result["unknown"]}


@app.post("/design/documentation")
async def design_docs(body: DesignTextBody, _: None = Protected) -> Dict[str, Any]:
    """Create a Markdown document outline."""
    from ..design.docs import documentation

    result = documentation(body.title, body.sections, body.audience)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])

    store = _design_store()
    asset = store.save(
        body.title, "documentation", result["markdown"], format="markdown", extension="md",
        summary="Outline: " + ", ".join(result["sections"]),
    )
    return {"asset": asset.as_dict(), "sections": result["sections"]}


@app.post("/design/presentation")
async def design_deck(body: DesignTextBody, _: None = Protected) -> Dict[str, Any]:
    """Create a slide deck as self-contained HTML."""
    from ..design.deck import presentation

    result = presentation(body.title, body.slides, body.theme)
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])

    store = _design_store()
    asset = store.save(
        body.title, "presentation", result["html"], format="html",
        summary="Deck: " + ", ".join(result["slides"]),
    )
    return {"asset": asset.as_dict(), "slides": result["slides"]}


@app.post("/design/assets/{asset_id}/link")
async def design_asset_link(
    asset_id: str, project_id: str = Query(...), _: None = Protected
) -> Dict[str, Any]:
    """Connect an asset to a project in the graph."""
    store = _design_store()
    asset = store.get(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"no asset {asset_id!r}")

    store.link_project(asset_id, project_id)
    node = get_core().graph.add_node(
        project_id=project_id,
        kind="design_asset",
        label=asset.title,
        data={"asset_id": asset_id, "asset_kind": asset.kind, "format": asset.format},
    )
    return {"asset_id": asset_id, "project_id": project_id, "node": node}


@app.post("/design/assets/{asset_id}/delete")
async def design_asset_delete(asset_id: str, _: None = Protected) -> Dict[str, Any]:
    """Delete an asset Vantage created."""
    store = _design_store()
    if store.get(asset_id) is None:
        raise HTTPException(status_code=404, detail=f"no asset {asset_id!r}")

    store.forget(asset_id)
    get_core().audit.record(
        intent="design.delete_asset",
        tool="delete_design_asset",
        target=asset_id,
        risk="high",
        confirmation=True,
        result="success",
    )
    return {"asset_id": asset_id, "deleted": True}


# ---------------------------------------------------------------------------
# Vision Agent (v1.3)
#
# Observe only. There is deliberately no action endpoint here: vision stops at
# PROPOSE, so an observation can never become an action on its own.
# ---------------------------------------------------------------------------
class ImageBody(BaseModel):
    path: str
    query: str = ""
    read: bool = True


class CompareBody(BaseModel):
    first: str
    second: str
    threshold: int = 24


@app.get("/vision/capabilities")
async def vision_capabilities_endpoint(_: None = Protected) -> Dict[str, Any]:
    """What vision Vantage actually has — and what it does not."""
    from ..vision.providers import describe as describe_vision

    return describe_vision()


@app.post("/vision/inspect")
async def vision_inspect(body: ImageBody, _: None = Protected) -> Dict[str, Any]:
    """An image's measurable properties."""
    from ..vision.image import inspect

    result = inspect(body.path)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/vision/read")
async def vision_read(body: ImageBody, _: None = Protected) -> Dict[str, Any]:
    """OCR an image. Returns real recognised text, or an honest failure."""
    from ..vision.ocr import read_text

    result = read_text(body.path)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/vision/find")
async def vision_find(body: ImageBody, _: None = Protected) -> Dict[str, Any]:
    """Locate text in an image, with normalised bounding boxes."""
    from ..vision.ocr import find_text

    if not body.query:
        raise HTTPException(status_code=400, detail="a query is required")

    result = find_text(body.path, body.query)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/vision/screen")
async def vision_screen(body: ImageBody, _: None = Protected) -> Dict[str, Any]:
    """Capture the screen and read text from it."""
    from ..vision.screen import read_screen

    result = read_screen(body.path)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/vision/compare")
async def vision_compare(body: CompareBody, _: None = Protected) -> Dict[str, Any]:
    """Compare two images; report whether and where they differ."""
    from ..vision.diff import compare

    result = compare(body.first, body.second, threshold=max(1, min(body.threshold, 255)))
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


# ---------------------------------------------------------------------------
# Laptop Control Center (v2.x)
#
# A cockpit for the developer's own machine: condition, running processes,
# open-project status and a ready-to-paste agent prompt. Read-only (the prompt
# builder only composes text) and built on the same fixed-argv discipline as
# ``tools.system`` -- no shell, no arbitrary command surface here.
# ---------------------------------------------------------------------------
class AgentPromptBody(BaseModel):
    goal: str
    project: str = ""


@app.get("/laptop/snapshot")
async def laptop_snapshot_endpoint(_: None = Protected) -> Dict[str, Any]:
    """Current machine condition: CPU, memory, disk, battery, uptime."""
    from ..tools import laptop_center

    result = await laptop_center.laptop_snapshot(_tool_context())
    return result.model_dump()


@app.get("/laptop/processes")
async def laptop_processes_endpoint(
    sort_by: str = Query("cpu"),
    limit: str = Query("15"),
    name: str = Query(""),
    _: None = Protected,
) -> Dict[str, Any]:
    """Running processes, sorted by CPU or memory and optionally filtered by name."""
    from ..tools import laptop_center

    result = await laptop_center.laptop_processes(
        _tool_context(), sort_by=sort_by, limit=limit, name=name
    )
    return result.model_dump()


@app.get("/laptop/projects")
async def laptop_projects_endpoint(
    project: str = Query(""),
    _: None = Protected,
) -> Dict[str, Any]:
    """Status of projects open in Vantage (files, languages, tests, git)."""
    from ..tools import laptop_center

    result = await laptop_center.laptop_project_status(_tool_context(), project=project)
    return result.model_dump()


@app.post("/laptop/agent-prompt")
async def laptop_agent_prompt_endpoint(
    body: AgentPromptBody, _: None = Protected
) -> Dict[str, Any]:
    """Build a ready-to-paste agent prompt from machine + project state."""
    from ..tools import laptop_center

    if not body.goal.strip():
        raise HTTPException(status_code=400, detail="a goal is required")
    result = await laptop_center.build_agent_prompt(
        _tool_context(), goal=body.goal, project=body.project
    )
    return result.model_dump()


# ---------------------------------------------------------------------------
# Watch companion (v1.8)
#
# A smartwatch cannot be dialled from this process, so the companion calls in:
# it collects what is queued, says what it actually showed, reports its battery,
# and carries back the wearer's decision.
#
# That last route is the security-relevant one, which is why the decision arrives
# as an inbound POST from the device rather than as a tool the planner could
# call. No tool in this build can resolve a confirmation; only the person
# wearing the watch can, by pressing a button the watch was showing.
# ---------------------------------------------------------------------------

class WatchCollectBody(BaseModel):
    limit: int = 10
    battery: Optional[float] = None
    charging: Optional[bool] = None


class WatchAckBody(BaseModel):
    item_id: str
    displayed: bool = True
    battery: Optional[float] = None
    charging: Optional[bool] = None


class WatchReplyBody(BaseModel):
    item_id: str
    decision: str


@app.get("/watch/status")
async def watch_status_endpoint(_: None = Protected) -> Dict[str, Any]:
    """How the watch link stands: pairing, mode, battery as reported, outbox counts."""
    from ..watch.gateway import get_watch_gateway

    return get_watch_gateway().status()


@app.post("/watch/collect")
async def watch_collect_endpoint(
    body: WatchCollectBody, _: None = Protected
) -> Dict[str, Any]:
    """Hand the companion its queued items and record that it took them.

    Taking proves the item reached the companion, not that it was displayed --
    those stay separate states.
    """
    from ..watch.gateway import get_watch_gateway

    gw = get_watch_gateway()
    gw.note_device_report(battery=body.battery, charging=body.charging)
    items = gw.take(limit=body.limit)
    return {
        "ok": True,
        "delivered_via": "companion_poll",
        "items": [i.as_payload() for i in items],
        "status": gw.status(),
    }


@app.post("/watch/ack")
async def watch_ack_endpoint(body: WatchAckBody, _: None = Protected) -> Dict[str, Any]:
    """The companion says an item was displayed (or swiped away without answering)."""
    from ..watch.gateway import get_watch_gateway

    gw = get_watch_gateway()
    gw.note_device_report(battery=body.battery, charging=body.charging)
    item = gw.ack(body.item_id, displayed=body.displayed)
    if item is None:
        raise HTTPException(status_code=404, detail="no watch item has that id")
    return {"ok": True, "item": item.to_dict(), "status": gw.status()}


@app.post("/watch/reply")
async def watch_reply_endpoint(body: WatchReplyBody, _: None = Protected) -> Dict[str, Any]:
    """Carry the wearer's button press back into the pipeline.

    A prompt tied to a pending confirmation approves or denies exactly that one,
    through the same expiry-checked resolver the CLI and dashboard use. A prompt
    that came from a quick routine runs the routine's command as if it had been
    spoken, so the watch is a remote with a confirmation in front of it and never
    an authority of its own.
    """
    from ..watch.gateway import get_watch_gateway

    gw = get_watch_gateway()
    item, error = gw.reply(body.item_id, body.decision)
    if error:
        raise HTTPException(status_code=400, detail=error)

    approved = item.reply == "CONFIRM"
    result: Dict[str, Any] = {"ok": True, "decision": item.reply, "action": "recorded"}
    core = get_core()
    if item.confirmation_id:
        record = core.policy.get_confirmation(item.confirmation_id) or {}
        response = await core.resolve_confirmation(
            item.confirmation_id, approved, session_id=str(record.get("session_id", ""))
        )
        result["action"] = "confirmation_resolved"
        result["confirmation_id"] = item.confirmation_id
        result["reply"] = response.reply
    elif item.command:
        if not approved:
            result["action"] = "routine_cancelled"
            result["command"] = item.command
            result["reply"] = f"Cancelled. {item.command!r} was not started."
        else:
            request = CommandRequest(text=item.command, source="watch", session_id="watch")
            response = await core.handle(request)
            result["action"] = "routine_dispatched"
            result["command"] = item.command
            result["reply"] = response.reply
    else:
        result["reply"] = "Noted. That prompt had nothing to run."
    result["item"] = item.to_dict()
    return result


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
PHONE_PAGE = Path(__file__).resolve().parents[2] / "dashboard" / "phone.html"
DASHBOARD = Path(__file__).resolve().parents[2] / "dashboard" / "index.html"
AI_CONNECTIONS = Path(__file__).resolve().parents[2] / "dashboard" / "ai_connections.html"
PLANNER_PAGE = Path(__file__).resolve().parents[2] / "dashboard" / "planner.html"
OPERATOR_PAGE = Path(__file__).resolve().parents[2] / "dashboard" / "operator.html"
WORK_PAGE = Path(__file__).resolve().parents[2] / "dashboard" / "work.html"
MISSION_CONTROL = Path(__file__).resolve().parents[2] / "dashboard" / "mission_control.html"


def model_status(settings) -> Dict[str, Any]:
    """Who answers a sentence on this laptop, as data rather than as prose.

    The phone had no idea (63-A): `grep -n "model|provider|llm" dashboard/phone.html` returned
    nothing, five phases after the vendor became selectable and two after the laptop's own panel
    grew a badge that names it. The values are the ones `/health` already publishes to the operator,
    including `provider_paths` -- which no surface had ever read (63-B) -- and they travel in the
    HTML the phone is already allowed to load, because giving a handset `/health` to get one status
    line would widen the surface Phase 58 spent a whole phase narrowing.

    Two rules carried over from the panel's badge, both about not over-claiming: with no key the line
    says rules and names no vendor nobody will call, and an unusable selection is printed as the
    refusal it is rather than falling back to a vendor name.
    """
    paths = provider_capabilities(settings)
    chat = paths["chat"]
    if not settings.llm_enabled:
        answered = "rules (no key)"
    else:
        answered = chat
    return {"llm_enabled": settings.llm_enabled, "answered_by": answered, **paths}


async def phone_client() -> Any:
    """Serve the phone's side of the pairing flow.

    Same rule as the dashboard and for the same reason: the page carries a single-use
    bootstrap code, never the master token, because it is fetched by a device that has
    not been trusted yet. It speaks only endpoints that already exist --
    pair/request, pair/pending, /command, /confirm/{id} -- so it opens no second,
    ungated path into the laptop.
    """
    if not PHONE_PAGE.exists():
        raise HTTPException(status_code=404, detail="phone client not found")
    settings = get_settings()
    # Minted as a *phone* code: the session it later buys can only reach what the
    # phone page calls. The page cannot ask for more, because it is this route,
    # not the request body, that decides.
    bootstrap = (_issue_credential(_bootstraps, BOOTSTRAP_TTL_SECONDS, "phone")
                 if settings.auth_enabled else "")
    # `<` is escaped so a value can never carry a `</script>` into the page, whatever a
    # provider's own error text happens to contain.
    model_json = json.dumps(model_status(settings), default=str).replace("<", "\u003c")
    return HTMLResponse(content=PHONE_PAGE.read_text(encoding="utf-8")
                        .replace("__JARVIS_BOOTSTRAP__", bootstrap)
                        .replace("__JARVIS_MODEL_JSON__", model_json))


@app.get("/phone", include_in_schema=False)
async def phone_page() -> Any:
    return await phone_client()


# ---------------------------------------------------------------------------
# AI Connections (Phase 72)
# ---------------------------------------------------------------------------

@app.get("/planner", include_in_schema=False)
async def planner_page() -> HTMLResponse:
    """The daily planner page (Epic 1).

    Served like /dashboard: public, carrying a single-use bootstrap the page
    spends once at POST /auth/session.
    """
    if not PLANNER_PAGE.exists():
        raise HTTPException(status_code=404, detail="planner page not found")
    settings = get_settings()
    bootstrap = (_issue_credential(_bootstraps, BOOTSTRAP_TTL_SECONDS, "dashboard")
                 if settings.auth_enabled else "")
    return HTMLResponse(
        content=PLANNER_PAGE.read_text(encoding="utf-8").replace("__JARVIS_BOOTSTRAP__", bootstrap),
        media_type="text/html",
    )


@app.get("/agents")
async def list_agents(_: None = Protected) -> Dict[str, Any]:
    """The specialised agents and the tools each one actually owns.

    Capability is derived from the registry, not from a hand-written list: an
    agent owns exactly the tools whose spec names it, and an agent with no tools
    is reported as such rather than presented as usable.
    """
    core = get_core()
    rows = []
    for name, agent in sorted(core.agents.items()):
        tools = sorted(agent.tool_names())
        rows.append({
            "name": name,
            "tool_count": len(tools),
            "tools": tools,
            "usable": bool(tools),
        })
    return {"count": len(rows), "agents": rows}


@app.get("/agents/{name}")
async def agent_detail(name: str, _: None = Protected) -> Dict[str, Any]:
    """One agent, its tools, and what each tool is allowed to do."""
    core = get_core()
    agent = core.agents.get(name)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"no agent {name!r}")

    tools = []
    for tool_name in sorted(agent.tool_names()):
        spec = core.tools.get(tool_name)
        if spec is None:
            continue
        tools.append({
            "name": tool_name,
            "risk": spec.risk_label(),
            "requires_confirmation": spec.requires_confirmation,
            "description": spec.description,
        })
    return {"name": name, "tool_count": len(tools), "tools": tools}


@app.get("/notifications/status")
async def notifications_status(_: None = Protected) -> Dict[str, Any]:
    """Whether banners can be shown, and on what this cannot promise."""
    from ..notify import describe

    info = describe()
    info["note"] = (
        "Scheduled reminders and briefings ask the system for a banner. Acceptance "
        "is not proof a banner appeared: Focus and notification settings can "
        "suppress it, and Vantage cannot see that."
    )
    return info


@app.post("/notifications/test")
async def notifications_test(body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    """Send one banner so the person can see whether it arrives."""
    from ..notify import notify

    body = body or {}
    return notify(title="Vantage", body=str(body.get("message") or
                                            "Notifications are working."))


@app.get("/apps/status")
async def apps_status(_: None = Protected) -> Dict[str, Any]:
    """Which known applications are installed here, and which are running.

    The operator page needs this: a button that offers to open an app the machine
    does not have is a dead end, and the previous list could not tell the
    difference.
    """
    from ..tools.base import ToolContext

    result = await get_core().tools.handler("get_app_status")(ToolContext())
    return {"apps": (result.output or {}).get("apps", [])}


@app.get("/operator", include_in_schema=False)
async def operator_page() -> HTMLResponse:
    """The application operator page (Feature 2).

    A focused surface over ``/command``: launch apps, open documents and sites,
    check machine state. Actions run through the normal pipeline, so permission
    gating and honest verified/unverified results are unchanged.
    """
    if not OPERATOR_PAGE.exists():
        raise HTTPException(status_code=404, detail="operator page not found")
    settings = get_settings()
    bootstrap = (_issue_credential(_bootstraps, BOOTSTRAP_TTL_SECONDS, "dashboard")
                 if settings.auth_enabled else "")
    return HTMLResponse(
        content=OPERATOR_PAGE.read_text(encoding="utf-8").replace("__JARVIS_BOOTSTRAP__", bootstrap),
        media_type="text/html",
    )


@app.get("/work", include_in_schema=False)
async def work_page() -> HTMLResponse:
    """The workspace / workflow supervisor page (Feature 3)."""
    if not WORK_PAGE.exists():
        raise HTTPException(status_code=404, detail="work page not found")
    settings = get_settings()
    bootstrap = (_issue_credential(_bootstraps, BOOTSTRAP_TTL_SECONDS, "dashboard")
                 if settings.auth_enabled else "")
    return HTMLResponse(
        content=WORK_PAGE.read_text(encoding="utf-8").replace("__JARVIS_BOOTSTRAP__", bootstrap),
        media_type="text/html",
    )


@app.get("/ai-connections", include_in_schema=False)
async def ai_connections_page() -> HTMLResponse:
    """AI provider connection settings page.

    Served the same way as /dashboard: public, carrying a single-use bootstrap
    code (minted for the dashboard surface) that the page spends once at
    POST /auth/session. It cannot be a Protected route -- a fresh tab navigation
    has no session token yet, and the whole point of the bootstrap is to let a
    page that has none buy itself a short-lived, revocable one.
    """
    if not AI_CONNECTIONS.exists():
        raise HTTPException(status_code=404, detail="AI connections page not found")
    settings = get_settings()
    bootstrap = (_issue_credential(_bootstraps, BOOTSTRAP_TTL_SECONDS, "dashboard")
                 if settings.auth_enabled else "")
    return HTMLResponse(
        content=AI_CONNECTIONS.read_text(encoding="utf-8")
        .replace("__JARVIS_BOOTSTRAP__", bootstrap),
        media_type="text/html",
    )


# ---------------------------------------------------------------------------
# Mission Control (unified AI command center)
# ---------------------------------------------------------------------------

@app.get("/mission-control", include_in_schema=False)
async def mission_control_page() -> HTMLResponse:
    """Unified AI command center: keys, chat, console, and Mac controls.

    Served with the same bootstrap-auth pattern as /dashboard and /ai-connections.
    """
    if not MISSION_CONTROL.exists():
        raise HTTPException(status_code=404, detail="Mission Control page not found")
    settings = get_settings()
    bootstrap = (_issue_credential(_bootstraps, BOOTSTRAP_TTL_SECONDS, "dashboard")
                 if settings.auth_enabled else "")
    return HTMLResponse(
        content=MISSION_CONTROL.read_text(encoding="utf-8")
        .replace("__JARVIS_BOOTSTRAP__", bootstrap),
        media_type="text/html",
    )


#: What makes the phone page an *installed* thing rather than a tab: a manifest, the
#: icons it names, and a worker that caches the shell. All three are static files from
#: `dashboard/`, served here so the page never points at a path that answers 404.
PHONE_DIR = Path(__file__).resolve().parents[2] / "dashboard"


@app.get("/manifest.webmanifest", include_in_schema=False)
async def phone_manifest() -> Any:
    path = PHONE_DIR / "manifest.webmanifest"
    if not path.exists():
        raise HTTPException(status_code=404, detail="manifest not found")
    return Response(content=path.read_bytes(), media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
async def phone_service_worker() -> Any:
    path = PHONE_DIR / "sw.js"
    if not path.exists():
        raise HTTPException(status_code=404, detail="worker not found")
    # Served from the site root on purpose: a worker's scope is its own directory, and a
    # worker at /phone/sw.js could never intercept the API calls the page makes.
    return Response(content=path.read_bytes(), media_type="application/javascript",
                    headers={"Service-Worker-Allowed": "/"})


@app.get("/icons/{icon_name}", include_in_schema=False)
async def phone_icon(icon_name: str) -> Any:
    """Only the two files that ship, by exact name: no arbitrary file read off disk."""
    if icon_name not in {"icon-192.png", "icon-512.png"}:
        raise HTTPException(status_code=404, detail="no such icon")
    path = PHONE_DIR / icon_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="icon not found")
    return Response(content=path.read_bytes(), media_type="image/png")


@app.get("/dashboard")
async def dashboard() -> Any:
    """Serve the control dashboard shell.

    Public, and it carries a credential -- but no longer *the* credential. The
    master token used to be templated into this HTML, which meant any local
    process could ``GET /dashboard`` and grep the secret out of it. What is
    injected now is a single-use bootstrap code that only this page load can
    spend, once, at ``POST /auth/session``.
    """
    if not DASHBOARD.exists():
        raise HTTPException(status_code=404, detail="dashboard not found")

    settings = get_settings()
    bootstrap = (_issue_credential(_bootstraps, BOOTSTRAP_TTL_SECONDS, "dashboard")
                 if settings.auth_enabled else "")
    html = DASHBOARD.read_text(encoding="utf-8").replace("__JARVIS_BOOTSTRAP__", bootstrap)
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# AI Chat (Phase 73)
# ---------------------------------------------------------------------------
from ..db.database import get_db
from ..logging_setup import get_logger

log = get_logger("api.chat")

AI_CHAT_DIR = Path(__file__).resolve().parents[2] / "dashboard" / "ai_chat.html"


@app.get("/ai/chat", include_in_schema=False)
async def ai_chat_page() -> HTMLResponse:
    """Vantage unified AI chat interface.

    Served the same way as /dashboard and /ai-connections: public, carrying a
    single-use bootstrap code the page spends once at POST /auth/session.

    It used to be a Protected route that served the page **without**
    substituting the bootstrap -- so the page's script sent the literal
    string "__JARVIS_BOOTSTRAP__" as its API token, and every request it
    made came back 401. A fresh tab navigation has no session token yet,
    which is exactly what the bootstrap is for, and the page is linked from
    the dashboard, so it is a page a person reaches.
    """
    if not AI_CHAT_DIR.exists():
        raise HTTPException(status_code=404, detail="AI chat page not found")
    settings = get_settings()
    bootstrap = (_issue_credential(_bootstraps, BOOTSTRAP_TTL_SECONDS, "dashboard")
                 if settings.auth_enabled else "")
    return HTMLResponse(
        content=AI_CHAT_DIR.read_text(encoding="utf-8")
        .replace("__JARVIS_BOOTSTRAP__", bootstrap),
        media_type="text/html",
    )


@app.post("/ai/chats")
async def create_chat(body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    """Create a new AI conversation."""
    db = get_db()
    title = body.get("title", "New conversation") if body else "New conversation"
    chat_id = secrets.token_urlsafe(16)
    now = utcnow().isoformat()
    
    db.execute(
        """INSERT INTO ai_chats (id, title, created_at, updated_at)
           VALUES (?, ?, ?, ?)""",
        (chat_id, title, now, now)
    )
    
    return {"id": chat_id, "title": title, "created_at": now}


@app.get("/ai/chats")
async def list_chats(limit: int = 50, _: None = Protected) -> List[Dict[str, Any]]:
    """List AI conversations, most recently updated first.

    Without this a client could only create and read a conversation whose id
    it already held -- so every page load started a new one and the previous
    conversation became unreachable from the UI that wrote it. The id was
    never lost, only unlistable.

    R2.2b: the listing covers both stores. A conversation that started in the
    macOS app is written by the `/command` pipeline and keyed by its session
    id; it had no `ai_chats` row, so it appeared in no listing at all. The
    service merges the two and names a pipeline session by its opening line.
    """
    core = get_core()
    return core.conversations.list_conversations(limit=limit)


@app.get("/ai/chats/{chat_id}")
async def get_chat(chat_id: str, _: None = Protected) -> Dict[str, Any]:
    """Get a conversation by ID."""
    db = get_db()
    row = db.query(
        "SELECT id, title, created_at, updated_at FROM ai_chats WHERE id = ?",
        (chat_id,)
    )
    if not row:
        raise HTTPException(status_code=404, detail="Chat not found")
    return dict(row[0])


@app.delete("/ai/chats/{chat_id}")
async def delete_chat(chat_id: str, _: None = Protected) -> Dict[str, Any]:
    """Delete a conversation and all its messages, in both stores.

    A conversation listed from the pipeline lives in `messages` under the same
    id as its chat row. Deleting only `ai_messages` and `ai_chats` left those
    turns behind, and because the listing is built from `messages`, the
    conversation came straight back on the next read -- a delete that reported
    success and changed nothing visible is exactly the kind of false success
    this project forbids.
    """
    db = get_db()
    db.execute("DELETE FROM ai_messages WHERE conversation_id = ?", (chat_id,))
    db.execute("DELETE FROM ai_chats WHERE id = ?", (chat_id,))
    db.execute("DELETE FROM messages WHERE session_id = ?", (chat_id,))
    return {"deleted": True}


@app.post("/ai/chats/{chat_id}/messages")
async def save_message(chat_id: str, body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    """Save a message to a conversation."""
    if not body:
        raise HTTPException(status_code=400, detail="No message body provided")
    
    db = get_db()
    
    role = body.get("role", "user")
    content = body.get("content", "")
    provider_meta = json.dumps(body.get("provider_meta", {})) if body.get("provider_meta") else "{}"
    
    msg_id = secrets.token_urlsafe(16)
    now = utcnow().isoformat()
    
    db.execute(
        """INSERT INTO ai_messages (id, conversation_id, role, content, provider_meta, timestamp)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (msg_id, chat_id, role, content, provider_meta, now)
    )
    
    # Update last message time on chat
    db.execute(
        """UPDATE ai_chats SET updated_at = ? WHERE id = ?""",
        (now, chat_id)
    )
    
    return {"id": msg_id, "conversation_id": chat_id, "role": role, "timestamp": now}


@app.get("/ai/chats/{chat_id}/messages")
async def get_chat_messages(chat_id: str, limit: int = 50, _: None = Protected) -> List[Dict[str, Any]]:
    """Get messages from a conversation, from whichever store holds them.

    `ai_messages` is the chat surface's own table; a conversation that started
    in the macOS app lives in `messages`, keyed by its session id. Returning
    only the former meant a listed pipeline conversation opened empty, which is
    a worse outcome than not listing it at all. The shared service merges both
    (de-duplicated by role and content) and bounds the result the same way.
    """
    core = get_core()
    turns = core.conversations.history(
        conversation_id=chat_id,
        session_id=chat_id,
        limit=max(1, min(int(limit or 50), 200)),
    )
    # The service merges on (role, content) and returns the shape a model is
    # fed. The route's contract is the stored row shape, so give the caller the
    # same fields it has always received.
    return [
        {
            "id": "",
            "role": turn["role"],
            "content": turn["content"],
            "provider_meta": "{}",
            "timestamp": "",
        }
        for turn in turns
    ]


#: How many prior turns to thread into a chat completion. Bounded: a whole
#: transcript is both a cost and a relevance problem for a follow-up question.
_CHAT_HISTORY_LIMIT = 12

@app.post("/ai/chat/send")
async def send_ai_message(body: dict = Body(None), _: None = Protected) -> Dict[str, Any]:
    """Send a message to the configured AI provider.

    Uses the restored provider adapters (OpenAI and Gemini) so the request goes
    out in the vendor's own wire shape, honours the configured model and a custom
    base URL, and reports what actually happened: a real successful answer, or the
    provider's own error. It never fabricates a response.
    """
    if not body:
        raise HTTPException(status_code=400, detail="No message body provided")

    text = body.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty text provided")

    # Which provider did the operator configure?
    settings = get_settings()
    try:
        provider_id = select_provider(settings).name
    except ProviderUnknown:
        raise HTTPException(status_code=503, detail="No AI provider configured")

    # The secret lives in the keychain, not in config -- so the adapter is built
    # with the stored key, over the configured endpoint and model.
    api_key = KeychainStorage.retrieve(provider_id)
    if not api_key and getattr(PROVIDERS.get(provider_id), "REQUIRES_KEY", True):
        raise HTTPException(
            status_code=401,
            detail="No API key is stored for this provider. Add one in AI Settings.",
        )

    import types
    runtime_settings = types.SimpleNamespace(
        ai_provider=provider_id,
        llm_api_key=api_key,
        llm_base_url=str(settings.llm_base_url or ""),
    )
    provider = select_provider(runtime_settings)
    model = str(settings.llm_model or "").strip() or provider.name

    # Multi-turn memory: thread the conversation's prior turns to the model so
    # a follow-up ("and the second one?") can resolve. Bounded to the last few
    # turns. This client persists the user message *before* sending, so the
    # in-flight `text` is already the newest user row — drop it to avoid feeding
    # the same sentence in twice. For a client that sends before persisting,
    # that trailing row is simply absent and nothing is dropped, so both orderings
    # stay correct.
    #
    # R2.2: the read goes through the shared conversation service, so this
    # surface and the pipeline surface see the same turns. A `session_id` may
    # also be supplied, in which case the pipeline's turns are included too --
    # a conversation that started on the app and continued in the dashboard is
    # one conversation, not two fragments.
    conversation_id = str(body.get("conversation_id") or "").strip()
    session_id = str(body.get("session_id") or "").strip()
    history: List[Dict[str, str]] = []
    if conversation_id or session_id:
        history = get_core().conversations.history(
            session_id=session_id,
            conversation_id=conversation_id,
            current_text=text,
            limit=_CHAT_HISTORY_LIMIT,
        )

    try:
        reply = await provider.chat("", text, model=model, history=history, timeout=30.0)
    except Exception as exc:  # transport surprise -- report it, don't crash
        log.error("AI request failed", extra_fields={"provider": provider_id, "error": str(exc)[:200]})
        raise HTTPException(status_code=503, detail=f"AI request failed: {str(exc)[:200]}")

    if not reply.ok:
        # Honest failure: the vendor said why it wouldn't. Never a fake success.
        log.info("AI provider reported a failure",
                 extra_fields={"provider": provider_id, "error": reply.error[:160]})
        return {
            "ok": False,
            "response": "",
            "provider": reply.provider,
            "model": reply.model,
            "error": reply.error,
        }

    # R2.2: the chat surface can now *act*, not only answer. The model is
    # asked to propose an action; the proposal is validated and then goes
    # through the same permission gate as a pipeline action, so a proposal is
    # never a grant. A proposal that needs approval comes back as one, and
    # the person is asked -- nothing runs until they answer.
    #
    # The answer and the action are kept distinct: `response` is what the
    # model said, `action` is what was proposed, and neither is ever set to
    # the other's value.
    action: Dict[str, Any] = {}
    if session_id or conversation_id:
        outcome = await get_core().propose_and_execute(
            CommandRequest(text=text, source="ai_chat", session_id=session_id)
        )
        if outcome.plan is not None and outcome.plan.actions:
            planned = outcome.plan.actions[0]
            action = {
                "tool": planned.tool,
                "params": planned.params,
                "reason": planned.reason,
                "status": planned.status.value,
                "executed": bool(outcome.actions),
                "needs_confirmation": bool(outcome.needs_confirmation),
                "confirmation_id": outcome.confirmation_id,
                "decision": outcome.decision.value if outcome.decision else None,
                "results": [
                    {
                        "tool": r.tool,
                        "ok": r.ok,
                        "message": r.message,
                        "error": r.error,
                        "verified": r.verified,
                        "verification": r.verification.value,
                    }
                    for r in outcome.actions
                ],
            }

    return {
        "ok": True,
        "response": reply.text,
        "provider": reply.provider,
        "model": reply.model,
        "notes": reply.notes or {},
        "action": action,
    }




# ---------------------------------------------------------------------------
# Connection Health (Phase 73)
# ---------------------------------------------------------------------------
@app.get("/ai/connection-health")
async def get_connection_health(_: None = Protected) -> Dict[str, Any]:
    """Report connection health for all configured providers (persisted state).

    Read-only and cheap: a GET must not fire a live network call per provider (the
    UI polls it, and a slow route here loops). It reports the same truthful
    persisted state as ``/ai/providers``; the explicit Test button is the action
    that re-verifies a provider.
    """
    results = {}
    for spec in list_providers():
        card = _provider_card(spec.id)
        state = card.get("test_status", "not_configured")
        results[spec.id] = {
            "configured": card.get("configured", False),
            "authorized": card.get("configured", False),
            "verified": card.get("verified", False),
            "status": {
                "passed": "available",
                "failed": "unavailable",
                "untested": "untested",
            }.get(state, "not_configured"),
            "last_error": card.get("last_error") or None,
            "last_verification": card.get("last_verified_at") or None,
        }
    return {"providers": results}


# ============================================================================
# AI ATTACHMENT SYSTEM (Phase 74 - Multimodal Support)
# ============================================================================

class AttachmentRequest(BaseModel):
    """Schema for attachment operations."""
    message_id: str
    file_type: str
    filename: str
    metadata: Dict[str, Any] = {}


@app.post("/ai/attachments/upload")
async def upload_attachment(
    file: UploadFile = File(...),
    message_id: str = Form(...),
    file_type: str = Form(""),
    filename: str = Form(""),
    _: None = Protected
) -> Dict[str, Any]:
    """Upload an attachment for an AI chat message.
    
    Supports:
    - Images: PNG, JPEG, GIF, WebP
    - Documents: PDF
    - Audio: WAV, MP3
    - Text: Plain text
    
    Each file must have a valid message_id association.
    """
    from ..config import get_settings
    
    # Validate request
    if not message_id:
        raise HTTPException(status_code=400, detail="message_id is required")
    
    # Validate MIME type and size
    allowed_types = {
        'image/png', 'image/jpeg', 'image/gif', 'image/webp',
        'application/pdf',
        'audio/wav', 'audio/mp3', 'audio/mpeg',
        'text/plain'
    }
    
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {file.content_type}. "
                   f"Allowed: {', '.join(allowed_types)}"
        )
    
    # Check file size (10MB limit for Phase 74)
    max_size = 10 * 1024 * 1024  # 10MB
    content = await file.read()
    if len(content) > max_size:
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Maximum size: {max_size / (1024*1024)}MB"
        )
    
    # Generate secure storage path
    settings = get_settings()
    attachments_dir = settings.home / "attachments" / "ai"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    file_ext = Path(file.filename).suffix or ".bin"
    storage_filename = f"{timestamp}-{secrets.token_urlsafe(12)}{file_ext}"
    storage_path = attachments_dir / storage_filename
    
    # Write file to disk
    with open(storage_path, "wb") as f:
        f.write(content)
    
    # Save to database
    db = get_db()
    attachment_id = secrets.token_urlsafe(16)
    now = utcnow().isoformat()
    
    db.execute(
        """INSERT INTO ai_attachments 
           (id, message_id, file_type, file_size, filename, storage_path, 
            upload_status, processing_status, metadata, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            attachment_id,
            message_id,
            file.content_type,
            len(content),
            file.filename,
            str(storage_path),
            "uploaded",
            "pending",
            json.dumps({}),
            now
        )
    )
    
    log.info(
        "attachment uploaded",
        extra_fields={
            "attachment_id": attachment_id,
            "file_type": file.content_type,
            "size_bytes": len(content),
            "filename": file.filename,
        },
    )
    
    return {
        "success": True,
        "attachment_id": attachment_id,
        "filename": file.filename,
        "file_type": file.content_type,
        "size_bytes": len(content),
        "status": "uploaded",
    }


@app.get("/ai/attachments/{attachment_id}")
async def get_attachment(attachment_id: str, _: None = Protected) -> Dict[str, Any]:
    """Get attachment metadata without exposing the file content."""
    db = get_db()
    row = db.query(
        "SELECT * FROM ai_attachments WHERE id = ?",
        (attachment_id,)
    )
    
    if not row:
        raise HTTPException(status_code=404, detail="Attachment not found")
    
    return {
        "id": row[0]["id"],
        "message_id": row[0]["message_id"],
        "file_type": row[0]["file_type"],
        "file_size": row[0]["file_size"],
        "filename": row[0]["filename"],
        "upload_status": row[0]["upload_status"],
        "processing_status": row[0]["processing_status"],
        "metadata": json.loads(row[0]["metadata"]),
        "created_at": row[0]["created_at"],
    }


@app.delete("/ai/attachments/{attachment_id}")
async def delete_attachment(attachment_id: str, _: None = Protected) -> Dict[str, Any]:
    """Delete an attachment both from database and filesystem."""
    db = get_db()
    
    # Get the attachment first
    rows = db.query(
        "SELECT * FROM ai_attachments WHERE id = ?",
        (attachment_id,)
    )
    
    if not rows:
        raise HTTPException(status_code=404, detail="Attachment not found")
    
    row = rows[0]
    storage_path = Path(row["storage_path"])
    
    # Delete from filesystem
    if storage_path.exists():
        try:
            storage_path.unlink()
        except Exception as e:
            log.warning("failed to delete attachment file", extra_fields={"error": str(e)})
    
    # Delete from database
    db.execute("DELETE FROM ai_attachments WHERE id = ?", (attachment_id,))
    
    return {
        "success": True,
        "message": "Attachment deleted successfully",
    }


def _attachment_result(metadata: Any) -> Dict[str, Any]:
    """The stored processing result, read back safely.

    ``metadata`` holds whatever the processing produced. A row written before
    the result was persisted has ``{}``; a row whose metadata is not valid JSON
    (or not an object) is treated the same way. Returning an empty dict is
    honest -- "nothing was stored" -- and a client renders it as "not
    processed yet" rather than showing a broken result.
    """
    if isinstance(metadata, dict):
        return metadata
    if not metadata:
        return {}
    try:
        value = json.loads(metadata)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


@app.get("/ai/attachments/messages/{message_id}")
async def get_attachments_for_message(message_id: str, _: None = Protected) -> Dict[str, Any]:
    """Get all attachments for a specific message."""
    db = get_db()
    rows = db.query(
        "SELECT * FROM ai_attachments WHERE message_id = ? ORDER BY created_at ASC",
        (message_id,)
    )
    
    return {
        "message_id": message_id,
        "attachments": [
            {
                "id": row["id"],
                "file_type": row["file_type"],
                "filename": row["filename"],
                "file_size": row["file_size"],
                "upload_status": row["upload_status"],
                "processing_status": row["processing_status"],
                # What the processing actually produced -- the extracted text,
                # the transcript, the measurement, or the reason it failed.
                # This is the field a client shows the person, so it is read
                # from the stored result rather than left to be re-derived.
                "result": _attachment_result(row["metadata"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ],
        "total_count": len(rows),
    }


@app.post("/ai/process-attachments")
async def process_attachments(
    body: Dict[str, Any] = Body(None),
    _: None = Protected
) -> Dict[str, Any]:
    """Process pending attachments (vision, transcription, PDF parsing)."""
    from ..core.models import new_id
    
    attachments_to_process = body.get("attachment_ids", [])
    
    if not attachments_to_process:
        # Process all pending attachments
        db = get_db()
        rows = db.query(
            "SELECT id FROM ai_attachments WHERE processing_status = 'pending'",
            ()
        )
        attachments_to_process = [row["id"] for row in rows]
    
    results = []
    
    for attachment_id in attachments_to_process:
        db = get_db()
        row = db.query(
            "SELECT * FROM ai_attachments WHERE id = ?",
            (attachment_id,)
        )
        
        if not row:
            results.append({
                "attachment_id": attachment_id,
                "status": "not_found"
            })
            continue
        
        row = row[0]
        file_path = Path(row["storage_path"])
        file_type = row["file_type"]
        
        try:
            # No real processing pipeline is implemented yet for any of these types.
            # Per the honest-reporting contract, an attachment must NOT be marked
            # "processed" (and a placeholder must not be presented as extracted
            # content) when nothing was actually processed. Each branch reports a
            # truthful status and a matching DB processing_status.
            if file_type.startswith('image'):
                # Real, local image inspection (Pillow). A measurement — size,
                # mode, brightness, dominant colours — is a genuine processing
                # result, not a placeholder.
                from ..vision.image import inspect as inspect_image
                info = inspect_image(str(file_path))
                if info.get("ok"):
                    processing_result = {
                        "status": "processed",
                        "type": "image_vision",
                        "summary": info.get("summary", ""),
                        "width": info.get("width"),
                        "height": info.get("height"),
                        "format": info.get("format"),
                        "mode": info.get("mode"),
                    }
                    db_status = "processed"

                    # The measurement says what the image *is*; the OCR says
                    # what it *says*. A screenshot is the common case, and
                    # reporting only its dimensions is like reporting the
                    # envelope of a letter. So the text is extracted too, when
                    # the recogniser can read it.
                    #
                    # The honesty rule is the same one the rest of this route
                    # follows: text is reported only when the recogniser
                    # actually returned it. An image with no text, a
                    # recogniser that is unavailable, and a recogniser that
                    # failed are three different facts, and none of them is
                    # "the image was processed and here is its text".
                    from ..vision.ocr import read_text as ocr_read_text
                    ocr = ocr_read_text(str(file_path))
                    if ocr.get("ok"):
                        extracted = str(ocr.get("text") or "").strip()
                        if extracted:
                            processing_result["text"] = extracted
                            processing_result["text_lines"] = len(ocr.get("lines") or [])
                        else:
                            # Read successfully, and there was nothing written
                            # in it. Said plainly, so "no text" is not read
                            # later as "text was extracted".
                            processing_result["text"] = ""
                            processing_result["text_note"] = (
                                "The image was read, and it contains no text."
                            )
                    else:
                        processing_result["text_note"] = (
                            "I measured the image but could not read any text "
                            "in it."
                        )
                else:
                    # Unreadable image -> a truthful failure, never a fake success.
                    processing_result = {
                        "status": "error",
                        "type": "image_vision",
                        "reason": info.get("error", "could not inspect the image"),
                    }
                    db_status = "error"

            elif file_type == 'application/pdf':
                # Real PDF text extraction (pypdf). A document that yields text is
                # genuinely processed; encrypted/unreadable is a truthful failure.
                from ..attachments.pdf import extract_text as extract_pdf
                pdf = extract_pdf(str(file_path))
                if pdf.get("ok"):
                    processing_result = {
                        "status": "processed",
                        "type": "pdf_extraction",
                        "text": pdf.get("text", ""),
                        "pages": pdf.get("pages"),
                        "truncated": pdf.get("truncated", False),
                    }
                    db_status = "processed"
                else:
                    processing_result = {
                        "status": "error",
                        "type": "pdf_extraction",
                        "reason": pdf.get("error", "could not extract PDF text"),
                    }
                    db_status = "error"

            elif file_type.startswith('audio'):
                # Transcribe through the configured STT backend — but only claim
                # "processed" when a REAL backend actually produced text. A mock/echo
                # backend (TranscriptSTT, real=False) must not be reported as ears that
                # heard the attachment (the same honesty rule as /voice/transcribe).
                from ..voice.stt import get_stt
                stt = get_stt(get_settings())
                if not stt.real:
                    processing_result = {
                        "status": "not_processed",
                        "type": "audio_transcription",
                        "reason": "no real speech-to-text backend is available to transcribe this recording",
                    }
                    db_status = "not_processed"
                else:
                    result = await stt.transcribe(Path(file_path))
                    if result.ok and result.text.strip():
                        processing_result = {
                            "status": "processed",
                            "type": "audio_transcription",
                            "text": result.text.strip(),
                            "confidence": result.confidence,
                            "backend": result.backend,
                        }
                        db_status = "processed"
                    else:
                        processing_result = {
                            "status": "error",
                            "type": "audio_transcription",
                            "reason": result.error or "transcription produced no text",
                        }
                        db_status = "error"

            else:
                processing_result = {
                    "status": "unsupported",
                    "type": "unsupported",
                    "reason": "No processing defined for this type",
                }
                db_status = "unsupported"

            # Update database to the HONEST status — never "processed" when
            # processing has not occurred.
            #
            # The result itself is persisted too, not just the status. It used
            # to be returned to the caller and then dropped on the floor: the
            # `metadata` column stayed `{}` forever, so a client that listed a
            # message's attachments later saw "processed" and no way to learn
            # what was extracted. The extraction is the product here — a
            # screenshot's text is the answer, not its dimensions — so it has
            # to survive the request that produced it.
            try:
                stored = json.dumps(processing_result, default=str)
            except (TypeError, ValueError):
                stored = json.dumps({"status": processing_result.get("status", "")})
            db.execute(
                "UPDATE ai_attachments SET processing_status = ?, metadata = ? WHERE id = ?",
                (db_status, stored, attachment_id)
            )

            results.append({
                "attachment_id": attachment_id,
                **processing_result
            })

        except Exception as e:
            log.error("attachment processing failed", extra_fields={
                "attachment_id": attachment_id,
                "error": str(e)
            })

            results.append({
                "attachment_id": attachment_id,
                "status": "error",
                "error": str(e)
            })

    return {
        # Count only attachments that were ACTUALLY processed (a real pipeline
        # produced output). Not "number of results" — that would overstate.
        "processed_count": sum(1 for r in results if r.get("status") == "processed"),
        "results": results,
    }


# ============================================================================
# WORKFLOW ROUTES (Phase 77)
# ============================================================================

@app.get("/workflows")
async def list_workflows(_: None = Protected) -> Dict[str, Any]:
    """List available workflow templates."""
    from ..workflows.engine import TEMPLATE_DEFINITIONS
    
    workflows = []
    for wf_id, name, desc, source, trigger in TEMPLATE_DEFINITIONS:
        workflows.append({
            "workflow_id": wf_id,
            "name": name,
            "description": desc,
            "source": source,
            "trigger": trigger,
        })
    
    return {"workflows": workflows}


@app.post("/workflows/{template_name}/execute")
async def execute_workflow(
    template_name: str,
    body: Dict[str, Any] = Body(None),
    _: None = Protected
) -> Dict[str, Any]:
    """Execute a workflow template."""
    
    if not body:
        raise HTTPException(status_code=400, detail="body is required")
    
    session_id = body.get("session_id", "")
    user_request = body.get("user_request", "")
    
    try:
        from ..workflows.orchestrator import WorkflowOrchestrator

        # Workflow steps run blocking processes (a test suite, a build) for
        # minutes. Called directly from an `async def` route that work held the
        # event loop, so the whole server stopped answering -- a 14-minute
        # RUN_TESTS left /planner timing out. Worker thread, not the loop.
        import asyncio

        orchestrator = WorkflowOrchestrator()
        result = await asyncio.to_thread(
            orchestrator.execute_workflow_request,
            user_input=user_request or f"Run {template_name}",
            session_id=session_id,
            workflow_template=template_name,
        )

        return result
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/workflows/status/{run_id}")
async def get_workflow_status(run_id: str, _: None = Protected) -> Dict[str, Any]:
    """Get status of a running workflow."""
    
    from ..workflows.orchestrator import WorkflowOrchestrator
    
    orchestrator = WorkflowOrchestrator()
    status = orchestrator.get_workflow_status(run_id)
    
    if not status:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    
    return {"status": status}


@app.post("/workflows/cancel")
async def cancel_workflow(
    body: Dict[str, Any] = Body(None),
    _: None = Protected
) -> Dict[str, Any]:
    """Cancel a running workflow."""
    
    if not body:
        raise HTTPException(status_code=400, detail="body is required")
    
    session_id = body.get("session_id", "")
    run_id = body.get("run_id")
    
    from ..workflows.orchestrator import WorkflowOrchestrator
    
    orchestrator = WorkflowOrchestrator()
    success = orchestrator.cancel_workflow(session_id, run_id)
    
    return {
        "success": success,
        "cancelled_run_id": run_id,
        "session_id": session_id,
    }


@app.get("/workflows/active/{session_id}")
async def get_active_workflows(session_id: str, _: None = Protected) -> Dict[str, Any]:
    """Get all active workflows for a session."""
    
    from ..workflows.orchestrator import WorkflowOrchestrator
    
    orchestrator = WorkflowOrchestrator()
    active = orchestrator.get_running_workflows(session_id)
    
    return {
        "active_workflows": active,
        "count": len(active),
    }


# ---------------------------------------------------------------------------
# Google Calendar + Tasks
#
# The connection lifecycle, status, and the two read paths. Writes to Google
# Calendar are not exposed: the connection is read-only by scope, and offering
# a button that can only fail is worse than not having it.
# ---------------------------------------------------------------------------


@app.get("/google/status")
async def google_status(_: None = Protected) -> Dict[str, Any]:
    """The real connection state, verified rather than remembered.

    ``status()`` makes blocking network calls (token refresh + tokeninfo). Run off
    the event loop: called directly from an async route it would freeze every
    other request for the duration of the 20s network ceiling, which is exactly
    the defect the workflow route already had (see the RUN_TESTS comment).
    """
    from ..integrations.google_auth import GoogleAuth, describe_google

    auth = GoogleAuth()
    status = await asyncio.to_thread(auth.status)
    return {
        "connection": status.as_dict(),
        "setup": describe_google(),
    }


@app.post("/google/connect/begin")
async def google_connect_begin(request: Request, _: None = SameOriginProtected) -> Dict[str, Any]:
    """Start an authorization. Returns the URL to open, never a token."""
    from ..integrations.google_auth import GoogleAuth, GoogleAuthError

    # The redirect must point at this backend, because that is where the
    # exchange happens. Built from the request so a non-default port works.
    port = request.url.port or 8765
    redirect_uri = f"http://127.0.0.1:{port}/google/callback"
    # `begin` itself is in-memory, but it takes the pending lock a concurrent
    # `complete` may hold; keep it off the loop for the same reason.
    try:
        pending = await asyncio.to_thread(GoogleAuth().begin, redirect_uri)
    except GoogleAuthError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"url": pending["url"], "state": pending["state"],
            "redirect_uri": redirect_uri}


@app.get("/google/callback")
async def google_callback(code: str = Query(""), state: str = Query(""),
                          error: str = Query("")) -> HTMLResponse:
    """Google's redirect target.

    Unauthenticated by necessity -- Google's redirect carries no Vantage token.
    That is safe because the exchange is bound by the single-use ``state`` and
    the PKCE verifier held in memory: a forged callback cannot complete an
    authorization it did not start. Nothing private is rendered here.
    """
    from ..integrations.google_auth import GoogleAuth, GoogleAuthError

    def page(title: str, body: str, ok: bool) -> HTMLResponse:
        colour = "#1a7f37" if ok else "#b42318"
        return HTMLResponse(
            f"<!doctype html><meta charset=utf-8><title>{title}</title>"
            "<body style=\"font-family:-apple-system,system-ui,sans-serif;"
            "max-width:520px;margin:80px auto;padding:0 24px;color:#111\">"
            f"<h2 style=\"color:{colour}\">{title}</h2><p>{body}</p>"
            "<p style=\"color:#666;font-size:14px\">You can close this tab.</p>"
            "</body>"
        )

    if error:
        return page("Google authorisation was cancelled",
                    "Nothing was connected.", ok=False)
    if not code or not state:
        return page("That link is incomplete",
                    "Google did not return an authorisation code.", ok=False)
    try:
        # The exchange is a blocking network call (up to the 20s ceiling). On
        # the loop it would stall every other request while Google answers.
        status = await asyncio.to_thread(GoogleAuth().complete, code, state)
    except GoogleAuthError as exc:
        return page("Could not connect Google", str(exc), ok=False)
    account = f" as {status.account}" if status.account else ""
    return page("Connected to Google",
                f"Vantage can now read your calendar and tasks{account}.", ok=True)


@app.post("/google/disconnect")
async def google_disconnect(_: None = SameOriginProtected) -> Dict[str, Any]:
    """Forget the local token and ask Google to revoke it."""
    from ..integrations.google_auth import GoogleAuth

    # Revocation is a blocking network call; see the note on google_status.
    return await asyncio.to_thread(GoogleAuth().disconnect)


@app.get("/google/calendar/events")
async def google_calendar_events(
    date: str = Query("today"),
    days: int = Query(1),
    _: None = Protected,
) -> Dict[str, Any]:
    """Events from the connected Google calendar.

    Deliberately Google-only rather than consulting the active provider: this
    endpoint exists so a screen can show *Google's* answer and its connection
    state together. A failure carries its reason and a non-success status --
    it is never presented as an empty calendar.
    """
    from ..integrations.google_calendar import GoogleCalendarProvider
    from ..tools.calendar import _resolve_range

    start, end, problem = _resolve_range(date, days)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    provider = GoogleCalendarProvider()
    # A Google read is a blocking network call, bounded only by the 20s ceiling.
    results = await asyncio.to_thread(provider.list_events, start, end, 200)
    return {
        "provider": provider.name,
        "status": results.status.value,
        "read_succeeded": results.status.read_succeeded,
        "detail": results.detail,
        "count": len(results),
        "events": [e.as_dict() for e in results],
    }


@app.get("/google/tasks")
async def google_tasks(
    list_id: str = Query(""),
    show_completed: bool = Query(False),
    limit: int = Query(100),
    _: None = Protected,
) -> Dict[str, Any]:
    """Tasks in the connected account's list."""
    from ..integrations.google_tasks import GoogleTasksClient

    results = await asyncio.to_thread(
        GoogleTasksClient().tasks, list_id=list_id,
        show_completed=show_completed, limit=limit)
    return {
        "provider": "google_tasks",
        "status": results.status.value,
        "read_succeeded": results.status.read_succeeded,
        "detail": results.detail,
        "count": len(results),
        "tasks": [t.as_dict() for t in results],
    }


@app.get("/google/tasks/lists")
async def google_task_lists(_: None = Protected) -> Dict[str, Any]:
    """The account's task lists, for choosing which one Vantage uses."""
    from ..integrations.google_tasks import GoogleTasksClient

    return await asyncio.to_thread(GoogleTasksClient().lists)


def _google_write_status(result: Dict[str, Any]) -> int:
    """The status a failed Google write deserves.

    A blanket 502 told a client "the upstream failed" for three different
    problems, one of which is the operator's own setup. The distinction matters
    because the fix differs: finish the configuration, connect an account, or
    wait for Google.
    """
    status = str(result.get("status") or "")
    if status in ("not_configured",):
        return 409                       # this server is not set up for Google yet
    if status in ("auth_error", "unavailable"):
        # No usable Google connection: either Google refused the stored token
        # or none was ever connected. Both are the caller's to fix by
        # connecting; neither is "Google was asked and did not deliver".
        return 401
    return 502                           # Google was asked and did not deliver


@app.post("/google/tasks")
async def google_task_create(body: dict = Body(None), _: None = SameOriginProtected) -> Dict[str, Any]:
    """Create a task in Google Tasks.

    A write, so it is same-origin protected. It reports ``ok`` from Google's own
    answer, never optimistically.
    """
    from datetime import datetime as _datetime

    from ..integrations.google_tasks import GoogleTasksClient

    payload = body or {}
    title = str(payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=422, detail="A task needs a title.")
    due = None
    raw_due = payload.get("due")
    if raw_due:
        try:
            due = _datetime.fromisoformat(str(raw_due))
        except ValueError:
            raise HTTPException(status_code=422,
                                detail="due must be an ISO date, e.g. 2026-11-01.")
    result = await asyncio.to_thread(
        GoogleTasksClient().create,
        title=title,
        notes=str(payload.get("notes") or ""),
        due=due,
        list_id=str(payload.get("list_id") or ""),
        source=str(payload.get("source") or ""),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=_google_write_status(result),
                            detail=result.get("error") or "Google refused it.")
    return result


@app.post("/google/tasks/{task_id}/complete")
async def google_task_complete(task_id: str, body: dict = Body(None),
                               _: None = SameOriginProtected) -> Dict[str, Any]:
    """Mark a Google task completed, verified against what Google reports."""
    from ..integrations.google_tasks import GoogleTasksClient

    result = await asyncio.to_thread(
        GoogleTasksClient().complete, task_id,
        list_id=str((body or {}).get("list_id") or ""))
    if not result.get("ok"):
        raise HTTPException(status_code=_google_write_status(result),
                            detail=result.get("error") or "Google refused it.")
    return result


@app.delete("/google/tasks/{task_id}")
async def google_task_delete(task_id: str, list_id: str = Query(""),
                             confirm: bool = Query(False),
                             _: None = SameOriginProtected) -> Dict[str, Any]:
    """Delete a Google task.

    The only operation in this integration that cannot be undone, so it needs
    `confirm=true` the same way cancelling an event does. Deleting a task is
    not the same act as completing it: completion keeps the record and the
    person can see what they finished, deletion removes it.
    """
    from ..integrations.google_tasks import GoogleTasksClient

    if not confirm:
        raise HTTPException(
            status_code=409,
            detail="Deleting a task is permanent. Send confirm=true to mean it.")
    result = await asyncio.to_thread(
        GoogleTasksClient().delete, task_id, list_id=list_id)
    if not result.get("ok"):
        raise HTTPException(status_code=_google_write_status(result),
                            detail=result.get("error") or "Google refused it.")
    return result

@app.exception_handler(Exception)
async def unhandled(_request: Any, exc: Exception) -> JSONResponse:  # pragma: no cover
    log.info("unhandled api error", extra_fields={"error": str(exc)})
    return JSONResponse(status_code=500, content={"detail": str(exc)})


# ---------------------------------------------------------------------------
# MCP (one remote connector)
#
# Status and discovery only. There is deliberately no "call a tool" route: an
# MCP tool is invoked through the tool registry, which means it carries the
# EXTERNAL risk tier and the confirmation workflow. A bare POST that ran an
# arbitrary allowlisted tool would be a second path around the approval gate.
# ---------------------------------------------------------------------------


@app.put("/mcp/config")
async def mcp_configure(body: dict = Body(None), _: None = SameOriginProtected) -> Dict[str, Any]:
    """Save the MCP endpoint and allowlist from the Connections screen.

    Validated before saving, because a typo here is a connector that silently
    never connects. Only ``http``/``https`` URLs are accepted: a ``file:`` or
    ``ftp:`` endpoint is not something this transport can speak, and accepting it
    would defer the failure to a confusing network error.

    The bearer token is **not** accepted here. A credential posted to a save
    endpoint would be a secret in a request body, a log line and a diagnostic
    payload; it stays in the Keychain (``KeychainStorage`` under ``mcp``) or the
    environment, exactly like a provider key.
    """
    from ..gateway.selection import save_mcp_config
    from ..integrations.mcp_client import parse_allowed_tools

    body = body or {}
    url = str(body.get("url", "") or "").strip()
    if url:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise HTTPException(
                status_code=422,
                detail=("The MCP endpoint must be an http:// or https:// URL with a host. "
                        f"{url!r} is not one."))
    allowed_raw = body.get("allowed_tools", None)
    allowed = parse_allowed_tools(allowed_raw) if isinstance(allowed_raw, str) else None
    saved = save_mcp_config(
        url=url,
        allowed_tools=(",".join(allowed) if allowed is not None else None),
        name=str(body.get("name", "") or "") or None,
    )
    # A connector built before this call holds the old config; drop the cached one
    # so the next status/execute reads what was just saved.
    from ..integrations import mcp_client

    mcp_client.configure(None)
    return {"saved": True, "config": {k: v for k, v in saved.items() if k != "token"},
            "allowed_tools": parse_allowed_tools(saved.get("allowed_tools", ""))}

@app.get("/mcp/status")
async def mcp_status(probe: bool = Query(True), _: None = Protected) -> Dict[str, Any]:
    """The MCP connection, verified by a live handshake unless ``probe=false``.

    ``connected`` means an ``initialize`` + ``tools/list`` round trip succeeded.
    A configured URL alone is ``disconnected``. The payload carries no token:
    ``MCPConfig.public()`` redacts the URL and never serialises the credential.
    """
    from ..integrations import mcp_client

    connector = mcp_client.active_connector() or mcp_client.connector_from_settings(
        get_settings())
    return {
        "connection": connector.status(probe=bool(probe)),
        "setup": mcp_client.describe_mcp(connector.config),
        "callable_tools": [mcp_client_tool_name(connector, t)
                           for t in connector.config.allowed_tools],
    }


def mcp_client_tool_name(connector: Any, remote: str) -> str:
    """The Vantage tool name a remote MCP tool registers under."""
    from ..tools.mcp_tools import tool_name

    return tool_name(connector.config.name, remote)


# ---------------------------------------------------------------------------
# n8n automation
#
# Four routes, each answering a question the UI actually asks. There is
# deliberately no route that lists *n8n's* workflows, creates one, edits one,
# activates one, or reaches its admin API: Vantage executes a closed registry of
# approved workflows through their webhooks, and a chat turn or a phone cannot
# widen that registry.
# ---------------------------------------------------------------------------

@app.get("/n8n/status")
async def n8n_status(probe: bool = Query(True), _: None = Protected) -> Dict[str, Any]:
    """The n8n integration's real state.

    ``ready`` means the configured server answered its own health endpoint just
    now. A configured URL alone is not ``ready``: this project's repeated defect
    is a state that answers a different question than the reader assumes.
    The payload never contains the API key — ``N8NConfig.public`` cannot.
    """
    from ..integrations import n8n as n8n_mod
    from ..integrations.n8n_client import client_from_settings

    config = n8n_mod.config_from_settings(get_settings())
    report = n8n_mod.validate_config(config)
    if probe and report["state"] == n8n_mod.ConnectionState.READY.value:
        check = client_from_settings().readiness()
        report["state"] = check["state"]
        report["detail"] = check.get("detail", "")
        if "latency_ms" in check:
            report["latency_ms"] = check["latency_ms"]
    return report

@app.get("/n8n/workflows")
async def n8n_workflows(_: None = Protected) -> Dict[str, Any]:
    """The approved workflows, with the inputs each one requires.

    This is Vantage's registry, not n8n's list. A workflow appears here only
    because a person added it to ``integrations/n8n.py``.
    """
    from ..integrations import n8n as n8n_mod

    config = n8n_mod.config_from_settings(get_settings())
    entries = []
    for item in n8n_mod.catalog():
        item = dict(item)
        item["enabled"] = config.permits(item["key"])
        entries.append(item)
    return {"workflows": entries, "enabled": config.enabled, "count": len(entries)}

@app.post("/n8n/workflows/{key}/run")
async def n8n_run_workflow(key: str, body: dict = Body(None),
                           _: None = SameOriginProtected) -> Dict[str, Any]:
    """Execute one approved workflow.

    Same-origin protected like every other write. The body may carry ``inputs``
    and ``confirmed``; it may **not** carry a workflow id, a URL, or a
    correlation id — the path's ``key`` is resolved against the registry, and
    the correlation id is generated here so a caller cannot claim another
    execution's history.

    A consequential workflow without ``confirmed: true`` is refused with 409.
    An execution that *ran and failed* is a 200 with ``ok: false`` (the request
    was valid and n8n answered); a *refusal* is a 4xx.
    """
    from ..integrations import n8n as n8n_mod
    from ..integrations.n8n_audit import record
    from ..integrations.n8n_client import ExecutionStatus, client_from_settings

    body = body or {}
    spec = n8n_mod.workflow(key)
    if spec is None:
        raise HTTPException(
            status_code=404,
            detail=(f"There is no approved workflow called {key!r}. "
                    f"Approved: {', '.join(n8n_mod.workflow_keys())}."))
    if spec.risk.requires_confirmation and not bool(body.get("confirmed")):
        raise HTTPException(
            status_code=409,
            detail=(f"“{spec.title}” can affect things outside Vantage. Send "
                    "confirmed: true once you have approved exactly what it will do."))
    inputs = body.get("inputs") or {}
    if not isinstance(inputs, dict):
        raise HTTPException(status_code=422, detail="inputs must be an object.")

    client = client_from_settings()
    result = await asyncio.to_thread(client.execute, spec.key, inputs,
                                     confirmed=bool(body.get("confirmed")))
    record(result, session=str(body.get("session_id") or ""))
    payload = result.as_dict()
    if result.status is ExecutionStatus.REFUSED:
        raise HTTPException(status_code=403, detail=result.message)
    return payload

@app.put("/n8n/workflows/{key}")
async def n8n_toggle_workflow(key: str, body: dict = Body(None),
                              _: None = SameOriginProtected) -> Dict[str, Any]:
    """Enable or disable one approved workflow **in Vantage**.

    This never touches n8n: the workflow keeps its webhook and its history. It
    only decides whether Vantage will run it, which is the safe way to take a
    workflow out of reach without deleting anything.
    """
    from ..gateway.selection import n8n_config, save_n8n_config
    from ..integrations import n8n as n8n_mod

    spec = n8n_mod.workflow(key)
    if spec is None:
        raise HTTPException(status_code=404,
                            detail=(f"There is no approved workflow called {key!r}. "
                                    f"Approved: {', '.join(n8n_mod.workflow_keys())}."))
    body = body or {}
    if "enabled" not in body:
        raise HTTPException(status_code=422, detail="Send enabled: true or false.")

    saved = n8n_config(get_settings().home)
    current = [k for k in (saved.get("allowed_keys") or "").split(",") if k.strip()]
    if not current:
        # An empty allowlist means "every registered workflow", so the first
        # explicit change has to start from that set.
        current = n8n_mod.workflow_keys()
    enabled = set(current)
    if bool(body.get("enabled")):
        enabled.add(spec.key)
    else:
        enabled.discard(spec.key)
    save_n8n_config(home=get_settings().home, allowed_keys=",".join(sorted(enabled)))
    return {"saved": True, "key": spec.key, "enabled": spec.key in enabled,
            "allowlist": sorted(enabled)}


@app.get("/n8n/workflows/{key}/status")
async def n8n_workflow_status(key: str, correlation_id: str = Query(""),
                              _: None = Protected) -> Dict[str, Any]:
    """What Vantage knows about one workflow's recent run.

    Deliberately Vantage's own record, not n8n's execution list: that lives on
    the admin API this integration cannot reach, and reading it would mean
    holding an admin credential. A run Vantage triggered is a run Vantage can
    answer for, and that is what this returns.
    """
    from ..integrations import n8n as n8n_mod
    from ..integrations.n8n_audit import recent

    if n8n_mod.workflow(key) is None:
        raise HTTPException(status_code=404,
                            detail=f"There is no approved workflow called {key!r}.")
    rows = [r for r in recent(100) if r.get("workflow") == key]
    if correlation_id:
        rows = [r for r in rows if r.get("correlation_id") == correlation_id]
    latest = rows[0] if rows else None
    return {"workflow": key, "found": latest is not None,
            "latest": latest, "count": len(rows)}


@app.get("/n8n/executions")
async def n8n_executions(limit: int = Query(20), _: None = Protected) -> Dict[str, Any]:
    """Vantage's own record of what it asked n8n to run.

    Deliberately not n8n's execution list: that lives on the admin API, which
    this integration cannot reach. This is the audit trail — workflow key,
    correlation id, outcome and a safe error summary, with no credential.
    """
    from ..integrations.n8n_audit import recent

    bounded = max(1, min(int(limit), 100))
    return {"executions": recent(bounded), "count": len(recent(bounded))}


def run() -> None:  # pragma: no cover - convenience entrypoint
    import uvicorn

    from ..log_safety import install as install_log_safety

    install_log_safety()
    settings = get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port)
