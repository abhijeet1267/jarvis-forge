"""Privacy Egress Firewall: what this project is allowed to contact, and why.

Every outbound read in the backend funnels through :func:`jarvis.net.http_read`,
so that is where a destination is decided. The firewall turns that single choke
point into a declared allowlist instead of an open door: a host is reached
because a *rule* admits it, and a refusal names the host so the person can see
what was attempted rather than a silent hang or a bare connection error.

The distinction that matters is **which egress paths exist and who opens them**.

* A **provider** endpoint (a model API) carries the conversation -- it is the
  one path where the content of a request is the person's own words. It is
  reachable only while ``privacy_mode`` is off, or when the host is explicitly
  marked as trusted for private data *and* the person turned that on.
* A **lookup** (an encyclopedia, a search index) carries only what the caller
  put in its query string, so it is allowed under privacy mode, but it is still
  on the list rather than implied.
* A **loopback** address never leaves the machine. It is always allowed, so the
  dashboard, the companion gateway and the AI provider gateway keep working --
  refusing ``127.0.0.1`` would break the product to look strict.

Anything not on the list is refused. The list is declared in this module from
the hosts the source actually contacts (measured, not guessed), and a caller may
add a host only by naming it, which is a change a reader can see in a diff.

What this is **not**: a packet filter. It is the in-process gate for code paths
that use :func:`jarvis.net.http_read`. A third-party application on the same Mac
generates traffic this module never sees, and the screen that shows the posture
says so rather than implying network-wide control it does not have.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

#: What a destination is for. It decides whether privacy mode blocks it.
PROVIDER = "provider"   # a model endpoint: carries conversation content
LOOKUP = "lookup"       # an encyclopedia / search index: carries the query only
SERVICE = "service"     # a first-party API (calendar, tasks, mail) the user configured
LOOPBACK = "loopback"   # this Mac; never leaves the machine

#: Paths that are allowed to carry private content when privacy mode is on.
#: Loopback never leaves the machine; lookups carry only the query string.
_ALLOWED_UNDER_PRIVACY = frozenset({LOOPBACK, LOOKUP})

REFUSED = "refused"
ALLOWED = "allowed"


@dataclass(frozen=True)
class Destination:
    """One declared destination and the reason it is reachable."""

    host: str
    kind: str
    why: str
    #: Whether the conversation itself may travel here while privacy mode is on.
    private_ok: bool = False

    def to_dict(self) -> Dict[str, object]:
        return {"host": self.host, "kind": self.kind, "why": self.why,
                "private_ok": self.private_ok}


@dataclass(frozen=True)
class Verdict:
    """The firewall's answer for one read attempt."""

    allowed: bool
    host: str
    kind: str            # the destination's kind, or "unknown"
    decision: str        # ALLOWED / REFUSED
    reason: str
    privacy_mode: bool = False
    destination: Optional[Destination] = field(default=None, compare=False)

    def to_dict(self) -> Dict[str, object]:
        return {"allowed": self.allowed, "host": self.host, "kind": self.kind,
                "decision": self.decision, "reason": self.reason,
                "privacy_mode": self.privacy_mode}


class EgressRefused(PermissionError):
    """A host is not on the allowlist (or privacy mode forbids this kind)."""

    def __init__(self, verdict: Verdict) -> None:
        self.verdict = verdict
        super().__init__(verdict.reason)


def _is_loopback(host: str) -> bool:
    """Whether ``host`` is this machine.

    ``127.0.0.0/8``, ``::1``, ``0.0.0.0``, ``localhost`` and the ``*.localhost``
    convention all count. A bare label with no dot (``n8n``, ``host``) is a
    Docker-style service name -- it resolves on a local network, not the
    internet, so it is treated as loopback for the purpose of *not* refusing it.
    That is a deliberate widening: refusing ``localhost:5678`` in the n8n tests
    would be refusing the user's own service. A name with no dot cannot be a
    public DNS name, so it cannot be a silent exfiltration path either.
    """
    text = (host or "").strip().lower()
    if not text:
        return False
    if text in {"localhost", "0.0.0.0", "::1", "[::1]"} or text.endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(text.strip("[]"))
    except ValueError:
        # Not an IP literal. A single label (no dot) is a local service name.
        return "." not in text and ":" not in text
    return addr.is_loopback or addr.is_unspecified


