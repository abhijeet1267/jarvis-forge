"""Google OAuth: the connection lifecycle, and nothing else.

This module owns one question: *is there a usable Google token right now, and
if not, why not?* Calendar and Tasks adapters ask it for an access token and
never touch the refresh token themselves.

Three boundaries are deliberate:

* **The refresh token never lives in settings.** Settings are read into logs,
  diagnostics and `/health` payloads; a refresh token is a long-lived
  credential and goes to the OS keychain through the project's existing
  ``KeychainStorage`` wrapper, which already handles the platform fallback.
* **A saved token is not a connected account.** "Connected" is only reported
  when a live call with the token succeeded. A stored-but-revoked token reads
  as ``expired`` with the reason, not as connected -- the failure mode this
  project has hit repeatedly is a boolean that answers a different question
  than the reader assumes.
* **Nothing here invents a client id.** Without configuration every entry
  point says ``not_configured`` and names exactly what is missing, so a
  missing Google Cloud project cannot look like a broken integration.

The flow implemented is OAuth 2.0 for a **desktop/public client** with PKCE:
the authorization-code exchange happens here (server-side, in the local
backend), and no client secret is required or embedded. A loopback redirect is
used, which is the flow Google specifies for installed apps.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import urllib.parse
import urllib.error
import urllib.request

from ..config import Settings, get_settings
from ..net import http_read
from ..logging_setup import get_logger
from ..security.providers import KeychainStorage

log = get_logger("google_auth")

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

#: Read-only calendar, and read+write tasks (tasks are the user's own to-do
#: list; creating one is the whole point of the integration). Calendar write is
#: deliberately absent until event editing is implemented, so we never ask for
#: a permission we do not use.
SCOPE_CALENDAR_READ = "https://www.googleapis.com/auth/calendar.readonly"
SCOPE_TASKS = "https://www.googleapis.com/auth/tasks"

#: Keychain service name for the stored token bundle.
TOKEN_SERVICE = "google.oauth"
TOKEN_ACCOUNT = "refresh_token"

#: A state value older than this is refused: the person took too long, and the
#: pending authorization is stale.
STATE_TTL_SECONDS = 600

class ConnectionState(str, Enum):
    """What the connection actually is, verified rather than saved."""

    NOT_CONFIGURED = "not_configured"   # no client id in settings
    DISCONNECTED = "disconnected"        # configured, but never authorised
    CONNECTED = "connected"              # a live call with the token worked
    EXPIRED = "expired"                  # stored, but Google refused it
    ERROR = "error"                      # Google or the network failed

@dataclass
class ConnectionStatus:
    state: ConnectionState
    detail: str = ""
    account: str = ""
    scopes: List[str] = field(default_factory=list)
    configured: bool = False
    has_token: bool = False

    @property
    def usable(self) -> bool:
        return self.state is ConnectionState.CONNECTED

    def as_dict(self) -> Dict[str, Any]:
        # Never include the token itself, or anything derived from it.
        return {
            "state": self.state.value,
            "detail": self.detail,
            "account": self.account,
            "scopes": list(self.scopes),
            "configured": self.configured,
            "has_token": self.has_token,
            "usable": self.usable,
        }

class GoogleAuthError(RuntimeError):
    """Raised with a message safe to show a person.

    ``code`` is Google's machine-readable error code when there is one
    (``invalid_grant``, ``invalid_client``, ...). Callers branch on it rather
    than searching the sentence, which would otherwise also match an error
    *description* that merely mentions the word.
    """

    def __init__(self, message: str, state: ConnectionState = ConnectionState.ERROR,
                 code: str = "") -> None:
        super().__init__(message)
        self.state = state
        self.code = code

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")

def scopes_for(settings: Settings) -> List[str]:
    scopes: List[str] = []
    if settings.google_calendar_enabled:
        scopes.append(SCOPE_CALENDAR_READ)
    if settings.google_tasks_enabled:
        scopes.append(SCOPE_TASKS)
    return scopes

class GoogleAuth:
    """Token lifecycle for one Google account.

    Deliberately not a singleton: callers pass settings, which keeps tests
    independent and lets a future multi-account version thread an id through
    without rewriting the adapters.
    """

    #: Total wall-clock ceiling for one Google HTTP exchange.
    TIMEOUT = 20.0

    #: The authorization in flight, shared by the whole process.
    #:
    #: This must NOT be per-instance. `begin` runs in one HTTP request and
    #: `complete` in another, and the routes construct a fresh GoogleAuth for
    #: each -- so an instance attribute was always empty by the time the
    #: callback arrived, and every real connect failed with "There is no
    #: authorization in progress". Measured on a live server before this was
    #: fixed. It is a class attribute because the value genuinely belongs to the
    #: process, not to the object that happened to start the flow.
    #:
    #: **Deployment constraint: exactly one backend process.** The supported
    #: launches all run a single uvicorn process -- ``run.py serve`` (no
    #: workers), the macOS panel's ``uvicorn jarvis.api.app:app`` (no
    #: ``--workers``) and ``app/launcher.py`` (spawns ``run.py``). Because the
    #: value lives in this process's memory, starting uvicorn with
    #: ``--workers N > 1`` would let a callback land on a worker that never saw
    #: the ``begin`` and refuse it with "There is no authorization in progress".
    #: Do not enable multiple workers without first persisting this state
    #: (0600 file with TTL under ``settings.home``); no supported path does.
    #:
    #: Single-use and short-lived: consume it on completion, and refuse it once
    #: STATE_TTL_SECONDS have passed.
    _pending: Optional[Dict[str, Any]] = None

    #: Serialises the read-check-consume sequence on ``_pending``.
    #:
    #: The routes now run their blocking exchanges off the event loop (see
    #: app.py), so ``begin``/``complete`` can genuinely run on different threads
    #: at once. Without a lock, two completions could both observe the same
    #: pending value before either consumed it, redeeming one state twice; and a
    #: ``complete`` could consume a value a concurrent ``begin`` had just
    #: replaced, pairing the newer verifier with an older code. The lock covers
    #: only the in-memory state transition -- never the network call, which must
    #: stay outside it.
    _pending_lock = threading.Lock()

    #: Cache stored on the *class*, not the instance.
    #:
    #: The routes and the adapters each build a fresh GoogleAuth per call, so an
    #: instance cache was empty on every call and every read paid for a token
    #: refresh -- and, worse, an instance that had cached a token could keep
    #: returning it after another object disconnected. Both were measured
    #: against the shared process-wide design of `_pending`; the cache has the
    #: same lifetime. Because routes now run these reads on worker threads, the
    #: cache is guarded by `_token_lock`: re-entrant (the refresh path caches
    #: while holding it) and held across the refresh itself, so two concurrent
    #: reads wait for one round trip rather than stampeding Google with two.
    _cached_token: Optional[str] = None
    _cached_expiry: float = 0.0
    _token_lock = threading.RLock()

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        # The pending authorization and the access-token cache are both
        # process-wide class state (see above); nothing token-bearing lives on
        # this object, so a caller that builds a fresh GoogleAuth per request
        # still shares the cache rather than refreshing on every read.

    # -- configuration ----------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self.settings.google_client_id.strip())

    def client_id(self) -> str:
        return self.settings.google_client_id.strip()

    # -- stored credential -------------------------------------------------

    def store_refresh_token(self, token: str) -> bool:
        if not token:
            return False
        ok = KeychainStorage.store(TOKEN_SERVICE, token)
        if ok:
            with GoogleAuth._token_lock:
                GoogleAuth._cached_token = None
                GoogleAuth._cached_expiry = 0.0
        return ok

    def stored_refresh_token(self) -> Optional[str]:
        return KeychainStorage.retrieve(TOKEN_SERVICE)

    def forget(self) -> bool:
        """Remove the stored credential. True if something was there."""
        # Clear the cache *before* deleting, so no concurrent reader can be
        # handed the cached access token on the far side of the disconnect.
        with GoogleAuth._token_lock:
            GoogleAuth._cached_token = None
            GoogleAuth._cached_expiry = 0.0
        return KeychainStorage.delete(TOKEN_SERVICE)

    # -- authorization -----------------------------------------------------

    def begin(self, redirect_uri: str) -> Dict[str, str]:
        """Start an authorization. Returns the URL to open and the state.

        PKCE is used even though this is a server-side exchange: it costs
        nothing and binds the code to this request, so an intercepted
        authorization code cannot be redeemed elsewhere.
        """
        if not self.configured:
            raise GoogleAuthError(
                "Google is not configured. Set JARVIS_GOOGLE_CLIENT_ID to the "
                "OAuth client id from your Google Cloud project.",
                ConnectionState.NOT_CONFIGURED,
            )
        verifier = _b64url(secrets.token_bytes(32))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state = secrets.token_urlsafe(24)
        # Under the lock so a concurrent ``complete`` cannot pair this attempt's
        # new verifier with an older callback (or clear it after validating).
        with GoogleAuth._pending_lock:
            GoogleAuth._pending = {
                "state": state,
                "verifier": verifier,
                "redirect_uri": redirect_uri,
                "created": time.time(),
            }
        params = {
            "client_id": self.client_id(),
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes_for(self.settings)),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",   # needed for a refresh token
            "prompt": "consent",        # otherwise a re-connect may omit the token
        }
        return {
            "url": AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params),
            "state": state,
        }

    def complete(self, code: str, state: str, redirect_uri: str = "") -> ConnectionStatus:
        """Finish an authorization. Returns the *verified* status."""
        # Check-and-consume under the lock, so two callbacks racing on the same
        # state cannot both pass the check and redeem it, and a ``begin`` on
        # another thread cannot swap the verifier between validation and use.
        # The network exchange stays outside the lock: it is slow, and holding
        # the lock across it would serialise every connect behind one timeout.
        with GoogleAuth._pending_lock:
            pending = GoogleAuth._pending
            if not pending:
                raise GoogleAuthError(
                    "There is no authorization in progress. Start the connection again.",
                    ConnectionState.DISCONNECTED,
                )
            if time.time() - pending["created"] > STATE_TTL_SECONDS:
                GoogleAuth._pending = None
                raise GoogleAuthError(
                    "The authorization took too long and is no longer valid. Start again.",
                    ConnectionState.DISCONNECTED,
                )
            if not secrets.compare_digest(state or "", pending["state"]):
                # A mismatch can be a forgery attempt *or* an ordinary second
                # attempt: a new tab, a double-clicked Connect, or a retry after a
                # failure all start a fresh authorization, and only the newest one
                # is held. Do NOT clear the pending value here -- the newer attempt
                # is still in flight, and dropping it would break a connect the
                # person is about to finish in their current tab.
                raise GoogleAuthError(
                    "This link belongs to an earlier connection attempt, which was "
                    "replaced by a newer one. Finish connecting in the most recent tab, "
                    "or start again from Settings. Nothing was connected by this link.",
                    ConnectionState.DISCONNECTED,
                )

            payload = {
                "client_id": self.client_id(),
                "code": code,
                "code_verifier": pending["verifier"],
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri or pending["redirect_uri"],
            }
            secret = self.settings.google_client_secret.strip()
            if secret:
                # A "Web application" client does have a secret. Accepted when
                # present, never required: the desktop client type does not.
                payload["client_secret"] = secret

            # Consume the pending authorization *before* the network call. If the
            # exchange fails, the same state must not be redeemable again: an
            # authorization code is single-use at Google's end, but a retry loop
            # here would keep re-presenting it, and a leaked state would stay
            # usable for its whole TTL. Marking it used first makes failure final,
            # which is what "single use" has to mean.
            GoogleAuth._pending = None
        data = self._post_token(payload)
        refresh = data.get("refresh_token")
        if not refresh:
            raise GoogleAuthError(
                "Google did not return a refresh token, so the connection could not "
                "be kept. Remove the app's access at myaccount.google.com and connect "
                "again.",
                ConnectionState.DISCONNECTED,
            )
        if not self.store_refresh_token(refresh):
            raise GoogleAuthError(
                "Google authorised the request, but the token could not be stored in "
                "the system keychain, so nothing was saved. Either the login keychain "
                "is locked, or this machine has no keychain backend available. Vantage "
                "will not write a long-lived credential to a plain file, so connect "
                "again once the keychain is usable.",
                ConnectionState.ERROR,
            )
        self._cache_access_token(data)
        return self.status(verify_with=data.get("access_token"))

    def _read(self, method: str, url: str, *, params=None, data=None,
              json_body=None, headers=None, form: bool = False):
        """One HTTP exchange, bounded by a *total* deadline.

        Returns ``(status, body_bytes)``. **An HTTP error is a response here,
        not an exception.** ``http_read`` wraps ``urlopen``, which raises
        ``HTTPError`` for 4xx/5xx -- but "Google said 401" is a fact about the
        connection that callers must branch on, and letting it surface as a
        transport failure made every revoked token read as "could not reach
        Google". The error's own body and status are recovered and returned.

        This project's rule (jarvis/net.py) is that every network read carries a
        wall-clock ceiling, so a server that trickles bytes cannot hold a turn
        open. ``http_read`` enforces that.
        """
        headers = dict(headers or {})
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        body = None
        if data is not None or json_body is not None:
            payload = json_body if json_body is not None else data
            if form:
                body = urllib.parse.urlencode(payload).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            elif isinstance(payload, (bytes, str)):
                body = payload.encode() if isinstance(payload, str) else payload
                headers.setdefault("Content-Type", "application/json")
            else:
                body = json.dumps(payload).encode()
                headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            raw = http_read(request, total=self.TIMEOUT, what="Google")
            return 200, raw
        except urllib.error.HTTPError as exc:
            # A refusal is an answer. Keep the status and whatever body Google
            # sent, so the caller can say *why* rather than "unreachable".
            try:
                return exc.code, exc.read() or b""
            finally:
                exc.close()

    def _post_token(self, payload: Dict[str, str]) -> Dict[str, Any]:
        try:
            status, raw = self._read("POST", TOKEN_ENDPOINT, data=payload, form=True)
        except Exception as exc:                       # network, DNS, timeout
            raise GoogleAuthError(
                f"Could not reach Google to complete the connection ({type(exc).__name__}).",
                ConnectionState.ERROR,
            ) from exc
        try:
            body = json.loads(raw or b"{}")
        except Exception as exc:
            raise GoogleAuthError(
                "Google's token response was not readable JSON.", ConnectionState.ERROR
            ) from exc
        # Google signals a refusal in the body, with HTTP 200 in some cases, so
        # the presence of "error" is what decides, not the status code.
        if status >= 400 or (isinstance(body, dict) and body.get("error")):
            # Google's error body is short and safe; the code is the useful part.
            raw_code = body.get("error") if isinstance(body, dict) else ""
            code = (raw_code.get("status") if isinstance(raw_code, dict) else raw_code)
            code = str(code or f"HTTP {status}")
            error_description = (body.get("error_description") if isinstance(body, dict) else "") or ""
            hint = {
                "invalid_grant": "the authorization was already used or has expired",
                "invalid_client": "the client id does not match this Google project",
                "redirect_uri_mismatch": "the redirect URI is not registered for this client",
            }.get(code, "Google refused the request")
            extra = f" ({error_description[:120]})" if error_description else ""
            raise GoogleAuthError(
                f"Google refused the token exchange: {code} — {hint}{extra}.",
                ConnectionState.ERROR,
                code=code,
            )
        return body

    # -- access tokens -----------------------------------------------------

    def _cache_access_token(self, data: Dict[str, Any]) -> None:
        token = data.get("access_token")
        if not token:
            return
        expires_in = float(data.get("expires_in") or 0)
        # Refresh a minute early so a token does not expire mid-request.
        expiry = time.time() + max(expires_in - 60, 0)
        with GoogleAuth._token_lock:
            GoogleAuth._cached_token = token
            GoogleAuth._cached_expiry = expiry

    def access_token(self) -> str:
        """A usable access token, refreshing if needed. Raises with a reason."""
        if not self.configured:
            raise GoogleAuthError(
                "Google is not configured (JARVIS_GOOGLE_CLIENT_ID is empty).",
                ConnectionState.NOT_CONFIGURED,
            )
        # Hold the lock across the refresh, not merely the cache read. Two
        # requests arriving on an empty cache would otherwise both refresh;
        # the second now waits and finds the first's token. The second check
        # after acquiring is what makes that true.
        with GoogleAuth._token_lock:
            if GoogleAuth._cached_token and time.time() < GoogleAuth._cached_expiry:
                return GoogleAuth._cached_token

            refresh = self.stored_refresh_token()
            if not refresh:
                raise GoogleAuthError(
                    "No Google account is connected. Connect one in Settings → Connections.",
                    ConnectionState.DISCONNECTED,
                )
            payload = {
                "client_id": self.client_id(),
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            }
            secret = self.settings.google_client_secret.strip()
            if secret:
                payload["client_secret"] = secret
            try:
                data = self._post_token(payload)
            except GoogleAuthError as exc:
                # invalid_grant here means the refresh token was revoked or the
                # password changed -- a real state change, not a transient error.
                # The stored token is deliberately NOT deleted: it is the only
                # record that an account was ever connected, and removing it
                # silently would turn "expired, reconnect" into "never
                # connected". The message names the fix, and disconnect remains
                # the explicit way to remove it.
                if getattr(exc, "code", "") == "invalid_grant" or "invalid_grant" in str(exc):
                    raise GoogleAuthError(
                        "Google no longer accepts the saved access — it was revoked or "
                        "expired. Reconnect in Settings → Connections.",
                        ConnectionState.EXPIRED,
                        code="invalid_grant",
                    ) from exc
                raise
            self._cache_access_token(data)
            if not GoogleAuth._cached_token:
                raise GoogleAuthError("Google returned no access token.",
                                      ConnectionState.ERROR)
            return GoogleAuth._cached_token

    # -- status ------------------------------------------------------------

    def status(self, verify_with: Optional[str] = None) -> ConnectionStatus:
        """The connection's real state.

        "Connected" is only reported when a live call succeeded. ``verify_with``
        lets a caller that already holds a fresh access token skip a round trip.
        """
        configured = self.configured
        has_token = self.stored_refresh_token() is not None
        if not configured:
            # A stored token with no client id is its own problem, and a distinct
            # one: the person *did* authorise once, and the setup needed is a
            # client id to spend that token with. Reporting "no account connected"
            # (has_token False) both hid the credential and pointed at the wrong fix.
            detail = (
                "A Google account was authorised earlier, but no OAuth client id is "
                "configured now, so that credential cannot be used. Set "
                "JARVIS_GOOGLE_CLIENT_ID to the client id the account was authorised "
                "against. You can clear the stale credential with Disconnect."
                if has_token else
                "Google is not configured. Set JARVIS_GOOGLE_CLIENT_ID to the OAuth "
                "client id from your Google Cloud project (see Connections for the steps).")
            return ConnectionStatus(
                ConnectionState.NOT_CONFIGURED,
                detail=detail,
                configured=False, has_token=has_token,
                scopes=scopes_for(self.settings),
            )

        if not has_token:
            return ConnectionStatus(
                ConnectionState.DISCONNECTED,
                detail="No Google account is connected.",
                configured=True, has_token=False,
                scopes=scopes_for(self.settings),
            )
        try:
            token = verify_with or self.access_token()
            account, scopes = self._verify(token)
        except GoogleAuthError as exc:
            return ConnectionStatus(
                exc.state,
                detail=str(exc),
                configured=True, has_token=True,
                scopes=scopes_for(self.settings),
            )
        return ConnectionStatus(
            ConnectionState.CONNECTED,
            detail="Connected to Google.",
            account=account, scopes=scopes or scopes_for(self.settings),
            configured=True, has_token=True,
        )

    def _verify(self, access_token: str) -> tuple[str, List[str]]:
        """Ask Google who this token is, verifying it is live.

        The tokeninfo endpoint is the cheapest real call: a revoked token
        returns 400, so this distinguishes "saved" from "working".
        """
        try:
            status, raw = self._read(
                "GET", "https://oauth2.googleapis.com/tokeninfo",
                params={"access_token": access_token},
            )
            body = json.loads(raw or b"{}") if status < 400 else {}
        except Exception as exc:
            raise GoogleAuthError(
                f"Could not verify the Google connection ({type(exc).__name__}).",
                ConnectionState.ERROR,
            ) from exc
        if status == 400:
            # Google rejected this access token. Drop it from the cache so the
            # next read refreshes instead of re-presenting a token Google has
            # already refused -- otherwise a single bad token would keep every
            # read failing until its nominal expiry.
            with GoogleAuth._token_lock:
                GoogleAuth._cached_token = None
                GoogleAuth._cached_expiry = 0.0
            raise GoogleAuthError(
                "Google rejected the saved access — it was revoked or expired. "
                "Reconnect in Settings → Connections.",
                ConnectionState.EXPIRED,
            )
        if status >= 400:
            raise GoogleAuthError(
                f"Google could not confirm the connection (HTTP {status}).",
                ConnectionState.ERROR,
            )
        scopes = str(body.get("scope") or "").split()
        return str(body.get("email") or ""), scopes

    def disconnect(self) -> Dict[str, Any]:
        """Forget the local token, and ask Google to revoke it where possible."""
        token = self.stored_refresh_token()
        revoked: Optional[bool] = None
        if token:
            try:
                status, raw = self._read("POST", REVOKE_ENDPOINT, data={"token": token},
                                         form=True)
                # Google answers 200 for a live token it revoked, and 400
                # ``invalid_token`` for one that is already dead. Both mean the
                # access is gone from Google's side; only the first used to be
                # counted, so a token someone had already revoked from
                # myaccount.google.com was reported as "Google could not be
                # reached" -- an unreachable-server claim about a reachable one.
                revoked = status < 400 or (
                    status == 400 and b"invalid_token" in (raw or b"").lower())
            except Exception:
                # The local credential is removed regardless: leaving a token
                # the person asked to disconnect would be worse than a remote
                # revoke that could not be confirmed.
                revoked = False
        removed = self.forget()
        detail = ("Disconnected, and Google was asked to revoke the access."
                  if revoked else
                  "Disconnected on this Mac." if removed else
                  "There was no stored Google connection.")
        if revoked is False:
            detail += (" Google could not be reached to revoke it remotely; remove access "
                       "at myaccount.google.com if you want it gone everywhere.")
        return {"removed": removed, "revoked": revoked, "detail": detail}

def describe_google(settings: Optional[Settings] = None) -> Dict[str, Any]:
    """Setup requirements, without leaking anything secret."""
    settings = settings or get_settings()
    configured = bool(settings.google_client_id.strip())
    return {
        "configured": configured,
        "calendar_enabled": settings.google_calendar_enabled,
        "tasks_enabled": settings.google_tasks_enabled,
        "scopes": scopes_for(settings),
        "setup": (
            []
            if configured
            else [
                "Create a project at console.cloud.google.com",
                "Enable the Google Calendar API and the Google Tasks API",
                "Create an OAuth client of type 'Desktop app'",
                "Set JARVIS_GOOGLE_CLIENT_ID to its client id",
            ]
        ),
        "redirect_uri": "http://127.0.0.1:8765/google/callback",
    }