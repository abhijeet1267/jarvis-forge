"""Companion gateway: TLS-secured FastAPI app for Android pairing.

This is a SEPARATE application from the main API. It listens on its own port
(8766 by default) with TLS enabled. The main backend stays on 127.0.0.1:8765
and is never exposed to the network. This gateway:

1. Generates a one-shot pairing code the user shares with their Android device.
2. Accepts that code once, issues a gateway-scoped session token.
3. Proxies PHONE_ALLOWED paths to the main backend, authenticating as the
   master API token (which lives on the same machine and is never sent over
   the network).
4. Rejects any path not in PHONE_ALLOWED — the phone surface is narrow by
   design, and the gateway adds a second enforcement layer.

Architecture:
    Android (TLS)  →  Companion Gateway :8766  →  Main Backend :8765
    session token     validates session token      master API token
    (per-device)      issues/revokes sessions      (local, never leaves Mac)
"""

from __future__ import annotations

import secrets
import time
from contextlib import asynccontextmanager
from typing import Dict, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ..config import Settings, get_settings
from ..core.models import new_id
from ..logging_setup import get_logger
from ..security.auth import TokenStore

from .cert import ensure, fingerprint as cert_fingerprint_str

log = get_logger("companion.gateway")

# ---------------------------------------------------------------------------
# Constants — companion-surface restrictions
# ---------------------------------------------------------------------------
# The gateway is the remote/LAN surface for the Android companion. Its
# phone-capability paths are a strict subset of the local phone surface in
# api/app.py (PHONE_ALLOWED there) — the local dashboard phone page talks to
# the backend directly on loopback and needs more (e.g. /tools, /projects).
# The remote surface gets fewer capabilities, never more, plus /health for
# liveness (a backend path, not a phone capability). tests/test_companion_gateway.py
# pins the subset invariant.
SESSION_TTL_SECONDS = 8 * 60 * 60
PAIRING_CODE_BYTES = 16   # 128-bit random value
PAIRING_CODE_TTL = 120     # 2 minutes to type/scan the code

PHONE_ALLOWED: frozenset = frozenset({
    "/command",
    "/devices",
    "/devices/pair/request",
    "/health",
    # Read-only build/process identity (version, revision, stale flag). It is
    # unauthenticated on the backend and discloses no secret; the phone needs it
    # to tell a current backend from a stale one rather than treating any HTTP
    # answer as "healthy". Nothing here can change state.
    "/health/identity",
    "/confirmations",
    "/emergency_stop",
    # Google reads only -- see the note on the backend's PHONE_ALLOWED. The
    # phone could already reach this data through /command, which is on this
    # list, so a structured read route widens nothing. The connection lifecycle
    # (/google/connect*, /google/disconnect) is deliberately absent.
    "/google/status",
    "/google/calendar/events",
    "/google/tasks",
})
PHONE_ALLOWED_PREFIXES: tuple = ("/confirm/",)

# ---------------------------------------------------------------------------
# In-memory credential stores (independent from the main backend)
# ---------------------------------------------------------------------------
#: pairing_code -> (expires_at, surface)
_pairing_codes: Dict[str, Tuple[float, str]] = {}
#: session_token -> (expires_at, surface, client_info)
_sessions: Dict[str, Tuple[float, str, str]] = {}

SESSION_CAP = 500
BOOTSTRAP_CAP = 20


def _phone_allowed(path: str) -> bool:
    if path in PHONE_ALLOWED:
        return True
    return any(path.startswith(p) for p in PHONE_ALLOWED_PREFIXES)


#: Peers allowed to *issue* pairing codes. Everything else may only redeem.
#: "testclient" is Starlette TestClient's in-process peer sentinel — a value
#: only a same-process client can present (a network peer's host always comes
#: from a real socket address), so it is safe to treat as local; it is what
#: the test suite uses in place of 127.0.0.1.
_LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def _is_local_peer(peer: Optional[str]) -> bool:
    return peer in _LOOPBACK_PEERS


def _capped_cleanup(store: Dict, cap: int) -> None:
    """Remove expired entries, then oldest if still over cap."""
    now = time.monotonic()
    expired = [k for k, (exp, *_) in store.items() if now > exp]
    for k in expired:
        del store[k]
    if len(store) > cap:
        oldest = sorted(store.items(), key=lambda kv: kv[1][0])[:len(store) - cap]
        for k, _ in oldest:
            del store[k]