def host_of(url: str) -> str:
    """The host a URL will connect to, lowercased, without a port."""
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return ""
    return (parts.hostname or "").strip().lower()


#: Hosts the source actually contacts. Each entry states what it is for; a host
#: added here without a reason is a reviewer's cue that something changed.
_DECLARED: Tuple[Destination, ...] = (
    # -- loopback services (this Mac) -----------------------------------------
    Destination("127.0.0.1", LOOPBACK, "the backend, dashboard and gateways on this Mac"),
    Destination("localhost", LOOPBACK, "the backend, dashboard and gateways on this Mac"),
    Destination("0.0.0.0", LOOPBACK, "a bound-on-all-interfaces local service"),
    # A bare label is a local service name (see ``_is_loopback``); the kind is
    # fixed per-host only for the ones the source names. Unknown bare labels are
    # admitted by the loopback rule rather than by a declared row.

    # -- model providers -------------------------------------------------------
    Destination("api.openai.com", PROVIDER, "the OpenAI model endpoint"),
    Destination("api.groq.com", PROVIDER, "the Groq model endpoint"),
    Destination("generativelanguage.googleapis.com", PROVIDER, "the Gemini model endpoint"),
    Destination("gen.pollinations.ai", PROVIDER, "the Pollinations model endpoint"),

    # -- lookups (query only, never the conversation) --------------------------
    Destination("en.wikipedia.org", LOOKUP, "asking an encyclopedia a factual question",
                private_ok=True),
    Destination("api.duckduckgo.com", LOOKUP, "instant-answer lookup", private_ok=True),
    Destination("export.arxiv.org", LOOKUP, "searching arXiv for papers", private_ok=True),
    Destination("arxiv.org", LOOKUP, "resolving an arXiv paper", private_ok=True),
    Destination("doi.org", LOOKUP, "resolving a DOI to its paper", private_ok=True),
    Destination("api.crossref.org", LOOKUP, "resolving citation metadata", private_ok=True),

    # -- first-party services the person configured ---------------------------
    Destination("www.googleapis.com", SERVICE, "the Google API the user connected"),
    Destination("oauth2.googleapis.com", SERVICE, "the Google OAuth token exchange"),
    Destination("calendar.google.com", SERVICE, "the Google Calendar the user connected"),
    Destination("mail.google.com", SERVICE, "the Gmail account the user connected"),
    Destination("www.google.com", SERVICE, "Google's own endpoints (tasks, discovery)"),
    Destination("github.com", SERVICE, "the GitHub account the user connected"),
    Destination("api.github.com", SERVICE, "the GitHub API the user connected"),
    Destination("www.youtube.com", SERVICE, "a YouTube link the user asked about"),
)


