"""Browser Controller for Controlled Browser Automation (Phase 92).

Provides a single authority for browser actions with:
- Session isolation per Vantage session/device/surface
- Navigation verification (before/after observation)
- Click/type/select operations with state verification
- Origin/security restrictions
- Prompt-injection-resistant handling of page content

Every action follows: OBSERVE BEFORE → EXECUTE → OBSERVE AFTER → VERIFY → REPORT
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Playwright is optional at import time.
#
# It used to be a hard import, which made this module impossible to import on
# a machine without it -- so the rules about what a page's text means, and the
# code that reads a page, could not be tested anywhere but a machine with
# Playwright installed. A missing optional dependency must be a *reported*
# condition, never an import error that takes the whole module with it.
try:  # pragma: no cover - the branch depends on the machine
    from playwright.async_api import Page, Playwright, async_playwright

    PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover - the branch depends on the machine
    Page = Any  # type: ignore[assignment,misc]
    Playwright = Any  # type: ignore[assignment,misc]
    async_playwright = None  # type: ignore[assignment]
    PLAYWRIGHT_AVAILABLE = False

from ..config import Settings, get_settings
from ..core.models import ActionResult, Verification
from ..logging_setup import get_logger
from .base import ToolContext, fail, ok, unverified
from .page_content import label_untrusted, strip_control_characters

log = get_logger("tools.browser_controller")

#: Why the controller cannot run, when it cannot. Reported rather than
#: raised, so a caller can say "Playwright is not installed" instead of
#: crashing on an import.
PLAYWRIGHT_MISSING = (
    "Browser automation needs Playwright, and it is not installed on this "
    "machine. Install it with: pip install playwright && playwright install chromium"
)

# Singleton instance registry
_BROWSER_CONTROLLERS: Dict[str, "BrowserController"] = {}


@dataclass
class BrowserSession:
    """Represents an isolated browser session."""
    
    session_id: str
    owner_session: Optional[str] = None  # Vantage session ID
    surface: Optional[str] = None  # voice | api | dashboard | phone
    device: Optional[str] = None  # laptop_main | phone_*
    created_at: datetime = field(default_factory=datetime.now)
    browser_type: str = "chromium"
    context: Any = None  # Playwright browser context
    page: Optional[Page] = None
    #: The browser this session's context belongs to. Tracked so closing the
    #: session can close the browser too -- a context closed under a browser
    #: that is still running leaves a Chromium process alive, which is a leak
    #: the product pays for on every page it reads.
    browser: Any = None
    current_url: str = ""
    title: str = ""
    status: str = "created"  # created | ready | navigating | active | closed
    
    def as_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "owner_session": self.owner_session,
            "surface": self.surface,
            "device": self.device,
            "browser_type": self.browser_type,
            "current_url": self.current_url,
            "title": self.title,
            "status": self.status,
        }


class BrowserController:
    """Controlled browser automation backend with session isolation."""
    
    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._playwright: Optional[Playwright] = None
        self._sessions: Dict[str, BrowserSession] = {}
        self._background_tasks: List[asyncio.Task] = []
        
    def latest_session_for(self, owner_session: str) -> Optional[str]:
        """The most recent live session this Vantage conversation owns.

        Browser tools are usable one at a time ("open this page", then "read
        it"), and the person should not have to paste a session id between them.
        Only a session the same conversation created is returned -- another
        conversation's browser is not this one's to read.
        """
        if not owner_session:
            return None
        owned = [s for s in self._sessions.values()
                 if getattr(s, "owner_session", None) == owner_session]
        if not owned:
            return None
        owned.sort(key=lambda s: s.created_at, reverse=True)
        return owned[0].session_id

    async def _ensure_playwright(self) -> Playwright:
        """Lazy-load Playwright instance.

        Raises ``RuntimeError`` with an actionable message when Playwright is
        not installed. A missing optional dependency is a condition to report,
        not an import error that takes the whole module with it.
        """
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(PLAYWRIGHT_MISSING)
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        return self._playwright

    @staticmethod
    def available() -> bool:
        """Whether browser automation can run on this machine."""
        return PLAYWRIGHT_AVAILABLE
    
    async def create_session(
        self,
        owner_session: Optional[str] = None,
        surface: Optional[str] = None,
        device: Optional[str] = None,
        headless: bool = True,
    ) -> BrowserSession:
        """Create an isolated browser session."""
        
        try:
            pw = await self._ensure_playwright()

            # Launch browser in headless mode for automation
            browser = await pw.chromium.launch(headless=headless)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 720},
                locale="en-US",
                timezone_id="America/New_York",
            )

            page = await context.new_page()

            # Set up navigation observer
            async def on_load(event):
                session = self._sessions.get(session_id)
                if session:
                    session.current_url = page.url
                    session.title = await page.title()
                    session.status = "ready"

            session_id = str(uuid.uuid4())
            session = BrowserSession(
                session_id=session_id,
                owner_session=owner_session,
                surface=surface,
                device=device,
                context=context,
                page=page,
                browser=browser,
                status="ready",
            )
            self._sessions[session_id] = session

            # Attach callback to page load
            page.on("load", on_load)

            log.info(f"Created browser session {session_id}", extra={
                "session_info": session.as_dict(),
            })

            return session

        except Exception as e:
            log.error(f"Failed to create browser session: {e}")
            raise
    
    async def navigate(self, session_id: str, url: str) -> ActionResult:
        """Navigate to URL with verification."""
        
        if session_id not in self._sessions:
            return fail("browser.navigate", f"Unknown session: {session_id}")
        
        session = self._sessions[session_id]
        page = session.page
        
        # Validate URL before navigation
        validated_url, error = self._validate_url(url)
        if error:
            return fail("browser.navigate", error)
        
        # Capture before state
        before_url = page.url
        
        try:
            # Navigate with timeout
            await page.goto(validated_url, wait_until="domcontentloaded", timeout=30000)
            
            # Wait for network idle to ensure page fully loaded
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass  # Continue even if network doesn't idle within timeout
            
            # Verify after state - capture actual URL
            after_url = page.url
            
            # Verify we navigated where expected
            if after_url != validated_url:
                return unverified(
                    "browser.navigate",
                    f"Navigated but actual URL differs from requested: expected {validated_url}, got {after_url}",
                    browser_session=session_id,
                    requested_url=validated_url,
                    actual_url=after_url,
                )
            
            # Get page title as additional verification
            title = await page.title()
            
            session.current_url = after_url
            session.title = title
            
            return ok(
                "browser.navigate",
                f"Navigated to {validated_url} (title: {title[:50]})",
                browser_session=session_id,
                verified=True,
                opened=validated_url,
                shown_url=after_url,
                title=title,
            )
            
        except Exception as e:
            return fail(
                "browser.navigate",
                f"Navigation failed to {url}: {str(e)[:200]}",
                browser_session=session_id,
                requested_url=url,
            )
    
    async def click(self, session_id: str, selector: str) -> ActionResult:
        """Click element with verification."""
        
        if session_id not in self._sessions:
            return fail("browser.click", f"Unknown session: {session_id}")
        
        session = self._sessions[session_id]
        page = session.page
        
        # Validate selector
        if not selector or len(selector) > 500:
            return fail("browser.click", "Invalid selector")
        
        # Safety check: reject dangerous selectors
        if any(danger in selector.lower() for danger in ["javascript:", "vbscript:", "onclick"]):
            return fail("browser.click", "Blocked suspicious selector")
        
        try:
            # Find element before clicking
            element = await page.query_selector(selector)
            if not element:
                return fail("browser.click", f"Element not found: {selector}")
            
            # Get element info before click
            element_text = await element.inner_text()
            element_visible = await element.is_visible()
            
            # Perform click
            await element.click(timeout=5000)
            
            # Wait briefly for any navigation or state change
            try:
                await asyncio.sleep(0.5)
            except Exception:
                pass
            
            # Verify after click
            try:
                still_exists = await element.is_attached()
            except Exception:
                still_exists = False
            
            return ok(
                "browser.click",
                f"Clicked element: {selector[:50]}",
                browser_session=session_id,
                selector=selector,
                verified=True,
                element_text_before=element_text,
                element_visible_before=element_visible,
                element_still_exists_after=still_exists,
            )
            
        except Exception as e:
            return fail(
                "browser.click",
                f"Click failed on {selector}: {str(e)[:200]}",
                browser_session=session_id,
                selector=selector,
            )
    
    async def type_text(self, session_id: str, selector: str, text: str) -> ActionResult:
        """Type text into input field with verification."""
        
        if session_id not in self._sessions:
            return fail("browser.type", f"Unknown session: {session_id}")
        
        session = self._sessions[session_id]
        page = session.page
        
        # Input validation
        if not selector:
            return fail("browser.type", "Missing selector")
        if not text:
            return fail("browser.type", "No text to type")
        if len(text) > 10000:
            return fail("browser.type", "Text too long")
        
        try:
            # Find element
            element = await page.query_selector(selector)
            if not element:
                return fail("browser.type", f"Element not found: {selector}")
            
            # Get existing value
            existing_value = await element.input_value()
            
            # Type text
            await element.fill(text, timeout=5000)
            
            # Wait for potential event handlers
            try:
                await asyncio.sleep(0.3)
            except Exception:
                pass
            
            # Verify by reading back
            typed_value = await element.input_value()
            
            if typed_value == text:
                return ok(
                    "browser.type",
                    f"Typed into {selector[:50]}",
                    browser_session=session_id,
                    selector=selector,
                    text=text,
                    verified=True,
                    existing_value=existing_value,
                    typed_value=typed_value,
                )
            else:
                return unverified(
                    "browser.type",
                    f"Typed but verification failed: expected {text}, got {typed_value}",
                    browser_session=session_id,
                    selector=selector,
                    expected=text,
                    observed=typed_value,
                )
                
        except Exception as e:
            return fail("browser.type", f"Type failed: {str(e)[:200]}")
    
    async def current_url(self, session_id: str) -> ActionResult:
        """Get current URL of session."""
        
        if session_id not in self._sessions:
            return fail("browser.current_url", f"Unknown session: {session_id}")
        
        session = self._sessions[session_id]
        
        return ok(
            "browser.current_url",
            f"Current URL: {session.current_url}",
            browser_session=session_id,
            url=session.current_url,
        )
    
    async def page_title(self, session_id: str) -> ActionResult:
        """Get page title."""

        if session_id not in self._sessions:
            return fail("browser.page_title", f"Unknown session: {session_id}")

        session = self._sessions[session_id]
        page = session.page

        try:
            title = await page.title()
            return ok(
                "browser.page_title",
                f"Title: {title}",
                browser_session=session_id,
                title=title,
            )
        except Exception as e:
            return fail("browser.page_title", f"Failed to get title: {e}")

    async def read_page(self, session_id: str, max_chars: int = 4000) -> ActionResult:
        """Read the visible text of the current page.

        This is the read the whole browser slice is missing: without it the
        controller can open a page, click a button and type into a field, but
        there is no way to find out what the page *said*. "Open a URL, read
        the title and content" is the slice the roadmap asks for, and the
        title alone is not content.

        **The page is untrusted input.** A web page can say anything, including
        text shaped like an instruction to the assistant. So the content is
        returned as *data* — in its own field, clearly labelled, and with
        anything that looks like an instruction to the model marked as such
        rather than passed on as if it were the user's own words.

        The text is observed, not inferred: it comes from the rendered
        ``<body>``, and the result says which URL it came from, so a caller
        can tell what was read.
        """
        if session_id not in self._sessions:
            return fail("browser.read_page", f"Unknown session: {session_id}")

        session = self._sessions[session_id]
        page = session.page

        try:
            # The rendered text of the page, not the raw HTML: a page that
            # renders "Hello" should read as "Hello", not as a script tag.
            raw = await page.inner_text("body")
        except Exception as exc:
            return fail("browser.read_page", f"Could not read the page: {exc}")

        # Format characters come out first: they are how a page hides text
        # from the person looking at it.
        text = strip_control_characters(raw or "").strip()
        if not text:
            return ok(
                "browser.read_page",
                "The page has no visible text.",
                browser_session=session_id,
                url=page.url,
                text="",
                truncated=False,
                untrusted=True,
                safe_for_model=True,
            )

        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]

        # The page is untrusted input, and it is marked as such. A page that
        # reads like instructions is flagged, so a caller cannot hand it to a
        # model as if it were the user's own words.
        labelled = label_untrusted(text)

        return ok(
            "browser.read_page",
            f"Read {len(text)} characters of page text.",
            browser_session=session_id,
            url=page.url,
            title=session.title,
            text=labelled["text"],
            truncated=truncated,
            # The page said this, not the user. A caller that feeds it to a
            # model needs to be able to say so.
            source="web_page",
            untrusted=True,
            looks_like_instruction=labelled["looks_like_instruction"],
            instruction_markers=labelled["instruction_markers"],
            safe_for_model=labelled["safe_for_model"],
        )
    
    async def screenshot(self, session_id: str) -> ActionResult:
        """Capture screenshot of current page."""
        
        if session_id not in self._sessions:
            return fail("browser.screenshot", f"Unknown session: {session_id}")
        
        session = self._sessions[session_id]
        
        try:
            path = self.settings.voice_path / "captures" / f"screenshot_{uuid.uuid4()}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            
            await session.page.screenshot(path=str(path), full_page=False)
            
            return ok(
                "browser.screenshot",
                f"Captured screenshot",
                browser_session=session_id,
                screenshot_path=str(path),
            )
        except Exception as e:
            return fail("browser.screenshot", f"Screenshot failed: {e}")
    
    async def close_session(self, session_id: str) -> ActionResult:
        """Close a browser session.

        The browser is closed with the context. Closing only the context
        leaves the Chromium process running under it, so every page the
        product read left a browser behind it.
        """

        if session_id not in self._sessions:
            return fail("browser.close", f"Unknown session: {session_id}")

        session = self._sessions[session_id]
        session.status = "closed"

        try:
            if session.context:
                await session.context.close()
            if session.page:
                await session.page.close()
        except Exception as e:
            log.warning(f"Error closing session {session_id}: {e}")

        try:
            if session.browser:
                await session.browser.close()
        except Exception as e:
            log.warning(f"Error closing browser for {session_id}: {e}")

        del self._sessions[session_id]

        # The last session going closes the driver too. A Playwright driver
        # left running holds a node process alive for as long as the program
        # that started it lives -- so a tool that opened one page and closed
        # it would still keep the process from exiting.
        if not self._sessions and self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as e:
                log.warning(f"Error stopping playwright: {e}")
            self._playwright = None

        return ok("browser.close", f"Closed session {session_id}")
    
    def _validate_url(self, url: str) -> tuple:
        """Validate URL. Returns (validated_url, error).

        The controller had its own, much weaker check: anything that did not
        start with ``http://`` or ``https://`` was prefixed with ``https://``,
        and the ``javascript:`` guard below it was unreachable because such a
        URL never reached it. So ``file:///etc/passwd`` became
        ``https://file:///etc/passwd`` -- a nonsense host rather than a refusal
        -- and a ``javascript:`` URL was passed through the same way.

        The sibling module ``jarvis/tools/browser.py`` already has the right
        rule (a known address, a bare domain upgraded to https, anything else
        refused rather than guessed), and it is the one that was ever
        reachable from the product. The controller now uses the same rule, so
        there is one answer to "is this a URL I may open" rather than two that
        disagree.
        """
        from .browser import normalise

        validated, problem = normalise(url)
        if not validated:
            return "", problem
        return validated, ""
    
    async def shutdown(self) -> None:
        """Shutdown controller and all sessions."""
        
        # Close all sessions
        for session_id in list(self._sessions.keys()):
            await self.close_session(session_id)
        
        # Cancel background tasks
        for task in self._background_tasks:
            task.cancel()
        
        # Stop Playwright
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        
        log.info("BrowserController shut down")
    
    @classmethod
    def get_instance(cls) -> BrowserController:
        """Get singleton instance."""
        # For now, create new instance - will be refactored to proper singleton later
        import threading
        if not hasattr(cls, "_instance"):
            cls._instance = cls()
        return cls._instance
    
    @classmethod
    def set_instance(cls, instance: BrowserController) -> None:
        """Set singleton instance."""
        cls._instance = instance


SPECS = []  # BrowserController actions registered separately from open_url


def describe_browser_controller() -> Dict[str, Any]:
    """Describe browser controller status."""
    
    instances = BrowserController.__dict__.get("_instance")
    if instances:
        num_sessions = len(instances._sessions) if hasattr(instances, '_sessions') else 0
        return {
            "available": True,
            "backend": "playwright",
            "sessions_active": num_sessions,
            "note": "Real browser control available via BrowserController",
        }
    
    # "not initialized yet" and "not installed" are different facts, and this
    # answered the second when it meant the first: `available: False` was read as
    # "browser automation is unavailable" in the status doc and the scores table,
    # while Playwright was installed and working the whole time.
    from .browser_controller import PLAYWRIGHT_AVAILABLE

    return {
        "available": bool(PLAYWRIGHT_AVAILABLE),
        "backend": "playwright" if PLAYWRIGHT_AVAILABLE else "none",
        "sessions_active": 0,
        "initialized": False,
        "note": ("Playwright is installed; no browser session has been created yet."
                 if PLAYWRIGHT_AVAILABLE else
                 "Playwright is not installed, so browser control is unavailable."),
    }
