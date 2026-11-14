"""Phase 63: a surface has to say who it is talking about.

Two open items, one theme, and the theme is Phase 50's rule applied to the two places that grew
after it.

**60-C** — the panel's model badge said `cloud` or `rules`. After Phase 60 there are two vendors
behind that word and the badge named neither, which is the smaller twin of the bug that phase
removed: a status element implying more precision than the system has. It now names the vendor
when one is configured, and stays on `rules` when none is — because printing a vendor nobody will
call is exactly the badge lie Phase 60 measured.

**61-A** — `get_desktop_control_state` answers whether *this process* may click, and macOS
attributes that to the responsible process, not to the product. Measured during Phase 61: the same
question answered `GRANTED` for a terminal-launched host and
`REQUIRES_MANUAL_SYSTEM_SETTINGS` for Vantage.app. The tool reported the state without saying
whose state it was, so a person reading "Desktop control is available" about a terminal process
would believe it about the app in their Dock. The permission service has carried an answer to this
since Phase 50 (`answered_by`, `about_bundled_host`, `status_note`); the desktop read now uses it.

The page assertions run the code actually shipped in `dashboard/index.html`, under node, in the
same style as Phases 14/26 — a source grep would pass on a function that never runs.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from jarvis.tools import desktop
from jarvis.tools.base import ToolContext

REPO = Path(__file__).resolve().parents[1]
node = shutil.which("node")


# ---------------------------------------------------------------------------
# 60-C — the badge in the shipped panel
# ---------------------------------------------------------------------------


def _function_extent(text: str, start: int) -> str:
    """Extract from `start` to the matching closing brace, at whatever indent the
    page actually uses. The old `.index("\n}")` assumed the closing brace sits at
    column 0, which the current formatting of the page no longer guarantees."""
    brace = text.index("{", start)
    depth = 0
    for j in range(brace, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
    raise ValueError("no matching closing brace")

@pytest.fixture(scope="module")
def badge(tmp_path_factory):
    """Run the shipped `applyHealth` and report the text it writes into #llm."""
    if not node:
        pytest.skip("node not available to run the page's own code")
    body = re.search(r"<script>(.*?)</script>",
                     (REPO / "dashboard" / "index.html").read_text(), re.S).group(1)
    start = body.index("function applyHealth")
    source = _function_extent(body, start)
    assert "llm_enabled" in source, "the extracted function is not the model badge"

    root = tmp_path_factory.mktemp("p63")

    def _run(health):
        script = root / "badge.js"
        script.write_text(
            "var text = null;\n"
            "global.document = { getElementById: () => ({ set textContent(v) { text = v; },"
            " get textContent() { return text; } }) };\n"
            + "eval(" + json.dumps(source) + ");\n"
            + "applyHealth(" + json.dumps(health) + ");\n"
            + "console.log(document.getElementById('llm').textContent);\n")
        proc = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr[:400]
        return proc.stdout.strip()

    return _run


def test_the_badge_names_the_vendor_a_key_would_reach(badge):
    """`llm_enabled: true` plus a selected provider is a fact about a specific vendor."""
    said = badge({"llm_enabled": True, "llm_source": "llm.env", "ai_provider": "gemini",
                  "tools": 148})
    assert "gemini" in said, said
    assert "cloud" not in said, f"the vague word is still the claim: {said}"


def test_the_badge_refuses_to_name_a_vendor_that_will_never_be_called(badge):
    """No key means rules answer. Printing the configured-by-default vendor would claim a
    call path that does not exist -- the mistake Phase 60 was created to remove."""
    said = badge({"llm_enabled": False, "llm_source": "none", "ai_provider": "openai",
                  "tools": 148})
    assert "rules" in said, said
    assert "openai" not in said and "gemini" not in said, said


def test_the_badge_still_counts_the_capabilities_it_always_showed(badge):
    said = badge({"llm_enabled": True, "llm_source": "env", "ai_provider": "openai",
                  "tools": 148})
    assert "148" in said, said


