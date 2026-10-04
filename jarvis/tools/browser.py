"""Opening a web address, and asking the browser afterwards what it is showing.

Asked what this system should feel like, the example was "hey Vantage, open the github
site". Measured first: there was no way to do it, and worse, `open github.com` was being
read as a *file* -- the planner built `<home>/github.com` and offered to read it. So this
module exists to replace a wrong answer, not only a missing one.

Two rules hold it honest:

* Nothing is guessed into a domain. A bare name opens only if it is in :data:`NAMED_SITES`
  -- a short, checkable list -- because inventing `flurbledink.com` sends the user's
  browser, and their session cookies, to whoever registered that name.
* A launch is not a page. ``open`` exiting 0 proves a browser was told; the address on
  screen is read back from the browser itself where that is possible, and when the browser
  will not answer, the answer is *unverified* rather than a claim.
"""

from __future__ import annotations

import re
from typing import Any, List, Tuple

from ..core.models import RiskLevel, ToolSpec
from .base import ToolContext, fail, ok, run_command, unverified

TOOL = "open_url"

#: Names a person is likely to say without a domain, and the address each one means.
#: Deliberately short: every entry here is checkable, and an unlisted name is a question.
NAMED_SITES = {
    "github": "https://github.com",
    "gmail": "https://mail.google.com",
    "google": "https://www.google.com",
    "youtube": "https://www.youtube.com",
    "maps": "https://maps.google.com",
    "calendar": "https://calendar.google.com",
    #: Brand-qualified because the bare word now means the installed application: saying
    #: "Calendar" opens Calendar.app, so the web product has to be named as itself.
    "google calendar": "https://calendar.google.com",
    "drive": "https://drive.google.com",
    "reddit": "https://www.reddit.com",
    "wikipedia": "https://www.wikipedia.org",
    "stack overflow": "https://stackoverflow.com",
    "linkedin": "https://www.linkedin.com",
    "twitter": "https://twitter.com",
    "whatsapp": "https://web.whatsapp.com",
}

_DOMAIN = re.compile(r"^(?:[\w-]+\.)+[a-z]{2,}(?:[/?#]\S*)?$", re.I)
_SCHEME = re.compile(r"^https?://", re.I)

#: How to ask each browser what it is showing. Anything not here cannot be confirmed.
_FRONT_URL = {
    "chrome": 'tell application "Google Chrome" to get URL of active tab of front window',
    "safari": 'tell application "Safari" to get URL of front document',
}


def normalise(target: str) -> Tuple[str, str]:
    """(url, problem) -- exactly one of the two is non-empty.

    A known name becomes its real address; a bare domain is upgraded to https; a random
    word is refused rather than guessed at.
    """
    text = str(target or "").strip().strip('"').rstrip("/")
    if not text:
        return "", "Which site?"
    if _SCHEME.match(text):
        return text, ""
    if "." in text and _DOMAIN.match(text.split("/")[0]):
        return f"https://{text}", ""
    named = NAMED_SITES.get(text.lower())
    if named:
        return named, ""
    return "", (f"I do not know an address for {text!r}, and I will not guess one -- "
                "a wrong domain opens your logged-in browser session on a stranger's site. "
                "Give me the address, or a name from: "
                f"{', '.join(sorted(NAMED_SITES))}.")


#: How long to wait for a browser to say what it is showing. Short on purpose: the page
#: is already opening, and a sentence that stalls behind an Apple event is worse than one
#: that says it could not confirm.
PAGE_QUERY_TIMEOUT_S = 4.0

#: Which browser to use when the sentence does not say. Chrome first because it is the
#: one whose open tab can be read back; a browser with no queryable URL still gets the
#: page, and the answer then says it could not confirm.
BROWSER_KEYS = ("chrome", "safari", "firefox", "brave", "edge")


def installed_browsers(apps, platform: str = "macos") -> List[Any]:
    """Browsers present on this machine, best-first for confirming what they show."""
    import os
    import shutil

    from .agents import BUNDLE_ROOTS

    found = []
    for key in BROWSER_KEYS:
        spec = apps.get(key)
        if spec is None:
            continue
        target = apps.launch_target(spec, platform=platform) if hasattr(apps, "launch_target") \
            else spec.launch.get(platform, "")
        if not target:
            continue
        if platform == "macos":
            if any(os.path.isdir(os.path.expanduser(root) + f"/{target}.app")
                   for root in BUNDLE_ROOTS):
                found.append(spec)
        elif shutil.which(target):
            found.append(spec)
    return found


async def open_url(ctx: ToolContext, url: str = "", browser: str = "") -> Any:
    """Open a web address, and report what the browser says it is showing."""
    target, problem = normalise(url)
    if not target:
        return fail(TOOL, problem, device=ctx.device_id, asked=url)

    spec = ctx.apps.resolve(browser, platform=ctx.platform) if str(browser).strip() else None
    if str(browser).strip() and spec is None:
        return fail(TOOL, f"I could not find a browser called {browser!r}.",
                    device=ctx.device_id, asked=browser)
    if spec is None:
        # Nothing named: use a browser this machine actually has, so the page it ended up
        # on can be read back. `open <url>` alone hands the choice to LaunchServices and
        # leaves no way to ask what happened.
        spec = (installed_browsers(ctx.apps, platform=ctx.platform) or [None])[0]

    if spec is None:
        argv = ["open", target]
        named_key = ""
    else:
        target_name = ctx.apps.launch_target(spec, platform=ctx.platform) or spec.display_name
        argv = ["open", "-a", target_name, target]
        named_key = spec.key

    code, _out, err = await run_command(argv, detach=True)
    if code != 0:
        return fail(TOOL, f"Could not open {target}: {err or 'the launch failed'}",
                    device=ctx.device_id)

    shown = ""
    reason = ""
    asked = named_key in _FRONT_URL
    if asked:
        # Measured live: an Apple event to a browser that has no front window, or that
        # macOS has not authorised, does not fail -- it waits. Phase 34's rule is that one
        # sentence carries one deadline, so this read is bounded like every other.
        rcode, rout, rerr = await run_command(
            ["osascript", "-e", _FRONT_URL[named_key]], timeout=PAGE_QUERY_TIMEOUT_S)
        if rcode == 0 and rout.strip():
            shown = rout.strip()
        elif rcode == 124:
            reason = "it did not answer in time"
        elif "authoriz" in (rerr or "").lower() or "not allowed" in (rerr or "").lower():
            reason = ("macOS has not authorised asking the browser what it is showing "
                      "(System Settings → Privacy & Security → Automation)")
        else:
            reason = (rerr or "it has no window yet").strip()[:120]

    if shown:
        return ok(TOOL, f"Opened {shown}.", device=ctx.device_id, verified=True,
                  opened=target, shown_url=shown, browser=named_key)
    if asked:
        return unverified(TOOL, f"Asked {spec.display_name} to open {target}; I could not "
                          f"confirm the page -- {reason}.",
                          device=ctx.device_id, opened=target, shown_url="", reason=reason)
    return unverified(TOOL, f"Asked your default browser to open {target}; I cannot confirm "
                      "what it is showing.", device=ctx.device_id, opened=target,
                      shown_url="")


SPECS: List[Tuple[ToolSpec, Any]] = [
    (
        ToolSpec(
            name=TOOL,
            description="Open a web address in a browser and report what the browser shows.",
            agent="laptop",
            risk=RiskLevel.LOW,
            reversible=True,
            requires_confirmation=False,
            parameters={"url": "string", "browser": "string"},
            required_parameters=["url"],
            tags=["laptop", "browser", "web"],
        ),
        open_url,
    ),
]
