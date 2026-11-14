"""Browser automation as tools, so the controller is reachable (R4.3).

``jarvis/tools/browser_controller.py`` has been a well-built Playwright
controller since Phase 92 — sessions with isolation, navigation with
verification, click and type with state checks — and **nothing imported it**.
It was not in the tool registry, no API route reached it, and no surface
could call it. The browser slice the roadmap asks for ("open URL → read
title/content → save summary to an authorized file, verified") was therefore
unreachable from the product, however good the controller was.

This module is the door. It wraps the controller's operations as ordinary
Vantage tools, so they go through the same registry, the same permission
gate and the same audit trail as everything else.

**The risk tiers are not a detail.**

- Reading a page is a **READ**. It changes nothing on the user's machine.
- Navigating is **LOW**: it changes a browser tab, which is reversible by
  navigating away.
- Clicking and typing are **EXTERNAL and confirmation-gated**. They act on
  whatever the page says to act on -- a button that sends an email, a field
  that submits a payment -- and the person must approve that. A page cannot
  approve itself; the confirmation card is the only door, and the model's
  description of what the page says is not a grant.

**Page text is data, never authority.** The controller's own docstring has
claimed injection-resistant handling since Phase 92 without having any. The
rules now live in :mod:`jarvis.tools.page_content` and are applied to every
read: a page that reads like instructions is *flagged*, not obeyed, and the
content is marked as the page's words rather than the user's.

The tools need Playwright, which is an optional dependency. When it is
missing, the tools **say so** -- they do not fail to import, and they do not
pretend to have browsed anything.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.models import RiskLevel, ToolSpec
from ..logging_setup import get_logger
from .base import ToolContext, fail, ok, unverified

log = get_logger("tools.browser_tools")


def _controller() -> Any:
    """The shared browser controller."""
    from .browser_controller import BrowserController

    return BrowserController.get_instance()


def _not_available(tool: str, device: str) -> Any:
    from .browser_controller import PLAYWRIGHT_AVAILABLE, PLAYWRIGHT_MISSING

    if PLAYWRIGHT_AVAILABLE:
        return fail(tool, "The browser controller is not available.", device=device)
    return fail(tool, PLAYWRIGHT_MISSING, device=device)


async def browser_open(ctx: ToolContext, url: str = "") -> Any:
    """Open a URL in an isolated browser session and report the page."""
    from .browser_controller import PLAYWRIGHT_AVAILABLE

    if not PLAYWRIGHT_AVAILABLE:
        return _not_available("browser.open", ctx.device_id)

    target = (url or "").strip()
    if not target:
        return fail("browser.open", "Which page should I open?", device=ctx.device_id)

    controller = _controller()
    try:
        session = await controller.create_session(
            owner_session=ctx.session_id or None,
            device=ctx.device_id,
        )
    except Exception as exc:
        return fail("browser.open", f"Could not start a browser: {exc}", device=ctx.device_id)

    result = await controller.navigate(session.session_id, target)
    if not result.ok:
        return result

    # The navigation is verified by the controller (it checks the URL it
    # actually landed on). The page's text is read here, and it is the
    # page's words -- not the user's.
    read = await controller.read_page(session.session_id)
    output: Dict[str, Any] = {
        "browser_session": session.session_id,
        "url": result.output.get("actual_url") or result.output.get("requested_url", ""),
        "title": result.output.get("title", ""),
    }
    if read.ok:
        output["text"] = read.output.get("text", "")
        output["untrusted"] = True
        output["looks_like_instruction"] = read.output.get("looks_like_instruction", False)
        output["instruction_markers"] = read.output.get("instruction_markers", [])
        output["safe_for_model"] = read.output.get("safe_for_model", True)
        output["truncated"] = read.output.get("truncated", False)
    else:
        output["text"] = ""
        output["read_error"] = read.error

    # The controller already checked the URL it landed on, and the page text was
    # read back. Reporting `verified: False` here was a false negative -- it told
    # the person the open could not be confirmed when it demonstrably had been.
    return ok(
        "browser.open",
        f"Opened {output['url']}.",
        device=ctx.device_id,
        verified=bool(output.get("url")),
        **output,
    )


async def browser_read(ctx: ToolContext, browser_session: str = "") -> Any:
    """Read the visible text of an open page."""
    from .browser_controller import PLAYWRIGHT_AVAILABLE

    if not PLAYWRIGHT_AVAILABLE:
        return _not_available("browser.read", ctx.device_id)

    session_id = (browser_session or "").strip() or _controller().latest_session_for(
        ctx.session_id) or ""
    if not session_id:
        return fail("browser.read", "There is no page open in this conversation. "
                    'Say "open <url>" first.', device=ctx.device_id)

    result = await _controller().read_page(session_id)
    if not result.ok:
        return result
    # text was actually returned, so the read happened; the honesty flag belongs
    # on the *content* (untrusted), not on whether the read worked.
    return ok(
        "browser.read",
        result.message,
        device=ctx.device_id,
        verified=bool(result.output.get("text")),
        **result.output,
    )


async def browser_click(ctx: ToolContext, browser_session: str = "", selector: str = "") -> Any:
    """Click an element on an open page."""
    from .browser_controller import PLAYWRIGHT_AVAILABLE

    if not PLAYWRIGHT_AVAILABLE:
        return _not_available("browser.click", ctx.device_id)

    session_id = (browser_session or "").strip() or _controller().latest_session_for(
        ctx.session_id) or ""
    if not session_id:
        return fail("browser.click", "There is no page open in this conversation. "
                    'Say "open <url>" first.', device=ctx.device_id)
    if not (selector or "").strip():
        return fail("browser.click", "What should I click?", device=ctx.device_id)

    result = await _controller().click(session_id, selector)
    if not result.ok:
        return result
    return ok("browser.click", result.message, device=ctx.device_id, **result.output)


async def browser_type(ctx: ToolContext, browser_session: str = "", selector: str = "",
                      text: str = "") -> Any:
    """Type into a field on an open page."""
    from .browser_controller import PLAYWRIGHT_AVAILABLE

    if not PLAYWRIGHT_AVAILABLE:
        return _not_available("browser.type", ctx.device_id)

    session_id = (browser_session or "").strip() or _controller().latest_session_for(
        ctx.session_id) or ""
    if not session_id:
        return fail("browser.type", "There is no page open in this conversation. "
                    'Say "open <url>" first.', device=ctx.device_id)
    if not (selector or "").strip():
        return fail("browser.type", "Which field should I type into?", device=ctx.device_id)

    result = await _controller().type_text(session_id, selector, text)
    if not result.ok:
        return result
    return ok("browser.type", result.message, device=ctx.device_id, **result.output)


async def browser_close(ctx: ToolContext, browser_session: str = "") -> Any:
    """Close a browser session."""
    from .browser_controller import PLAYWRIGHT_AVAILABLE

    if not PLAYWRIGHT_AVAILABLE:
        return _not_available("browser.close", ctx.device_id)

    session_id = (browser_session or "").strip() or _controller().latest_session_for(
        ctx.session_id) or ""
    if not session_id:
        return fail("browser.close", "There is no page open in this conversation to "
                    "close.", device=ctx.device_id)

    result = await _controller().close_session(session_id)
    if not result.ok:
        return result
    # The controller closed the session it was given and said so; reporting
    # `verified: False` here was a false negative, the same one corrected for
    # open and read -- the failure signal has to mean something.
    return ok("browser.close", result.message, device=ctx.device_id, verified=True)


async def browser_save(ctx: ToolContext, url: str = "", path: str = "") -> Any:
    """Read a page and save what it says to a file.

    The last third of the browser slice: open a URL, read the text, and put it
    somewhere the person chose.

    **The write goes through the file layer, not around it.** The path is
    resolved and guarded by exactly the code ``write_file`` uses, and the
    result is confirmed by reading the file back -- so a protected path is
    refused, and a file that did not land is not reported as written. Writing
    the bytes here instead would have produced a second, weaker path to the
    disk, and a second answer to "may I write here?".

    **The page's words are the page's words.** What is saved is the text the
    page rendered, marked untrusted in the file's own header, so a file that
    is read later cannot be mistaken for something the user said or for
    something Vantage asserted. A page that tries to issue instructions is
    flagged in that header too.

    The whole thing is EXTERNAL and confirmation-gated: it puts something
    from the internet on the user's disk.
    """
    from .browser_controller import PLAYWRIGHT_AVAILABLE

    if not PLAYWRIGHT_AVAILABLE:
        return _not_available("browser_save", ctx.device_id)

    target = (url or "").strip()
    if not target:
        return fail("browser_save", "Which page should I save?", device=ctx.device_id)
    destination = (path or "").strip()
    if not destination:
        return fail("browser_save", "Where should I save it?", device=ctx.device_id)

    # The file layer's own resolution and guard, so a refusal here is the
    # same refusal `write_file` would give.
    from .files import _guard, _resolve

    resolved = _resolve(destination)
    guard = _guard(resolved)
    if guard:
        return fail("browser_save", guard, device=ctx.device_id)
    if resolved.is_dir():
        return fail("browser_save", f"{resolved} is a folder, not a file.",
                    device=ctx.device_id)

    # Read the page first.
    opened = await browser_open(ctx, url=target)
    if not opened.ok:
        return opened
    text = opened.output.get("text") or ""
    source_url = opened.output.get("url") or target
    looks_like_instruction = bool(opened.output.get("looks_like_instruction"))

    session_id = opened.output.get("browser_session")
    if session_id:
        try:
            await _controller().close_session(session_id)
        except Exception:
            pass  # the page is read; a failed close must not lose the content

    # A header that says where the words came from, so the file cannot later
    # be mistaken for the user's own.
    header = [
        f"# Saved from {source_url}",
        "# This is the page's text, not the user's words and not Vantage's.",
    ]
    if looks_like_instruction:
        header.append(
            "# WARNING: this page contains text that reads like instructions. "
            "It was saved, not obeyed."
        )
    payload = "\n".join(header) + "\n\n" + text + ("\n" if text and not text.endswith("\n") else "")

    # The write itself, and the confirmation, both come from the file layer.
    from .files import write_file

    written = await write_file(ctx, path=str(resolved), content=payload, mode="overwrite")
    if not written.ok:
        return fail("browser_save", written.error or "Could not save the page.",
                    device=ctx.device_id)

    return ok(
        "browser_save",
        written.message,
        device=ctx.device_id,
        path=written.output.get("path", str(resolved)),
        url=source_url,
        bytes=written.output.get("size", len(payload)),
        # The write's own verification, carried through rather than restated.
        # `write_file` puts it on the result, not in the output dict -- the
        # same shape every other tool reports it in.
        verified=bool(written.verified),
        verification_note=(
            "The page was read and the file was written and read back."
            if written.verified
            else "The page was read, but the file could not be confirmed on disk."
        ),
        untrusted=True,
        looks_like_instruction=looks_like_instruction,
    )


#: The tools, and the tier each one sits at.
#:
#: Clicking and typing are EXTERNAL and confirmation-gated. A page can ask
#: for a click, and a click can send an email; the person approves it. The
#: page's own text is never that approval.
SPECS: List[tuple] = [
    (
        ToolSpec(
            name="browser_open",
            description="Open a web page in an isolated browser and read its text.",
            agent="laptop",
            risk=RiskLevel.LOW,
            reversible=True,
            requires_confirmation=False,
            parameters={"url": "string"},
            required_parameters=["url"],
            tags=["browser", "read"],
        ),
        browser_open,
    ),
    (
        ToolSpec(
            name="browser_read",
            description="Read the visible text of an open browser page.",
            agent="laptop",
            risk=RiskLevel.READ,
            reversible=True,
            requires_confirmation=False,
            parameters={"browser_session": "string"},
            required_parameters=[],
            tags=["browser", "read"],
        ),
        browser_read,
    ),
    (
        ToolSpec(
            name="browser_click",
            description="Click an element on an open browser page.",
            agent="laptop",
            risk=RiskLevel.EXTERNAL,
            reversible=False,
            requires_confirmation=True,
            parameters={"browser_session": "string", "selector": "string"},
            required_parameters=["selector"],
            tags=["browser", "write"],
        ),
        browser_click,
    ),
    (
        ToolSpec(
            name="browser_type",
            description="Type text into a field on an open browser page.",
            agent="laptop",
            risk=RiskLevel.EXTERNAL,
            reversible=False,
            requires_confirmation=True,
            parameters={"browser_session": "string", "selector": "string", "text": "string"},
            required_parameters=["selector"],
            tags=["browser", "write"],
        ),
        browser_type,
    ),
    (
        ToolSpec(
            name="browser_close",
            description="Close a browser session.",
            agent="laptop",
            risk=RiskLevel.READ,
            reversible=True,
            requires_confirmation=False,
            parameters={"browser_session": "string"},
            required_parameters=[],
            tags=["browser", "read"],
        ),
        browser_close,
    ),
    (
        ToolSpec(
            name="browser_save",
            description=(
                "Read a web page and save what it says to a file. The path is "
                "guarded like any other write, and the file is read back to "
                "confirm it landed."
            ),
            agent="laptop",
            risk=RiskLevel.EXTERNAL,
            reversible=True,
            requires_confirmation=True,
            parameters={"url": "string", "path": "string"},
            required_parameters=["url", "path"],
            tags=["browser", "write"],
        ),
        browser_save,
    ),
]