def test_the_served_health_carries_what_the_badge_needs():
    """The badge is only honest if the server publishes the field it reads. Cheap to assert and
    exactly the drift that turns a UI change into a decoration."""
    import importlib

    from fastapi.testclient import TestClient

    from tests.conftest import AUTH_HEADERS

    api = importlib.import_module("jarvis.api.app")
    with TestClient(api.app, base_url="http://127.0.0.1:8765", headers=AUTH_HEADERS) as client:
        health = client.get("/health").json()
    for key in ("llm_enabled", "llm_source", "ai_provider", "tools"):
        assert key in health, (key, sorted(health))


# ---------------------------------------------------------------------------
# 61-A — whose permission is it, actually
# ---------------------------------------------------------------------------


def _answer(monkeypatch, row):
    monkeypatch.setattr(desktop, "_accessibility", lambda ctx: row, raising=False)
    monkeypatch.setattr(desktop, "_osascript",
                        lambda script, args, timeout=8.0: (0, "Notes"), raising=False)


def test_a_granted_answer_says_which_process_it_describes(monkeypatch):
    """Measured in Phase 61: GRANTED for a terminal-launched host, refused for the app.

    The row already knows the difference; what was missing was saying it in the sentence a person
    reads, so "available" is never silently about one process while being read about another.
    """
    import asyncio

    _answer(monkeypatch, {
        "status": "GRANTED", "about_bundled_host": False,
        "status_note": "read by code running at /Users/you/jarvis-forge, which is not inside "
                       "the Vantage app bundle",
    })

    result = asyncio.run(desktop.get_desktop_control_state(ToolContext()))

    assert result.ok is True
    assert result.output["about_bundled_host"] is False
    assert "not the app" in result.message.lower() or "terminal" in result.message.lower(), \
        result.message
    assert "Notes" in result.message, result.message


def test_a_bundled_answer_says_so_too(monkeypatch):
    """The positive case has to be named as well, or the caveat reads like a permanent apology."""
    import asyncio

    _answer(monkeypatch, {
        "status": "GRANTED", "about_bundled_host": True,
        "status_note": "read by Vantage running from /Applications/Vantage.app",
    })

    result = asyncio.run(desktop.get_desktop_control_state(ToolContext()))

    assert "Vantage.app" in result.message or "the app" in result.message.lower(), result.message


def test_a_refusal_names_the_same_subject(monkeypatch):
    """The other direction matters more: "not available" answered by a non-bundled process is not
    a statement about the app the user has to grant."""
    import asyncio

    _answer(monkeypatch, {
        "status": "REQUIRES_MANUAL_SYSTEM_SETTINGS", "about_bundled_host": False,
        "status_note": "read by code running outside the app bundle",
    })

    result = asyncio.run(desktop.get_desktop_control_state(ToolContext()))

    assert result.ok is True, "reading a state is a read, not a failure"
    assert result.output["can_control"] is False
    assert "Accessibility" in result.message
    assert result.output.get("status_note"), result.output


def test_the_subject_is_reported_even_when_the_reader_fails(monkeypatch):
    """A grant that cannot confirm itself is unverified -- and still has to say who it was about.

    The three assertions belong in one test because they are one claim: the subject is metadata on
    the answer, not decoration on the happy path.
    """
    import asyncio

    monkeypatch.setattr(desktop, "_accessibility", lambda ctx: {
        "status": "GRANTED", "about_bundled_host": False, "status_note": "outside the bundle"},
        raising=False)
    monkeypatch.setattr(desktop, "_osascript",
                        lambda script, args, timeout=8.0: (1, ""), raising=False)

    result = asyncio.run(desktop.get_desktop_control_state(ToolContext()))

    # `unverified` is the established shape: the call completed, and what is unknown is whether
    # the world changed. Asserting ok=False here would invent a second vocabulary for it.
    assert result.verified is False, result.verified
    assert result.output["about_bundled_host"] is False
    assert "not the app in your Dock" in result.message, result.message
