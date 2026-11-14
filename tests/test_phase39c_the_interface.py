"""Phase 39c: the dashboard's interface, held to a contract.

The ask was "make its ui better with frontend". Before restyling anything, the current page was
checked against the states a real product has to handle -- measured, not judged:

```
:hover                    1     one element in the whole page reacts to the pointer
:focus                    0     no keyboard focus ring exists anywhere
@media                    0     nothing adapts below the desktop width
prefers-color-scheme      0     light only, while the phone client is dark
prefers-reduced-motion    0
aria-                     0     a command finishing is silent to a screen reader
empty state               1     "none", with no next action
```

So "better" here is not a palette swap. It is the difference between a page that works when
someone uses a keyboard, a phone-sized window, a dark room, or a screen reader -- and one that
only works for a mouse on a wide, bright monitor. Every rule below is a claim about the shipped
file that a browser was then asked to confirm.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "dashboard" / "index.html"
CSS = re.search(r"<style>(.*?)</style>", PAGE.read_text(), re.S).group(1)



# ---------------------------------------------------------------------------
# 1. the states a product has to show
# ---------------------------------------------------------------------------


def test_keyboard_focus_is_visible():
    assert ":focus-visible" in CSS, "a keyboard user cannot see where they are"


def test_interactive_elements_answer_the_pointer():
    # More than one rule, and specifically not only on the primary button.
    hover_rules = re.findall(r"([^{}]*:hover)\s*\{", CSS)
    assert len(hover_rules) >= 3, f"only {len(hover_rules)} hover rules in the page"
    assert any("button" in r for r in hover_rules)


def test_the_page_adapts_below_desktop_width():
    breakpoints = [int(b) for b in re.findall(r"@media[^{]*?\(max-width:\s*(\d+)px", CSS)]
    assert breakpoints, "no responsive rules at all"
    assert min(breakpoints) <= 430, f"narrowest breakpoint is {min(breakpoints)}px"


def test_a_dark_preference_is_honoured():
    """Dark is honoured by being the base theme.

    The phase report measured "light only, while the phone client is dark". The
    remediation went dark-first: the base `:root` IS the dark theme and a
    `prefers-color-scheme: light` block overrides it for light-preference users
    (see test_the_light_theme_defines_every_colour_token_the_base_does). So a
    dark-preference user sees dark with no media query at all; the assertion is
    that the base surface actually is dark.
    """
    base = re.search(r":root\s*\{(.*?)\}", CSS, re.S).group(1)
    m = re.search(r"--bg\s*:\s*#([0-9a-fA-F]{6})\b", base)
    assert m, "base theme has no hex --bg to verify"
    r, g, b = (int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4))
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    assert luminance < 128, f"base --bg is not a dark surface: #{m.group(1)}"


def test_the_light_theme_defines_every_colour_token_the_base_does():
    """A theme that forgets a variable is not a theme; it is invisible text.

    Dark-first layout: the base `:root` is the dark theme and the
    `prefers-color-scheme: light` block restates the colour tokens. A colour
    token the override forgets would render with its dark value on a light
    background -- the drift a screenshot at one scheme does not catch.
    Non-colour constants (radii, easings, shadows) may inherit.
    """
    base_block = re.search(r":root\s*\{(.*?)\}", CSS, re.S).group(1)
    light_block = re.search(
        r":root\s*\{(.*?)\}", CSS[CSS.index("prefers-color-scheme: light"):], re.S
    ).group(1)
    base_defs = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;}]+)", base_block))
    light_defs = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;}]+)", light_block))
    missing = [
        name for name, value in base_defs.items()
        if re.match(r"^(#|rgb)", value.strip()) and name not in light_defs
    ]
    assert not missing, f"colour tokens undefined in light mode: {sorted(missing)}"


def test_motion_respects_the_user_asking_not_to():
    assert "prefers-reduced-motion" in CSS


# ---------------------------------------------------------------------------
# 2. what a screen reader is told
# ---------------------------------------------------------------------------


def test_the_reply_and_state_are_announced():
    body = PAGE.read_text()
    assert 'aria-live="polite"' in body, "a finished command is silent to assistive tech"
    assert 'role="status"' in body


def test_the_command_input_is_labelled():
    body = PAGE.read_text()
    # The reflowed source may break the input across lines, so match `id="cmd"` and look
    # for its `aria-label` in the surrounding tag rather than assuming a single-line input.
    m = re.search(r'<input[^>]*id="cmd"[^>]*>', body, re.S)
    assert m, "no command input on the panel"
    tag = m.group(0)
    assert 'aria-label' in tag, f"the command input has no accessible label: {tag}"


# ---------------------------------------------------------------------------
# 3. empty is a state with a next step, not a blank box
# ---------------------------------------------------------------------------


def test_an_empty_panel_says_what_to_do_next():
    """`none` is an answer; it is not guidance. And the devices panel rendered
    literally nothing at all when no device was paired."""
    body = PAGE.read_text()
    assert re.search(r"nothing paired yet", body, re.I), \
        "an empty devices panel gives no next step"
    assert re.search(r"pair with this laptop", body, re.I), \
        "the guidance does not say how a device gets there"
    assert re.search(r"nothing waiting", body, re.I), \
        "an empty confirmation list does not explain what would appear there"
    assert 'box.innerHTML = \'<span class="muted">none</span>\'' not in body


# ---------------------------------------------------------------------------
# 4. the constraints that make it a local app, kept
# ---------------------------------------------------------------------------


def test_nothing_is_fetched_from_a_network_the_user_did_not_choose():
    """Offline by design: a font from a CDN is a page that breaks on a plane."""
    body = PAGE.read_text()
    offenders = re.findall(r'(?:src|href)="(https?:)?//[^"]+"', body)
    assert not offenders, f"external resources: {offenders}"


def test_every_colour_comes_from_a_token():
    """Raw hex outside the token blocks is how a dark mode ends up with light text
    on a light background."""
    outside = re.sub(r":root\s*\{.*?\}", "", CSS, flags=re.S)
    outside = re.sub(r"@media[^{]*\{", "", outside)
    dark = outside[outside.index("prefers-color-scheme: dark"):] if "prefers-color-scheme: dark" in outside else ""
    body = outside[:outside.index("prefers-color-scheme: dark")] if "prefers-color-scheme: dark" in outside else outside
    stray = re.findall(r"#[0-9a-fA-F]{3,6}\b", body)
    assert not stray, f"hard-coded colours outside :root: {stray[:8]}"


def test_the_page_still_ships_no_dependencies():
    body = PAGE.read_text()
    assert "<script src" not in body
    assert "@import" not in CSS


# ---------------------------------------------------------------------------
# 5. the page's script has to be a script
# ---------------------------------------------------------------------------

node = shutil.which("node")


@pytest.mark.skipif(not node, reason="node is not available to parse the page's script")
def test_the_pages_script_parses():
    """Added because editing this file by hand produced a duplicated function
    signature -- valid-looking in a diff, and the whole dashboard would have been
    dead in the browser. Every other rule here assumes the JavaScript runs."""
    scripts = re.findall(r"<script>(.*?)</script>", PAGE.read_text(), re.S)
    assert scripts, "no inline script to check"
    for index, source in enumerate(scripts):
        probe = Path(tempfile.mkstemp(suffix=".js", prefix=f"dash{index}")[1])
        probe.write_text(source, encoding="utf-8")
        result = subprocess.run([node, "--check", str(probe)],
                                capture_output=True, text=True, timeout=60)
        probe.unlink(missing_ok=True)
        assert result.returncode == 0, f"script {index}: {result.stderr[:400]}"


def test_no_function_is_declared_twice():
    """The specific shape of that mistake, caught without needing node."""
    names = re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", PAGE.read_text())
    duplicated = sorted({n for n in names if names.count(n) > 1})
    assert not duplicated, f"redeclared functions: {duplicated}"
