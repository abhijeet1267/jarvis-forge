"""API authentication, host validation and rate limiting (sections 25, 26).

A localhost HTTP API that can launch apps, move files and delete things is
reachable by *any web page the user happens to have open*. Without these
controls a single malicious page could drive JARVIS from the browser.

Three layers, each independent:

1. **Token auth** -- a per-install secret in a header. A custom header also
   forces a CORS preflight, which a foreign origin cannot satisfy.
2. **Host allowlist** -- rejects requests whose ``Host`` header is not a
   loopback name, defeating DNS rebinding.
3. **Rate limiting** -- a token bucket per client address, so a runaway client
   cannot hammer the pipeline.
"""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..config import Settings, get_settings
from ..logging_setup import get_logger

log = get_logger("security.auth")

TOKEN_HEADER = "X-JARVIS-Token"

# Host header values that are unambiguously local.
LOOPBACK_HOSTS = {
    "127.0.0.1",
    "localhost",
    "::1",
    "[::1]",
    "0.0.0.0",  # only ever a bind address, but harmless to accept
}


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------
class TokenStore:
    """Loads or creates the per-install API token."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self._token: Optional[str] = None

    @property
    def path(self) -> Path:
        return self.settings.token_path

    def token(self) -> str:
        """Return the API token, generating and persisting one if needed."""
        if self._token:
            return self._token

        # An explicitly configured token wins (useful for tests and containers).
        configured = (self.settings.api_token or "").strip()
        if configured:
            self._token = configured
            return self._token

        existing = self._read_existing()
        if existing:
            self._token = existing
            return self._token

        self._token = secrets.token_urlsafe(32)
        self._write(self._token)
        log.info("api token generated", extra_fields={"path": str(self.path)})
        return self._token

    def _read_existing(self) -> Optional[str]:
        try:
            if self.path.exists():
                value = self.path.read_text(encoding="utf-8").strip()
                return value or None
        except OSError as exc:
            log.info("could not read api token", extra_fields={"error": str(exc)})
        return None

    def _write(self, token: str) -> None:
        """Persist the token with owner-only permissions."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Create with 0600 from the outset, then write, so the secret is
            # never briefly world-readable.
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(token + "\n")
            os.chmod(self.path, 0o600)
        except OSError as exc:
            log.info("could not persist api token", extra_fields={"error": str(exc)})

    def verify(self, presented: Optional[str]) -> bool:
        """Constant-time comparison of a presented token."""
        if not presented:
            return False
        return hmac.compare_digest(presented.strip(), self.token())


# ---------------------------------------------------------------------------
# Host header validation (anti DNS-rebinding)
# ---------------------------------------------------------------------------
def interface_addresses() -> set:
    """Every IP address this machine currently owns.

    Used by LAN mode. It is the narrowest set that still lets a phone in: the request
    must name an address this host actually has, which is what keeps the DNS-rebinding
    defence intact when the check stops being "loopback only".
    """
    import socket

    found = {"127.0.0.1", "::1", "localhost"}
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6):
            found.add(info[4][0])
    except OSError:
        pass
    return found


#: RFC 6762 reserves this suffix for multicast DNS, and public DNS never answers for it.
#: That is the only reason a hostname can be trusted by the LAN gate at all.
MDNS_SUFFIX = ".local"

#: How long a name answer stands. A lease renewal changes the address, and a "no" that
#: never expires would lock out a network that has since been fixed.
MDNS_TTL_SECONDS = 60.0

#: name -> (monotonic time it was answered, whether it was ours)
_mdns_cache: dict = {}


def _now() -> float:
    """Indirection so the expiry rule can be tested without stopping the real clock."""
    return time.monotonic()


def _resolve_mdns(name: str) -> List[str]:
    """Every address the OS resolver has for ``name``; empty when it has none."""
    import socket

    try:
        return [info[4][0] for info in socket.getaddrinfo(name, None)]
    except OSError:
        return []


def lan_startup_problems(settings: Optional[Settings] = None) -> List[str]:
    """What is wrong with starting in this configuration, in plain sentences.

    Empty means it is safe to start. LAN mode is the one setting that turns a laptop
    into something other devices can talk to, so it is checked rather than assumed.
    """
    settings = settings or get_settings()
    if not settings.lan_mode:
        return []
    problems: List[str] = []
    if not settings.auth_enabled:
        problems.append(
            "LAN mode needs the API token: with authentication off, any device on this "
            "network could run commands on the laptop. Turn authentication on (the token "
            "is generated and stored on first start), or leave the door shut.")
    if str(settings.host) in ("127.0.0.1", "localhost", "::1"):
        problems.append(
            "LAN mode is on but the server binds to "
            f"{settings.host}, which no other device can reach. Set JARVIS_HOST=0.0.0.0 "
            "to bind every interface, or a specific address, and mean it. (The desktop "
            "app's 'Allow devices on my wifi' checkbox sets both.)")
    return problems


