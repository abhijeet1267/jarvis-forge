"""Per-provider/model route-health observations (9Router dead-route detection).

A lightweight, thread-safe, bounded in-memory registry that records the last few
facts about each configured ``(provider_id, model)`` pair — success/failure, latency,
HTTP status, error category, and whether the observation came from a real chat
request, an explicit test, or a cheap gateway reachability probe.

Design constraints honoured here:

* **Bounded.** Only the latest observation per key is kept; the key list itself is
  capped so a runaway model namespace cannot grow memory without limit.
* **No secrets.** Only redacted categories, statuses and timestamps are stored. The
  user's prompt and the assistant's reply are never captured.
* **No chat payloads.** This is a health ledger, not an audit trail.
* **Thread-safe.** Concurrent chat/test requests must not cross-attribute a result
  to another provider or corrupt a count, so every read/mutation goes through one
  lock.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Tuple

#: Hard cap on tracked (provider, model) keys. Oldest key is dropped beyond this.
MAX_KEYS = 512

#: Route-health states, in the order they are reported.
HEALTH_UNKNOWN = "unknown"
HEALTH_HEALTHY = "healthy"
HEALTH_DEGRADED = "degraded"
HEALTH_UNHEALTHY = "unhealthy"
HEALTH_NOT_CONFIGURED = "not_configured"

#: Failure categories that count as evidence a *route* is bad (dead/flaky), as
#: opposed to evidence about the request or the credential.
_ROUTE_FAILURE_CATEGORIES = {
    "timeout",
    "rate_limited",
    "server_error",
    "route_unavailable",
    "upstream_gone",
    "network",
}

#: Consecutive route-failures before a provider/model is marked ``degraded``.
DEGRADED_THRESHOLD = 2
#: Consecutive route-failures before a provider/model is marked ``unhealthy``.
UNHEALTHY_THRESHOLD = 5


class RouteHealthRegistry:
    """One in-process ledger of the latest health evidence per provider/model."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._order: list = []  # insertion order for bounded eviction

    # -- recording ----------------------------------------------------------

    def record(
        self,
        provider_id: str,
        model: str,
        *,
        ok: bool,
        source: str,
        status: int = 0,
        category: str = "",
        latency_ms: int = 0,
        upstream_model: str = "",
        upstream_id: str = "",
        upstream_identity_known: bool = False,
        error: str = "",
    ) -> None:
        """Record one observation, replacing any prior one for the same key."""
        now = time.time()
        key = (str(provider_id), str(model or ""))
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = self._new_entry()
                self._entries[key] = entry
                self._order.append(key)
                self._evict_locked()

            if ok:
                entry["last_success_at"] = now
                entry["consecutive_failures"] = 0
                entry["last_error_category"] = ""
                entry["last_http_status"] = 0
                entry["last_error"] = ""
            else:
                entry["last_failure_at"] = now
                cat = category or "unknown"
                entry["last_error_category"] = cat
                entry["last_http_status"] = status
                entry["last_error"] = error[:200]
                if cat in _ROUTE_FAILURE_CATEGORIES:
                    entry["consecutive_failures"] += 1
                # Auth/config/request errors are not route evidence; leave the
                # consecutive-failure counter untouched so they don't poison it.

            entry["last_latency_ms"] = latency_ms
            entry["last_check_source"] = source
            if upstream_model:
                entry["upstream_model"] = upstream_model
                entry["upstream_identity_known"] = True
            elif upstream_id:
                entry["upstream_id"] = upstream_id
                entry["upstream_identity_known"] = True
            entry["upstream_identity_known"] = bool(
                upstream_model or upstream_id or upstream_identity_known
            )
            entry["updated_at"] = now

    @staticmethod
    def _new_entry() -> Dict[str, Any]:
        return {
            "last_success_at": None,
            "last_failure_at": None,
            "last_latency_ms": 0,
            "consecutive_failures": 0,
            "last_error_category": "",
            "last_http_status": 0,
            "last_error": "",
            "last_check_source": "",
            "upstream_model": "",
            "upstream_id": "",
            "upstream_identity_known": False,
            "updated_at": None,
        }

    def _evict_locked(self) -> None:
        while len(self._order) > MAX_KEYS:
            oldest = self._order.pop(0)
            self._entries.pop(oldest, None)

    # -- reading ------------------------------------------------------------

    def snapshot(self, provider_id: str, model: str) -> Dict[str, Any]:
        """A copy of the latest observation for a key, or a blank one."""
        key = (str(provider_id), str(model or ""))
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return self._new_entry()
            return dict(entry)

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()
            self._order.clear()


#: Process-wide singleton, reset by tests through :func:`reset_route_health`.
_registry: Optional[RouteHealthRegistry] = None


def get_route_health() -> RouteHealthRegistry:
    global _registry
    if _registry is None:
        _registry = RouteHealthRegistry()
    return _registry


def reset_route_health() -> None:
    global _registry
    _registry = None


def compute_state(entry: Dict[str, Any], *, configured: bool) -> str:
    """Map a recorded observation onto one of the documented health states.

    The mapping is deliberately conservative: a single failure never marks a route
    unhealthy, and no evidence at all is ``unknown``, never ``healthy``.
    """
    if not configured:
        return HEALTH_NOT_CONFIGURED
    if entry.get("last_success_at") is None and entry.get("last_failure_at") is None:
        return HEALTH_UNKNOWN
    failures = int(entry.get("consecutive_failures") or 0)
    if entry.get("last_success_at") and failures == 0:
        return HEALTH_HEALTHY
    if failures >= UNHEALTHY_THRESHOLD:
        return HEALTH_UNHEALTHY
    if failures >= DEGRADED_THRESHOLD:
        return HEALTH_DEGRADED
    return HEALTH_UNKNOWN


def human_state(state: str, *, gateway_reachable: Optional[bool] = None) -> str:
    """A sentence a person can act on, built from what was actually observed.

    ``unknown`` used to read "Gateway reachable — route status unknown"
    unconditionally. That is a claim, and the API had no evidence for it: a
    provider whose last request timed out still showed "Gateway reachable",
    because the string was a constant rather than a fact. The reachability
    argument is now required for that branch — pass ``True`` only when a probe
    actually succeeded, ``False`` when it failed, and ``None`` when nothing was
    probed.
    """
    if state == HEALTH_UNKNOWN:
        if gateway_reachable is True:
            return "Gateway reachable — no route observations yet"
        if gateway_reachable is False:
            return "Gateway unreachable — route not observed yet"
        return "No route observations yet"
    return {
        HEALTH_UNKNOWN: "No route observations yet",
        HEALTH_HEALTHY: "Recent completion succeeded",
        HEALTH_DEGRADED: "Route degraded",
        HEALTH_UNHEALTHY: "Route unavailable",
        HEALTH_NOT_CONFIGURED: "Not configured",
    }.get(state, state)