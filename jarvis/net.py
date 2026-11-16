"""Network reads that stop when they are told to stop.

Every caller in this project passes a `timeout` to `urllib.request.urlopen`, and that
number means something narrower than it looks: it is a **socket inactivity** timeout. A
server that answers and then sends one byte every few seconds never trips it, so the
caller's "20 second" read can run for as long as the server likes to keep dribbling.
Measured against a local trickler before this module existed, an arXiv search was still
waiting after 60 seconds with `timeout=20` set, and a first full-suite run of its tests
did not fail -- it hung.

`http_read` makes the number on the label the number that is true: a total budget for the
whole call, connect and body included.

The honest limitation: a Python thread cannot be killed, so when the budget expires the
reading thread is abandoned and its socket closes when the response is garbage-collected.
That is a stray connection for a while; the alternative was the caller waiting forever.
"""

from __future__ import annotations

import contextlib
import threading
import time
import urllib.request
from contextvars import ContextVar

__all__ = ["DeadlineExceeded", "EgressRefused", "http_read", "remaining_budget",
           "request_budget"]

#: How long a single socket wait may run inside a total budget. Small enough that a
#: stalled server is noticed quickly, large enough that a normal response is not
#: interrupted mid-flight.
GAP_TIMEOUT = 5.0


class DeadlineExceeded(TimeoutError):
    """The read did not finish inside its total budget.

    Carries the budget in its message, because the caller's job is to tell a person how
    long they waited -- and "it timed out" without a number is the same unexplained
    silence this module exists to remove.
    """

    def __init__(self, seconds: float, what: str = "the server") -> None:
        self.seconds = float(seconds)
        super().__init__(f"{what} kept the connection open without finishing within "
                         f"{seconds:g}s, so it was stopped")


#: Re-exported so a caller can catch a refusal from this module without importing
#: the security package, keeping ``jarvis.net`` the single dependency it already is.
from .security.egress import EgressRefused, get_firewall, host_of, privacy_mode_on  # noqa: E402


def _egress_refusal(request) -> "EgressRefused | None":
    """The firewall's verdict on this request, as an exception to raise, or None.

    A request whose URL cannot be read is refused rather than allowed: there is
    no address to check, and an unchecked read is exactly what this gate exists
    to stop. A firewall that cannot be built (an import failure) refuses too --
    failing closed is the only honest default for an egress gate.
    """
    url = getattr(request, "full_url", "") or getattr(request, "get_full_url", lambda: "")()
    try:
        verdict = get_firewall().check(str(url), privacy_mode=privacy_mode_on())
    except Exception as exc:  # pragma: no cover - defensive, must fail closed
        from .security.egress import EgressRefused as _Refused, Verdict

        return _Refused(Verdict(False, host_of(str(url)), "unknown", "refused",
                                f"the egress firewall could not decide ({exc}); "
                                "the read is refused rather than sent"))
    if verdict.allowed:
        return None
    return EgressRefused(verdict)


def http_read(request, *, total: float, what: str = "the server",
              gap: float = GAP_TIMEOUT) -> bytes:
    """Fetch `request` and return its body, giving up after `total` seconds in all.

    `urlopen` stays the thing that opens the connection, so tests that patch it keep
    working; only the wait is wrapped.

    A sentence-level budget, if one is open, is the ceiling: `total` is the cap for this
    one read, and the remaining budget is the cap for the request as a whole. Whichever is
    smaller wins, so neither can quietly outrun the other.
    """
    allowed = min(float(total), remaining_budget())
    if allowed <= 0:
        raise DeadlineExceeded(float(total), f"{what} (the sentence's network budget was "
                                             "already spent before this read started)")

    # The Privacy Egress Firewall. Every outbound read in this project goes
    # through this function, so this is where a destination is decided: a host
    # is reached because a rule admits it, and privacy mode refuses the kinds
    # that would carry the conversation off the machine. Refused *before* a
    # socket is opened, so a blocked read costs nothing and cannot be mistaken
    # for a network failure.
    refusal = _egress_refusal(request)
    if refusal is not None:
        raise refusal

    box: dict = {}

    def read_it() -> None:
        try:
            with urllib.request.urlopen(request, timeout=gap) as response:
                box["body"] = response.read()
        except BaseException as exc:  # surfaced by the caller thread below
            box["error"] = exc

    worker = threading.Thread(target=read_it, daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(allowed)
    if worker.is_alive():
        _consume(allowed)
        raise DeadlineExceeded(allowed, what)
    _consume(time.monotonic() - started)
    if "error" in box:
        raise box["error"]
    return bytes(box.get("body") or b"")


#: Seconds of network budget left for the request being handled, or inf when no
#: sentence-level budget is open. A contextvar because the reads happen in the request's
#: own task, and a leaked budget would eventually refuse every call the process makes.
_BUDGET: ContextVar[float] = ContextVar("jarvis_net_budget", default=float("inf"))


def remaining_budget() -> float:
    """How much of the current sentence's network budget is left."""
    return _BUDGET.get()


def _consume(seconds: float) -> None:
    left = _BUDGET.get()
    if left != float("inf"):
        _BUDGET.set(max(0.0, left - float(seconds)))


@contextlib.contextmanager
def request_budget(seconds: float):
    """Cap every network read made inside this block at `seconds` in total."""
    token = _BUDGET.set(float(seconds))
    try:
        yield
    finally:
        _BUDGET.reset(token)


def remaining_of(total: float, started: float) -> float:
    """Seconds left in a budget, never negative -- so a retry can be refused by the
    clock rather than started and abandoned."""
    return total - (time.monotonic() - started)