def mdns_name_is_ours(name: str, *, ttl: float = MDNS_TTL_SECONDS) -> bool:
    """True when ``name`` is a ``.local`` name pointing at an address this machine owns.

    Two conditions, because one is not enough. ``.local`` is reserved for multicast DNS
    and cannot be answered by public DNS, so an attacker cannot register it and point it
    here the way ``evil.example.com`` can be pointed at 127.0.0.1 -- that half closes the
    rebinding door the loopback rule was written for. And the name must still resolve to
    an address in :func:`interface_addresses`, because a stranger's laptop on the same
    wifi has a ``.local`` name too.

    The answer is cached per name: this runs on every request, and a resolver call per
    request is a slow path any device on the network can drive.
    """
    if not name.lower().endswith(MDNS_SUFFIX):
        return False
    key = name.lower()
    now = _now()
    hit = _mdns_cache.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    answers = set(_resolve_mdns(name))
    ours = bool(answers) and bool(answers & interface_addresses())
    _mdns_cache[key] = (now, ours)
    return ours


def host_allowed(host_header: Optional[str], settings: Optional[Settings] = None) -> bool:
    """True when the Host header names a loopback address.

    A browser can be tricked into resolving an attacker-controlled hostname to
    127.0.0.1. The request then arrives with ``Host: evil.example.com``. Checking
    the header closes that door.
    """
    settings = settings or get_settings()
    if not settings.enforce_host_header:
        return True
    if not host_header:
        return False

    # Strip the port, handling IPv6 literals in brackets.
    host = host_header.strip()
    if host.startswith("["):
        host = host.split("]")[0] + "]"
    elif ":" in host:
        host = host.rsplit(":", 1)[0]

    if host.lower() in LOOPBACK_HOSTS:
        return True
    # LAN mode: the phone's request carries this laptop's own address, so accept
    # exactly the addresses this machine owns, plus its mDNS name when that name
    # resolves to one of them. A public hostname is still never accepted -- that is
    # the DNS-rebinding case the loopback rule was written for. See `mdns_name_is_ours`.
    if settings.lan_mode:
        stripped = host.strip("[]")
        return stripped in interface_addresses() or mdns_name_is_ours(stripped)
    return False


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
@dataclass
class _Bucket:
    tokens: float
    updated: float = field(default_factory=time.monotonic)


class RateLimiter:
    """A token bucket per client key."""

    def __init__(self, per_minute: int = 120, burst: int = 30) -> None:
        self.per_minute = max(1, per_minute)
        self.burst = max(1, burst)
        self._buckets: Dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    @property
    def _rate_per_second(self) -> float:
        return self.per_minute / 60.0

    def check(self, key: str, cost: float = 1.0) -> Tuple[bool, float]:
        """Consume ``cost`` tokens. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                # Seed with `now`, not the field default: the default factory
                # would run *after* `now` was sampled, making the first elapsed
                # interval negative and briefly shrinking the bucket.
                bucket = _Bucket(tokens=float(self.burst), updated=now)
                self._buckets[key] = bucket

            elapsed = max(0.0, now - bucket.updated)
            bucket.tokens = min(
                float(self.burst), bucket.tokens + elapsed * self._rate_per_second
            )
            bucket.updated = now

            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True, 0.0

            deficit = cost - bucket.tokens
            retry_after = deficit / self._rate_per_second if self._rate_per_second else 60.0
            return False, round(retry_after, 2)

    def reset(self, key: Optional[str] = None) -> None:
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)


# ---------------------------------------------------------------------------
# Shared singletons
# ---------------------------------------------------------------------------
_token_store: Optional[TokenStore] = None
_rate_limiter: Optional[RateLimiter] = None
_lock = threading.Lock()


def get_token_store() -> TokenStore:
    global _token_store
    with _lock:
        if _token_store is None:
            _token_store = TokenStore()
        return _token_store


def get_rate_limiter() -> RateLimiter:
    global _rate_limiter
    with _lock:
        if _rate_limiter is None:
            settings = get_settings()
            _rate_limiter = RateLimiter(
                per_minute=settings.rate_limit_per_minute,
                burst=settings.rate_limit_burst,
            )
        return _rate_limiter


def describe_security() -> dict:
    """Security posture, surfaced by /health so it is never a mystery."""
    settings = get_settings()
    store = get_token_store()
    limiter = get_rate_limiter()
    return {
        "auth_enabled": settings.auth_enabled,
        "token_path": str(store.path),
        "token_configured": bool(settings.api_token) or store.path.exists(),
        "allowed_origins": settings.allowed_origins(),
        "host_header_enforced": settings.enforce_host_header,
        "rate_limit_per_minute": limiter.per_minute,
        "rate_limit_burst": limiter.burst,
        "confirm_timeout_seconds": settings.confirm_timeout,
    }