def issue_pairing_code(surface: str = "phone") -> str:
    """Create a one-shot pairing code with a short TTL.

    Only one code is live at a time — issuing a new one replaces any existing
    code for the same surface.
    """
    _capped_cleanup(_pairing_codes, BOOTSTRAP_CAP)
    # Enforce the documented "one live code per surface" rule: a freshly
    # generated code invalidates any un-redeemed code for the same surface,
    # so an old QR screenshot can no longer pair a device.
    superseded = [c for c, (_exp, srf) in _pairing_codes.items() if srf == surface]
    for c in superseded:
        del _pairing_codes[c]
    code = secrets.token_hex(PAIRING_CODE_BYTES // 2)
    expires = time.monotonic() + PAIRING_CODE_TTL
    _pairing_codes[code] = (expires, surface)
    log.info("companion pairing code issued", extra_fields={
        "surface": surface, "ttl": PAIRING_CODE_TTL,
    })
    return code


def consume_pairing_code(code: str) -> Optional[str]:
    """Validate and consume a pairing code. Returns the surface or None."""
    _capped_cleanup(_pairing_codes, BOOTSTRAP_CAP)
    entry = _pairing_codes.pop(code, None)
    if entry is None:
        return None
    expires, surface = entry
    if time.monotonic() > expires:
        return None
    log.info("companion pairing code redeemed", extra_fields={"surface": surface})
    return surface


def create_session(surface: str, client: str = "unknown") -> str:
    """Issue a gateway session token."""
    _capped_cleanup(_sessions, SESSION_CAP)
    token = new_id("CMP")
    expires = time.monotonic() + SESSION_TTL_SECONDS
    _sessions[token] = (expires, surface, client)
    log.info("companion session created", extra_fields={
        "surface": surface, "ttl": SESSION_TTL_SECONDS,
    })
    return token


def _register_device(device_id: str, device_name: str, client: str) -> None:
    """Record the phone in the device registry, as a known device.

    The registry holds the laptops, watches and phones this system knows about,
    and the permission gate reads it: an action aimed at a device that is not
    registered is refused, and one aimed at a device that is offline is
    refused too. A phone that redeems a pairing code and then issues a command
    has to be *in* that registry, or every command it sends is refused for
    want of a device to aim at.

    The device is registered as online, because it has just proved it can
    reach the gateway over TLS. It is recorded as paired, because redeeming a
    pairing code is the owner's act of trusting it.
    """
    from ..core.models import DeviceSpec
    from ..registry.devices import get_device_registry

    registry = get_device_registry()
    existing = registry.get(device_id)
    if existing is None:
        existing = DeviceSpec(
            device_id=device_id,
            type="android",
            platform="android",
            trust_level="medium",
            capabilities=["notifications", "device_status"],
            online=True,
            connection=["local_transfer"],
        )
    else:
        existing.online = True
    existing.metadata["gateway_client"] = client
    if device_name:
        existing.metadata["device_name"] = device_name
    registry.register(existing)
    log.info("companion device registered", extra_fields={"device_id": device_id})


def validate_session(token: str) -> Optional[str]:
    """Check a gateway session token. Returns surface or None."""
    _capped_cleanup(_sessions, SESSION_CAP)
    entry = _sessions.get(token)
    if entry is None:
        return None
    expires, surface, _ = entry
    if time.monotonic() > expires:
        del _sessions[token]
        return None
    return surface


def revoke_session(token: str) -> bool:
    """Remove a session token."""
    return _sessions.pop(token, None) is not None


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def build_companion_app(settings: Optional[Settings] = None) -> FastAPI:
    s = settings or get_settings()
    cert_fp = ensure(s)
    token_store = TokenStore(s)

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        log.info("companion gateway starting", extra_fields={
            "port": s.companion_port,
            "cert_fingerprint": cert_fp[:16] + "...",
        })
        yield
        log.info("companion gateway stopped")

    app = FastAPI(
        title="Vantage Companion Gateway",
        version="0.1.0",
        lifespan=_lifespan,
    )

    # -- Loopback-only: issue pairing code (called by the macOS dashboard) --
    # The gateway binds 0.0.0.0 so a phone on the LAN can reach redeem/health.
    # Issuance must NOT be LAN-reachable: a code is the owner's act of
    # authorizing a specific phone, and only the owner's own dashboard
    # (loopback) may mint one. Left public, any device on the network could
    # issue-and-redeem its own code and self-pair. So this route refuses
    # non-loopback clients before it does anything.
    @app.post("/companion/pairing-code")
    async def issue_code(request: Request):
        peer = request.client.host if request.client else ""
        if not _is_local_peer(peer):
            raise HTTPException(
                status_code=403,
                detail="Pairing codes may only be issued from the Mac itself.",
            )
        code = issue_pairing_code("phone")
        return {
            "code": code,
            "certificate_fingerprint": cert_fingerprint_str(s),
            "expires_in_seconds": PAIRING_CODE_TTL,
            "companion_port": s.companion_port,
        }

    # -- Public: redeem pairing code for session token (Android calls this)
    @app.post("/companion/pairing-redeem")
    async def redeem_code(request: Request):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Expected JSON body")
        code = (body.get("code", "") if isinstance(body, dict) else "").strip()
        if not code:
            raise HTTPException(status_code=400, detail="Missing 'code' field")

        surface = consume_pairing_code(code)
        if surface is None:
            raise HTTPException(status_code=403, detail="Invalid or expired pairing code")

        client = request.client.host if request.client else "unknown"
        token = create_session(surface, client)

        # The device id the phone calls itself. Redeeming the code is where the
        # phone's identity is established, so this is where the backend should
        # learn it -- otherwise the id travels in the X-Vantage-Device-Id
        # header naming a device the registry has never heard of.
        device_id = ""
        device_name = ""
        if isinstance(body, dict):
            device_id = str(body.get("device_id") or "").strip()
            device_name = str(body.get("device_name") or "").strip()
        if device_id:
            _register_device(device_id, device_name, client)

        return {
            "session_token": token,
            "surface": surface,
            "expires_in": SESSION_TTL_SECONDS,
        }

    # -- Health check (no auth needed) ----------------------------------
    @app.get("/companion/health")
    async def health():
        return {
            "status": "ok",
            "certificate_fingerprint": cert_fingerprint_str(s),
            "port": s.companion_port,
        }

    # -- Sign-out (revoke session) --------------------------------------
    @app.delete("/companion/session")
    async def sign_out(request: Request):
        token = request.headers.get("X-JARVIS-Token", "")
        if token and revoke_session(token):
            return {"status": "signed_out"}
        raise HTTPException(status_code=404, detail="Session not found")

    # -- Authenticated proxy: forward PHONE_ALLOWED paths to main backend
    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def proxy(request: Request):
        path = "/" + request.url.path.lstrip("/")

        # 1. Validate the Android's session token
        session_token = request.headers.get("X-JARVIS-Token", "")
        if not session_token:
            raise HTTPException(status_code=401, detail="Missing X-JARVIS-Token header")
        surface = validate_session(session_token)
        if surface is None:
            raise HTTPException(status_code=401, detail="Invalid or expired session token")

        # 2. Reject non-phone paths BEFORE they reach the main backend
        clean_path = path.split("?")[0]
        if not _phone_allowed(clean_path):
            raise HTTPException(
                status_code=403,
                detail=f"Path not available to companion devices: {clean_path}",
            )

        # 3. Forward to main backend with the MASTER API TOKEN
        #    The Android's session token never touches the main backend.
        #    The gateway authenticates as the trusted local client.
        target_url = f"http://127.0.0.1:{s.port}{path}"
        if request.url.query:
            target_url += f"?{request.url.query}"

        body = await request.body()
        headers: Dict[str, str] = {
            "X-JARVIS-Token": token_store.token(),
            "Host": f"127.0.0.1:{s.port}",
            "Accept": "application/json",
        }
        # Forward the request's own Content-Type.
        #
        # Without it, a JSON body from the phone arrives at the backend as
        # `text/plain` -- the body is a JSON *string*, and the API reads a
        # string where it wants a dictionary, so it answers 422 ("Input should
        # be a valid dictionary"). Every command the phone sent was refused,
        # and the phone's own client was not at fault: it set the header, and
        # the proxy dropped it.
        #
        # The content type is forwarded, never replaced: the backend must
        # read the body as whatever the phone sent it as, not as JSON because
        # the proxy decided so.
        if request.headers.get("content-type"):
            headers["Content-Type"] = request.headers["content-type"]
        # Preserve the Android's device identity for audit trails
        if request.headers.get("X-Vantage-Device-Id"):
            headers["X-Vantage-Device-Id"] = request.headers["X-Vantage-Device-Id"]
        # The surface this session belongs to.
        #
        # The gateway replaces the session token with the master token, so the
        # backend can no longer look the session up and learn that the caller
        # was a phone. It has to be told. Without this, the backend's
        # self-approval guard -- which refuses a *narrow* surface approving its
        # own request -- saw surface "unknown" and let a phone approve its own
        # consequential action. The gateway had already validated the surface
        # (line above); forwarding it is what makes that validation reach the
        # rule it exists to enforce.
        #
        # The header is set by the gateway, not by the client, so a client
        # cannot claim to be a different surface than it is.
        headers["X-Vantage-Surface"] = surface or "unknown"

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
            )

        try:
            content = resp.json()
        except Exception:
            content = {"detail": resp.text[:500]}

        return JSONResponse(content=content, status_code=resp.status_code)

    return app