class EgressFirewall:
    """The allowlist, plus the privacy-mode rule that reads it.

    One instance per process is the normal case (``get_firewall``); the class is
    constructed directly in tests, which is why policy is instance state and not
    a module global.
    """

    def __init__(self, destinations: Optional[Iterable[Destination]] = None) -> None:
        self._by_host: Dict[str, Destination] = {}
        for dest in (destinations if destinations is not None else _DECLARED):
            self._by_host[dest.host.lower()] = dest

    # -- reads ---------------------------------------------------------------
    def destinations(self) -> List[Dict[str, object]]:
        return [d.to_dict() for d in sorted(self._by_host.values(),
                                            key=lambda d: (d.kind, d.host))]

    def destination(self, host: str) -> Optional[Destination]:
        return self._by_host.get(str(host or "").strip().lower())

    def add(self, destination: Destination) -> None:
        """Declare one more host. Used by tests and by a caller that must reach
        something the project did not know about -- never to widen an existing
        entry silently."""
        self._by_host[destination.host.lower()] = destination

    # -- the decision --------------------------------------------------------
    def check(self, url: str, *, privacy_mode: bool = False) -> Verdict:
        """Decide whether a read to ``url`` may proceed.
        """
        host = host_of(url)
        if not host:
            return Verdict(False, host, "unknown", REFUSED,
                           "the address names no host, so there is nowhere to send it")
        if _is_loopback(host):
            dest = self.destination(host) or Destination(
                host, LOOPBACK, "a local service on this Mac")
            return Verdict(True, host, LOOPBACK, ALLOWED,
                           f"{host} is on this Mac, so the read never leaves it",
                           privacy_mode=privacy_mode, destination=dest)

        dest = self.destination(host)
        if dest is None:
            return Verdict(
                False, host, "unknown", REFUSED,
                f"{host} is not a declared destination; nothing in this project "
                "contacts it, so the read is refused rather than sent",
                privacy_mode=privacy_mode)

        if privacy_mode and not (dest.private_ok or dest.kind in _ALLOWED_UNDER_PRIVACY):
            return Verdict(
                False, host, dest.kind, REFUSED,
                f"privacy mode is on and {host} is a {dest.kind} destination, which "
                "would carry the conversation off this Mac",
                privacy_mode=privacy_mode, destination=dest)

        return Verdict(True, host, dest.kind, ALLOWED,
                       f"{host} is declared ({dest.kind}): {dest.why}",
                       privacy_mode=privacy_mode, destination=dest)


_FIREWALL: Optional[EgressFirewall] = None


def require(url: str) -> None:
    """Refuse a destination the firewall does not admit, or return None.

    The seam for callers that do **not** go through
    :func:`jarvis.net.http_read` -- the integrations that build an ``httpx``
    client directly (the relay, the calendar provider, voice STT, the transport
    endpoint). Without this they would be an ungated way out, which is the same
    defect as having no firewall at all: the next caller that forgets to check
    is the one that matters.

    Raises :class:`EgressRefused` so the caller's existing transport-error
    handling can name it, rather than a bare boolean a caller could ignore.
    """
    verdict = get_firewall().check(url, privacy_mode=privacy_mode_on())
    if not verdict.allowed:
        raise EgressRefused(verdict)


def get_firewall() -> EgressFirewall:
    """The process's firewall. Built once; policy is cheap and read-only."""
    global _FIREWALL
    if _FIREWALL is None:
        _FIREWALL = EgressFirewall()
    return _FIREWALL


def reset_firewall() -> None:
    """Drop the process firewall so the next call rebuilds it (tests only)."""
    global _FIREWALL
    _FIREWALL = None


def privacy_mode_on() -> bool:
    """Whether privacy mode is on right now.

    Read from settings on every check, so flipping the switch takes effect on
    the next read without rebuilding the firewall.
    """
    try:
        from ..config import get_settings

        return bool(get_settings().privacy_mode)
    except Exception:  # pragma: no cover - a settings failure must not open egress
        return True


def describe_firewall() -> Dict[str, object]:
    """Posture for ``/health`` and the Trust screen."""
    firewall = get_firewall()
    on = privacy_mode_on()
    by_kind: Dict[str, int] = {}
    for dest in firewall.destinations():
        by_kind[str(dest["kind"])] = by_kind.get(str(dest["kind"]), 0) + 1
    return {
        "privacy_mode": on,
        "enforced_at": "jarvis.net.http_read",
        "declared_hosts": len(firewall.destinations()),
        "by_kind": by_kind,
        "destinations": firewall.destinations(),
        "not_covered": (
            "This gates reads that go through jarvis.net.http_read. Traffic from "
            "other applications on this Mac is not seen or controlled."
        ),
    }