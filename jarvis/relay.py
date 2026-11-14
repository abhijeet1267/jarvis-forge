"""Phase 59 — an authenticated relay: a door with a bouncer, not a second laptop.

Vantage is reachable from the wifi it is standing on and nowhere else. The honest fix is not to
open the laptop to the internet; it is a relay that sits outside the house and forwards **only**
requests it can attribute, while every decision stays inside.

The trust shape this module implements:

```
client ──(PSK handshake: challenge ──> proof over challenge+device+timestamp)──> relay
client ──(X-JARVIS-Token: its own host session)────────────────────────────────> relay
relay  ──(the same header, unchanged; device_id from what signed)──────────────> host
                                                                                  │
                                            PermissionEngine / confirmation / tool /
                                            verification / audit  ── all still the host's ──
```

Four properties are load-bearing, and each has a test rather than a comment:

1. **The relay never holds the master token, and never holds a session of its own.** It carries
   the credential the client presented. A relay with a laptop-wide token would turn one stolen
   relay session into total control of the machine, and would make every command look like it
   came from the laptop itself.
2. **Identity comes from what you can sign.** `device_id` in a forwarded body is overwritten with
   the device that passed the PSK handshake, because `CommandBody.device_id` is otherwise a string
   the caller types.
3. **Revocation is checked per request, not per session.** The device's key file is looked up on
   every forward, so `revoke` takes effect immediately instead of at the end of a TTL. That is one
   file read on a path that is already crossing a network.
4. **Down is down, never empty.** A host that will not answer produces `502 host_unavailable` or
   `504 host_timeout`, never `[]`. An outage that reads as an empty dataset is the bug this project
   has already fixed twice — for mail and for calendar.

Freshness is the one place the existing primitive is not enough. `pairing.verify_proof` signs the
challenge alone, which is fine for a signature bound to one live TLS connection; a relay session is
a new envelope that can be captured and resent, so the signed payload here adds the device id and a
timestamp, challenges are single-use, and the timestamp window is bounded. No new cryptography —
same PSK, same HMAC-SHA256, same `hmac.compare_digest`.

**What this file does not do:** prove anything about reaching the laptop from outside a network.
Every test and every live check is loopback. A local hop cannot demonstrate a path through a
router, an ISP or a firewall, and pretending otherwise is how a demo becomes a lie.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .logging_setup import get_logger
from .security.privacy import redact

# Imported at module level, not inside build_relay_app: FastAPI resolves a route's annotations
# through the function's *globals*, so a function-local ``Request`` import makes ``request:
# Request`` look like a required query parameter and every proxied call answers 422.
from fastapi import Body, FastAPI, Request
from fastapi.responses import JSONResponse, Response

#: The project's own logger, because ``extra_fields`` is how every other module gets structured
#: context; a stdlib logger raises TypeError on it, which is a crash hiding inside a log line.
log = get_logger("jarvis.relay")

#: Where the relay session rides. Deliberately not ``X-JARVIS-Token``: that header belongs to the
#: host credential, and the two must never be confused for one another.
SESSION_HEADER = "X-Relay-Session"

#: What a remote client may ask for. Narrower than the phone's own list on purpose: a component
#: sitting outside the house should carry the least it can, and ``/health`` is here only because a
#: remote client otherwise cannot tell a closed laptop from a broken one.
RELAY_ALLOWED = frozenset({
    "/health",
    "/command",
    "/confirmations",
    "/devices",
    "/tools",
    "/emergency_stop",
    "/voice/status",
})
RELAY_ALLOWED_PREFIXES = ("/confirm/",)

#: Only these headers cross the relay. Everything else is dropped, including hop-by-hop noise and
#: anything that could turn the relay into an open proxy for a third host.
FORWARDED_HEADERS = ("x-jarvis-token", "authorization", "content-type")

CHALLENGE_TTL_S = 60.0
TIMESTAMP_SKEW_S = 120.0
MAX_TRACKED_CHALLENGES = 512
DEFAULT_SESSION_TTL_S = 15 * 60.0
#: A relay hop inside the sentence deadline Phase 34 gives a command, with room for both legs.
DEFAULT_TIMEOUT_S = 12.0


class RelayUnavailable(Exception):
    """The host could not be reached at all. An outage, not an empty answer."""


class RelayTimeout(Exception):
    """The host was reached and did not answer in time. Also an outage, and a different one."""


def relay_proof(secret: bytes, challenge: str, device_id: str, timestamp: int) -> str:
    """HMAC-SHA256 over the challenge, the device and the timestamp.

    The device id is inside the signature so a challenge issued for one handset cannot be used by
    another, and the timestamp is inside it so a captured handshake ages out.
    """
    body = f"{challenge}\n{device_id}\n{int(timestamp)}".encode("utf-8")
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


@dataclass
class _Challenge:
    device_id: str
    expires_at: float


@dataclass
class _Session:
    device_id: str
    expires_at: float


@dataclass
class RelayService:
    """The relay's whole state: who may call, and where to send them."""

    host_base: str
    pairing: Any
    sender: Optional[Callable[..., Tuple[int, bytes]]] = None
    clock: Callable[[], float] = time.time
    session_ttl: float = DEFAULT_SESSION_TTL_S
    timeout: float = DEFAULT_TIMEOUT_S
    surface: str = "relay"
    _challenges: Dict[str, _Challenge] = field(default_factory=dict)
    _sessions: Dict[str, _Session] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.sender is None:
            self.sender = self._httpx_sender
        if not str(self.host_base).startswith(("http://", "https://")):
            raise ValueError("host_base must be an http(s) URL")

    # -- device identity ----------------------------------------------------

    def secret_for(self, device_id: str) -> Optional[bytes]:
        """The PSK, if this device is the one a person approved and has not revoked.

        Read from the pairing store on every use rather than copied into relay state: that is what
        makes ``revoke`` take effect immediately instead of at the end of a session's life.
        """
        if not device_id:
            return None
        try:
            return self.pairing.load_secret(str(device_id).strip())
        except Exception:
            log.warning("pairing store could not be consulted", extra_fields={"device": device_id})
            return None

    # -- handshake ----------------------------------------------------------

    def new_challenge(self, device_id: str) -> Optional[Tuple[str, float]]:
        """Issue a single-use challenge: ``(code, mint_time)``, or None when this device is not
        authorised to have one.

        The mint time goes back to the caller because it is part of what gets signed: the client
        signs the clock the relay is using, not its own, so a captured handshake cannot be stamped
        as fresh later.
        """
        if self.secret_for(device_id) is None:
            return None
        self._forget_stale()
        if len(self._challenges) >= MAX_TRACKED_CHALLENGES:
            self._challenges.pop(next(iter(self._challenges)))
        code = secrets.token_hex(16)
        now = self.clock()
        self._challenges[code] = _Challenge(device_id=str(device_id), expires_at=now + CHALLENGE_TTL_S)
        return code, now

    def redeem(self, *, device_id: str, challenge: str, proof: str,
               timestamp: float) -> Tuple[Optional[str], str]:
        """Spend a challenge for a relay session. Returns ``(token_or_None, reason)``.

        The challenge is removed whether or not the proof was right, so a failed attempt cannot be
        used to probe a live challenge for a second guess — the same rule the host's bootstrap
        codes follow.
        """
        record = self._challenges.pop(str(challenge or ""), None)
        if record is None:
            return None, "unknown or already spent"
        now = self.clock()
        if record.expires_at < now:
            return None, "expired"
        if record.device_id != str(device_id):
            return None, "issued to another device"
        minted_at = record.expires_at - CHALLENGE_TTL_S
        if abs(float(timestamp) - minted_at) > TIMESTAMP_SKEW_S:
            # The client signed a timestamp the relay handed out; a value minutes away from that
            # is either a replay of something old or a clock that cannot be trusted.
            return None, "timestamp outside the allowed window"
        secret = self.secret_for(device_id)
        if secret is None:
            return None, "device is not paired"
        expected = relay_proof(secret, str(challenge), str(device_id), int(timestamp))
        if not hmac.compare_digest(expected, str(proof or "").strip()):
            return None, "proof did not verify"
        token = secrets.token_urlsafe(24)
        self._sessions[token] = _Session(device_id=str(device_id), expires_at=now + self.session_ttl)
        log.info("relay session issued", extra_fields={"device": device_id})
        return token, "ok"

    # -- sessions -----------------------------------------------------------

    def live_session(self, token: Optional[str]) -> Optional[_Session]:
        """The session, if it is live, still belongs to a device that is still paired."""
        if not token:
            return None
        session = self._sessions.get(str(token))
        if session is None:
            return None
        if session.expires_at < self.clock():
            self._sessions.pop(str(token), None)
            return None
        if self.secret_for(session.device_id) is None:
            # Revoked, or its key was forgotten: the session dies with it, in the same request.
            self._sessions.pop(str(token), None)
            log.info("relay session dropped with its device",
                     extra_fields={"device": session.device_id})
            return None
        return session

    def signout(self, token: Optional[str]) -> bool:
        return self._sessions.pop(str(token or ""), None) is not None

    def _forget_stale(self) -> None:
        now = self.clock()
        for code in [c for c, rec in self._challenges.items() if rec.expires_at < now]:
            self._challenges.pop(code, None)

    # -- the actual forwarding ---------------------------------------------

    @staticmethod
    def allows(path: str) -> bool:
        if path in RELAY_ALLOWED:
            return True
        return any(path.startswith(prefix) for prefix in RELAY_ALLOWED_PREFIXES)

    def forward(self, *, token: Optional[str], method: str, path: str, query: str,
                headers: Dict[str, str], body: bytes) -> Tuple[int, bytes, str]:
        """One attempt, one bounded deadline, the host's answer returned unchanged.

        Returning the host's own status and bytes is the design: a verification of UNKNOWN, a
        refusal, or a pending confirmation all look exactly the way they do on the laptop, because
        they *are* the laptop's answers. The relay has no vocabulary of its own beyond the two
        outage codes, and inventing one would be the moment it became a second authority.
        """
        session = self.live_session(token)
        if session is None:
            return 401, _json_bytes({"relay_error": "session_invalid",
                                     "detail": "No live relay session. Authenticate again."}), ""
        # Strip before deciding: "/relay//", "/relay/" and "/relay" all carry no host path, and
        # answering "I refuse to forward that" about a path that does not exist would be the
        # relay guessing at meaning it was not given.
        target_path = f"/{str(path).strip('/')}"
        if target_path == "/":
            return 404, _json_bytes({"relay_error": "no_path",
                                     "detail": "Nothing to forward: the path is empty."}), ""
        if not self.allows(target_path):
            return 403, _json_bytes({
                "relay_error": "path_not_allowed",
                "detail": f"The relay will not carry {target_path}. It forwards a short "
                          "list of paths and refuses everything else.",
            }), ""
        credential = _host_credential(headers)
        if not credential:
            return 401, _json_bytes({
                "relay_error": "no_host_credential",
                "detail": "The relay carries the client's own host credential and will not "
                          "substitute one of its own.",
            }), ""

        target = f"{self.host_base.rstrip('/')}{target_path}"
        if query:
            target = f"{target}?{query}"
        # Canonical casing on the way out. HTTP header names are case-insensitive, so this
        # changes nothing to the host -- but "the client's own credential is forwarded, and
        # nothing else of its headers" is a claim worth being able to read off the wire.
        outbound = {"Content-Type": "application/json", "X-JARVIS-Token": credential}
        payload = _attribute(body, session.device_id)

        try:
            status, raw = self.sender(method=method.upper(), url=target, headers=outbound,
                                      content=payload, timeout=self.timeout)
        except RelayTimeout as exc:
            return 504, _json_bytes({"relay_error": "host_timeout", "detail": str(exc)}), "host_timeout"
        except RelayUnavailable as exc:
            return 502, _json_bytes({"relay_error": "host_unavailable",
                                     "detail": str(exc)}), "host_unavailable"
        except Exception as exc:  # a transport surprise is still an outage, never an empty answer
            # A transport error's text is not under this code's control: an
            # httpx/h11 exception can carry the request URL, and this service
            # forwards a credential on every call. Redact before it reaches the
            # log or the response. `redact` catches key=value, Bearer and
            # standalone credential shapes; a bare token with no surrounding
            # context is not a shape any redactor can recognise, which is why
            # this is a floor rather than a guarantee.
            detail = redact(str(exc))
            log.warning("relay forward failed", extra_fields={"error": detail})
            return 502, _json_bytes({"relay_error": "host_unavailable",
                                     "detail": f"The laptop could not be reached: {detail}"}), "host_unavailable"
        return int(status), bytes(raw), ""

    # -- what an operator may be told --------------------------------------

    def describe(self) -> Dict[str, Any]:
        """A status view with no credential material in it.

        Counts, never tokens: a route that listed sessions would be a place where a live
        credential can be read off a screen, which is exactly what Phase 10 was about.
        """
        self._forget_stale()
        now = self.clock()
        live = sum(1 for s in self._sessions.values() if s.expires_at >= now
                   and self.secret_for(s.device_id) is not None)
        return {
            "host_base": self.host_base,
            "live_sessions": live,
            "allowed_paths": sorted(RELAY_ALLOWED) + [p + "*" for p in RELAY_ALLOWED_PREFIXES],
            "timeout": self.timeout,
            "session_ttl": self.session_ttl,
            "surface": self.surface,
        }

    # -- the real transport -------------------------------------------------

    def _httpx_sender(self, *, method: str, url: str, headers: Dict[str, str],
                      content: bytes, timeout: float) -> Tuple[int, bytes]:
        """The only place the relay talks to anything, kept single so the timeout is stated once."""
        import httpx

        try:
            with httpx.Client(timeout=timeout) as client:
                reply = client.request(method, url, headers=headers, content=content)
        except httpx.TimeoutException as exc:
            raise RelayTimeout(f"the laptop did not answer within {timeout}s — {exc}") from exc
        except httpx.HTTPError as exc:
            raise RelayUnavailable(f"the laptop at {url} is not answering: {exc}") from exc
        return reply.status_code, reply.content


