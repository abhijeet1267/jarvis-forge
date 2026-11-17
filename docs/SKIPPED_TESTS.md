# Skipped tests — audited and reduced

**At revision `0b23014`:** 4611 passed, **20 skipped**, 0 failed in 539 s
(`.venv/bin/python -m pytest -q -p no:cacheprovider -rs`).

Before this audit the same command reported 30 skipped. Ten of those were tests
that had never executed, and lifting the skip found real breakage in every group.

## What was wrong with the skips, and what running found

### `test_phase93_desktop_control.py` — 7 tests, blanket `@pytest.mark.skip`

The reason string was `"macOS-only test"` on a file that only targets macOS. Seven
desktop-control tests had never run on the one platform they were written for.
Running them found three separate breakages:

- `from .agents import send_command` — a helper that no longer exists.
- `ToolContext(platform=..., token=...)` — neither is a field, so the context could
  not be constructed even with the skip lifted.
- `click_desktop(...)` and `get_desktop_control_state(...)` called without
  `await`, though both are coroutines. `.ok` on a coroutine object raised
  AttributeError, so those assertions were never evaluating a result.
- Two also passed `Settings` where `_accessibility` needs a `ToolContext`, which
  its own `except` swallowed into `UNKNOWN` — the same defect found in the wizard.

The permanent fix is `pytest.mark.skipif(sys.platform != "darwin", ...)`: skip
where macOS is absent, run where it is not. **All 8 now pass.**

### `test_phase94_wizard.py` — 3 tests, skipped for a requirement they lack

Reasoned as "requires actual system permissions", but they assert the *guidance
text* a permission reports — the System Settings path, the note on optional
permissions. That needs no permission.

Running them found `PermissionStatus` used but never imported. **21 pass, was 18.**

One assertion in this group was genuinely wrong rather than the code: a click
refused with AppleScript `-25200` (`errAEEventNotPermitted`) is a real permission
denial, and the test accepted only the literal word "authoriz".

## What remains skipped, and why it is honest

| Category | Count | Reason |
|---|---|---|
| Missing OS prerequisite | 5 | `tkinter` / `qrcode` not installed; `no Android SDK` |
| External credential | 1 | no real API key for the optional live provider test |
| Missing hardware | 2 | microphone permission and hardware; physical phone |
| Empty result set | 3 | `/projects`, `/auth/sessions`, pending confirmations all empty, so there is nothing to decode-check |
| Environment | 2 | requires an interactive desktop session |
| No model configured | 1 | no model endpoint on this machine |
| Companion cert | 1 | certificate not generated here |
| Browser layout | 1 | needs Playwright for layout verification |
| Playwright / voice / device | 4 | gated on prerequisites genuinely absent |

None of these masks a failure. Each names a prerequisite this machine does not
have, and the appropriate action is to leave it skipped rather than to install
something or weaken the test.

**Two worth enabling later** if their prerequisites are installed:

- `tkinter` (5 tests): `brew install python-tk@3.11`, then the GUI-scan tests run.
- `qrcode` (2 tests): `.venv/bin/pip install "qrcode[pil]"`.

Neither is required for the product to work; they cover optional UI paths.

## Method

The list was produced by running the full suite with `-rs` and grouping the
`SKIPPED` lines by file and reason, then reading each skipped test to decide
whether the stated prerequisite was real. No test was converted to a pass, and no
assertion was weakened — where a skip was lifted, the test had to genuinely pass.