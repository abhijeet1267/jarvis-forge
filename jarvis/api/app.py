"""FastAPI surface for Vantage (section 37, Phase 0).

Exposes the orchestrator, registries, audit log and memory over HTTP so the
dashboard, the CLI and (later) the phone/watch companions all talk to the same
permissioned core.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, field_validator

from .. import __version__
from ..config import get_settings, llm_source
from ..core.models import CommandRequest, CommandResponse
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
from ..security.privacy import PrivacyManager, describe_privacy
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

    # v0.8: run scheduled jobs in the background while the server is up.
    core = get_core()
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

    try:
        yield
    finally:
        await core.scheduler.stop()


app = FastAPI(
    title="Vantage Core",
    version=__version__,
    description="Permissioned multi-agent personal AI operating system (v0.3 core).",
    lifespan=lifespan,
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
# ---------------------------------------------------------------------------
BOOTSTRAP_TTL_SECONDS = 60
SESSION_TTL_SECONDS = 8 * 60 * 60

#: Hard ceiling on live credentials, so nothing here grows without bound.
MAX_DASHBOARD_CREDENTIALS = 512

_bootstraps: Dict[str, float] = {}
_sessions: Dict[str, float] = {}
_credential_lock = threading.Lock()


def _issue_credential(table: Dict[str, float], ttl: float) -> str:
    """Create an unguessable credential that expires in ``ttl`` seconds."""
    code = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _credential_lock:
        for stale in [key for key, exp in table.items() if exp <= now]:
            table.pop(stale, None)
        while len(table) >= MAX_DASHBOARD_CREDENTIALS:
            # Dicts keep insertion order, so this drops the oldest live entry.
            table.pop(next(iter(table)))
        table[code] = now + ttl
    return code


def _redeem_credential(table: Dict[str, float], code: Optional[str]) -> bool:
    """True when ``code`` is live. It is removed either way: single use."""
    if not code:
        return False
    with _credential_lock:
        expiry = table.pop(code, None)
    return expiry is not None and expiry > time.monotonic()


def _session_is_live(code: Optional[str]) -> bool:
    """True for a session token that has not expired. Leaves it in place."""
    if not code:
        return False
    with _credential_lock:
        expiry = _sessions.get(code)
    return expiry is not None and expiry > time.monotonic()


def clear_dashboard_credentials() -> None:
    """Revoke every outstanding bootstrap and session token."""
    with _credential_lock:
        _bootstraps.clear()
        _sessions.clear()


async def require_auth(request: Request) -> None:
    """Dependency that enforces the API token on protected routes."""
    settings = get_settings()
    if not settings.auth_enabled:
        return

    presented = request.headers.get(TOKEN_HEADER)
    if not presented:
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            presented = authorization[7:]

    # A dashboard session token is as good as the master token here -- it is
    # shorter-lived, revocable, and never touches disk.
    if _session_is_live(presented):
        return

    if not get_token_store().verify(presented):
        log.info("rejected unauthenticated request", extra_fields={"path": request.url.path})
        raise HTTPException(status_code=401, detail="Missing or invalid API token.")


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
    elif origin not in settings.allowed_origins():
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
class CommandBody(BaseModel):
    text: str
    session_id: Optional[str] = None
    device_id: str = "laptop_main"
    source: str = "api"
    context: Dict[str, Any] = {}

    @field_validator("text")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("command text must not be empty")
        return value.strip()


class MemoryBody(BaseModel):
    content: str
    scope: str = "long_term"
    project: Optional[str] = None


class BootstrapBody(BaseModel):
    """The single-use code the dashboard was served with on page load."""

    bootstrap: str = ""


class VoiceTextBody(BaseModel):
    text: str
    speak: bool = False
    require_wake: Optional[bool] = None

    @field_validator("text")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("text must not be empty")
        return value.strip()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
async def root() -> Dict[str, Any]:
    return {
        "name": "Vantage Core",
        "version": __version__,
        "docs": "/docs",
        "dashboard": "/dashboard",
        "tools": len(get_core().tools.names()),
    }


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


@app.get("/health")
async def health(_: None = Protected) -> Dict[str, Any]:
    core = get_core()
    return {
        "status": "ok",
        "tools": len(core.tools.names()),
        "agents": list(core.agents.keys()),
        "llm_enabled": core.settings.llm_enabled,
        # "configured" and "reachable" are not the same claim, and neither is
        # "set in the shell" and "set in a file that failed to load".
        "llm_source": llm_source(),
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
    }


@app.get("/auth/token")
async def auth_token(_: None = SameOriginProtected) -> Dict[str, Any]:
    """Issue a fresh dashboard session to an already-authenticated client.

    This route used to hand the **master API token** to any caller: its
    ``Origin`` check was conditional, so a header-less ``curl`` -- no
    ``Origin``, no token -- was never tested at all and walked away with the
    secret that authorises every tool, which also made the 0600 on
    ``$JARVIS_HOME/api_token`` moot.

    It now refuses a foreign or header-less caller *before* it will even say
    whether a credential is good, and what it returns is a session token. The
    master token never leaves the process.
    """
    settings = get_settings()
    if not settings.auth_enabled:
        return {"auth_enabled": False, "token": None, "expires_in": 0}
    return {
        "auth_enabled": True,
        "token": _issue_credential(_sessions, SESSION_TTL_SECONDS),
        "expires_in": SESSION_TTL_SECONDS,
    }


@app.post("/auth/session")
async def auth_session(request: Request, body: BootstrapBody) -> Dict[str, Any]:
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
        return {"auth_enabled": False, "session_token": None, "expires_in": 0}

    if not _redeem_credential(_bootstraps, body.bootstrap.strip()):
        # Expired, already spent, or invented. The response deliberately says
        # nothing about which, so it cannot be used to probe live codes.
        log.info("rejected dashboard bootstrap", extra_fields={"path": request.url.path})
        raise HTTPException(status_code=403, detail="Bootstrap refused.")

    return {
        "auth_enabled": True,
        "session_token": _issue_credential(_sessions, SESSION_TTL_SECONDS),
        "expires_in": SESSION_TTL_SECONDS,
    }


@app.post("/command", response_model=CommandResponse)
async def command(body: CommandBody, _: None = Protected) -> CommandResponse:
    """Run one natural-language command through the full pipeline."""
    core = get_core()
    request = CommandRequest(
        text=body.text,
        device_id=body.device_id,
        source=body.source,
        context=body.context,
    )
    if body.session_id:
        request.session_id = body.session_id
    return await core.handle(request)


@app.post("/confirm/{confirmation_id}", response_model=CommandResponse)
async def confirm(
    confirmation_id: str,
    approve: bool = Query(True),
    session_id: str = Query(""),
    _: None = Protected,
) -> CommandResponse:
    """Approve or deny a pending confirmation."""
    core = get_core()
    return await core.resolve_confirmation(confirmation_id, approve, session_id)


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
            "parameters": s.parameters,
        }
        for s in get_core().tools.all()
    ]


@app.get("/audit")
async def audit(limit: int = 50, _: None = Protected) -> List[Dict[str, Any]]:
    return get_core().audit.recent(limit)


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
        scope = MemoryScope.LONG_TERM
    project_id = core.graph.get_or_create_project(body.project) if body.project else None
    item = core.memory.remember(body.content, scope=scope, project_id=project_id)
    return item.model_dump(mode="json")


@app.get("/projects")
async def projects(_: None = Protected) -> List[Dict[str, Any]]:
    return get_core().graph.list_projects()


# ---------------------------------------------------------------------------
# Voice (Phase 2)
# ---------------------------------------------------------------------------
@app.get("/voice/status")
async def voice_status(_: None = Protected) -> Dict[str, Any]:
    """Report every voice subsystem and whether it can actually run here."""
    return get_voice().status()


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
    """Synthesise text to speech and report where the audio landed."""
    result = await get_voice().speak(body.text)
    return {
        "ok": result.ok,
        "backend": result.backend,
        "played": result.played,
        "audio_path": str(result.audio_path) if result.audio_path else None,
        "error": result.error,
        "duration_ms": result.duration_ms,
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


def _audit_summary(core) -> Dict[str, Any]:
    rows = core.audit.recent(limit=500)
    total = len(rows)
    by_result: Dict[str, int] = {}
    by_tool: Dict[str, int] = {}
    for row in rows:
        result = row["result"] or "unknown"
        # Very early builds stored a message here instead of a status token.
        # Label those honestly rather than inventing a status for them.
        if len(result) > 32:
            result = "legacy_message"
        by_result[result] = by_result.get(result, 0) + 1
        tool = row["tool"] or row["intent"] or "unknown"
        by_tool[tool] = by_tool.get(tool, 0) + 1

    chain = core.audit.verify_chain()
    return {
        "sampled": total,
        "by_result": by_result,
        "top_tools": dict(sorted(by_tool.items(), key=lambda kv: kv[1], reverse=True)[:10]),
        "chain_intact": chain["ok"],
        "chain_checked": chain["checked"],
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


@app.get("/contacts")
async def list_contacts(_: None = Protected) -> List[Dict[str, Any]]:
    rows = get_db().query("SELECT contact_id, name, address FROM contacts ORDER BY name")
    return [dict(r) for r in rows]


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
    bootstrap = _issue_credential(_bootstraps, BOOTSTRAP_TTL_SECONDS) if settings.auth_enabled else ""
    return HTMLResponse(content=PHONE_PAGE.read_text(encoding="utf-8").replace(
        "__JARVIS_BOOTSTRAP__", bootstrap))


@app.get("/phone", include_in_schema=False)
async def phone_page() -> Any:
    return await phone_client()


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
    bootstrap = _issue_credential(_bootstraps, BOOTSTRAP_TTL_SECONDS) if settings.auth_enabled else ""
    html = DASHBOARD.read_text(encoding="utf-8").replace("__JARVIS_BOOTSTRAP__", bootstrap)
    return HTMLResponse(content=html)


@app.exception_handler(Exception)
async def unhandled(_request: Any, exc: Exception) -> JSONResponse:  # pragma: no cover
    log.info("unhandled api error", extra_fields={"error": str(exc)})
    return JSONResponse(status_code=500, content={"detail": str(exc)})


def run() -> None:  # pragma: no cover - convenience entrypoint
    import uvicorn

    settings = get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port)