def _json_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _host_credential(headers: Dict[str, str]) -> str:
    for name, value in headers.items():
        lowered = name.lower()
        if lowered == "x-jarvis-token" and str(value).strip():
            return str(value).strip()
        if lowered == "authorization" and str(value).lower().startswith("bearer "):
            return str(value)[7:].strip()
    return ""


def _attribute(body: bytes, device_id: str) -> bytes:
    """Stamp the device that signed in, rather than the one that was typed.

    A body that is not a JSON object is forwarded untouched — the relay is not going to rewrite
    something it cannot understand, and the host will refuse what it cannot parse.
    """
    if not body:
        return body
    try:
        parsed = json.loads(body)
    except ValueError:
        return body
    if not isinstance(parsed, dict):
        return body
    typed = str(parsed.get("device_id") or "").strip()
    if typed and typed != device_id:
        log.info("relay corrected the claimed device",
                 extra_fields={"claimed": typed, "authenticated": device_id})
    parsed["device_id"] = device_id
    parsed.setdefault("source", "relay")
    return json.dumps(parsed).encode("utf-8")


# ---------------------------------------------------------------------------
# the HTTP surface
# ---------------------------------------------------------------------------


def build_relay_app(service: RelayService):
    """The relay as an ASGI app, so it can be run standalone and tested in one line."""
    app = FastAPI(title="Vantage relay", docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/relay/challenge")
    async def challenge(payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
        device_id = str((payload or {}).get("device_id") or "").strip()
        issued = service.new_challenge(device_id)
        if issued is None:
            # One answer for "no such device" and "not approved yet": which pairings exist is the
            # laptop's business, and a caller should not be able to enumerate it here.
            return JSONResponse({"relay_error": "device_not_paired",
                                 "detail": "This device is not paired with that laptop."},
                                status_code=403)
        code, now = issued
        return JSONResponse({"challenge": code, "now": now})

    @app.post("/relay/session")
    async def session(payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
        fields = payload or {}
        token, reason = service.redeem(
            device_id=str(fields.get("device_id") or ""),
            challenge=str(fields.get("challenge") or ""),
            proof=str(fields.get("proof") or ""),
            timestamp=float(fields.get("timestamp") or 0.0),
        )
        if token is None:
            log.info("relay authentication refused", extra_fields={"reason": reason})
            return JSONResponse({"relay_error": "authentication_failed", "detail": reason},
                                status_code=401)
        return JSONResponse({"relay_session": token, "device_id": str(fields.get("device_id")),
                             "expires_in": int(service.session_ttl)})

    @app.delete("/relay/session")
    async def signout(request: Request) -> JSONResponse:
        return JSONResponse({"revoked": service.signout(request.headers.get(SESSION_HEADER))})

    @app.api_route("/relay/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(request: Request, path: str) -> Response:
        body = await request.body()
        # Starlette hands back header names lowercased, so an exact-case lookup for
        # ``X-Relay-Session`` silently finds nothing and every request arrives anonymous.
        headers = {name.lower(): value for name, value in request.headers.items()}
        status, raw, _code = service.forward(
            token=headers.get(SESSION_HEADER.lower()), method=request.method, path=path,
            query=request.url.query, headers=headers, body=body)
        return Response(content=raw, status_code=status, media_type="application/json")

    return app


def main(argv: Optional[List[str]] = None) -> int:
    """Run the relay on its own. It holds no laptop credential to lose."""
    import argparse

    parser = argparse.ArgumentParser(description="Vantage authenticated relay")
    parser.add_argument("--host", default="http://127.0.0.1:8765",
                        help="the Vantage host to forward to")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    arguments = parser.parse_args(argv)

    import uvicorn

    from jarvis.devices.pairing import PairingManager

    service = RelayService(host_base=arguments.host, pairing=PairingManager())
    uvicorn.run(build_relay_app(service), host=arguments.bind, port=int(arguments.port),
                log_level="warning")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
