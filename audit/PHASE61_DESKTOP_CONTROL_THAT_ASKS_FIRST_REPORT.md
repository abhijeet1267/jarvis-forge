# Phase 61 — desktop control that asks, and only then touches

| | |
|---|---|
| Phase | 61 (Computer Use — the last item in the build prompt's §26 order) |
| Date | 2026-10-05 |
| Starting commit | `c922a26` (Phase 60 close-out) |
| Ending commit | `ddb52df` (the work) → the close-out commit named in §12 |
| Tests before | 2355 passed · 0 failed · 0 skipped |
| Tests after | **2380 passed · 0 failed** (76.5 s; one arXiv-gated test skipped on one of four runs) |
| Tools / gated | 145 / 30 → **148 / 32** — three added, none loosened |
| Risk split | read 77 · low 38 · external 15 · destructive 12 · physical 3 → **read 78 · low 38 · external 15 · destructive 14 · physical 3** |
| Planner rules | 52 → **52** (one new branch inside an existing rule, not a new rule) |
| Host HTTP surface | 122 route objects / 115 paths → **unchanged** |
| Real actuation | **DELIBERATELY NOT DEMONSTRATED** — see §7 |
| Push status | **nothing pushed; local commits only** |

---

## 1. What was measured before anything was written

The register said *"No Computer Use capability — §26 not started"*. Half of that is wrong in the
same shape as Phase 60's row: the **eyes** already shipped and the **hands** did not.

```
existing, READ-tier:   capture_screen · take_screenshot · get_frontmost_application ·
                       get_running_applications · vision_capabilities · the OCR tools
existing, actuation:   none. grep over jarvis/tools and jarvis/agents for a click, a keystroke
                       or a mouse event returns only comments promising the opposite:
                         tools/vision.py:5  "There is no action tool in this module: nothing
                                             here clicks, types, moves or delete[s]"
                         agents/vision_agent.py:8  "...cannot click, type, move or delete"
already named for it:  permissions/service.py:122  Capability("accessibility",
                       required_for="Advanced desktop control: reading windows, clicking, typing")
```

So the permission vocabulary for this feature was written down two phases ago and nothing used it.
The gap was the hands, and hands are where the spec's security model actually bites.

## 2. §26.2 decided the design, not the feature list

> Untrusted model output is data. It is never authority.

A Computer Use demo is exactly the thing that rule exists to catch: whatever text was last on the
screen — a webpage, a PDF, an email — becomes the hand that moves the cursor. So:

* **`click_desktop` and `type_desktop_text` are `DESTRUCTIVE` and confirmation-gated, and
  `reversible=False`.** A click lands on whatever is focused in the user's session; there is no
  undo, and "accidental destructive actions" is a named threat on §26.1. Every DESTRUCTIVE tool in
  this registry is gated (measured invariant since Phase 22) and these two join it without
  bending it — asserted against the live registry, not against a list.
* **They carry no `examples`.** Examples are how the router learns what a tool is for. A test keeps
  the field empty, and another sweeps a dozen sentences — "click at 100 200", "click my save
  button", "type hello world", "press the button at 5 5" — and asserts none of them resolve to an
  actuation. The gate is the card a person approves, not a parsing accident.
* **`get_desktop_control_state` is `READ` and *is* routed**, because Phase 28's rule still holds: a
  capability that exists must be askable about. "can you control my computer", "can you click on my
  screen", "do you have accessibility permission" and "is desktop control available" all reach the
  read.

## 3. The permission is consulted before anything leaves the process

Three states refuse, and each refusal is the permission's own words, not a generic error:
`DENIED`, `REQUIRES_MANUAL_SYSTEM_SETTINGS`, `NOT_DETERMINED`/`UNKNOWN`. An unreadable permission is
**not** a granted one — that is the failure mode this whole project is built on (a confident answer
to the wrong question). With a recorder on the sender, the tests prove the call count is zero for
every refusal path, so "we did not touch anything" is a measurement rather than a claim.

Coordinates and text are checked first too: a non-integer, a negative, anything above
`MAX_COORDINATE`, empty text or more than `MAX_TEXT_CHARS` all fail with **no** `osascript` call
made.

## 4. Injection, on the surface where it would be worst

AppleScript string concatenation is this platform's shell injection. The three scripts are
`on run argv` programs that read their values from arguments — `keystroke (item 1 of argv)` cannot
change the program being run, while `keystroke "…"` built by interpolation can. A test sends the
payload `" & do shell script "rm -rf ~` and asserts it appears in `args`, never in the script
source, **and carries a negative control**: the same test builds the naive interpolated form and
asserts the payload *does* appear in it, so the assertion is known to be able to fail rather than
vacuously true.

## 5. Success is only what a re-read observed

`click_desktop` reads the frontmost application before and after. If the after-read fails or
returns something that is not a plausible single line, the answer is **UNVERIFIED** (`Verification.UNKNOWN`)
with the reason — not "clicked". The bound is not fussiness: `osascript` can write errors to stdout
with a zero exit, and a whole error block would otherwise be reported as the name of the application
the click landed on. That case was found by the live proof's own recorder (see §7), which answered
`0, "not sent: this recorder never runs anything"` and was happily consumed as a window title.

`type_desktop_text` is honest about the weaker case: the characters go into a field Vantage cannot
see into, so even a successful send is reported **unverified**. The §26.1 threat list calls the
alternative "fake success messages".

## 6. The debt ledger moved, on purpose

Phase 28 pins how many capabilities a sentence cannot reach — `assert len(unreachable) == 28` — with
a docstring saying it moves only when someone changes it deliberately. This phase moved it to 30,
and the assertion now **names the two additions and states why they are there**:

```python
assert {"click_desktop", "type_desktop_text"} <= set(unreachable), \
    "the actuations are unreachable on purpose; if they stopped being so, that is a design change"
assert "get_desktop_control_state" not in unreachable, \
    "the read must be reachable by a sentence -- that is the part Phase 28 was about"
assert len(unreachable) == 30, unreachable
```

A frozen total that lists its own exceptions is a ledger; one that does not is a number waiting to
be bumped.

## 7. Live, on an isolated home — and what was deliberately not demonstrated

Isolated `JARVIS_HOME`, port 8920, the real permission reader, the real planner.

```
POST /command "can you control my computer"  -> 200 intent=desktop_control_state
                                               actions=['get_desktop_control_state']
                                               "Desktop control is available; the focused
                                                application is Qoder."
POST /command "click at 100 200"             -> 200 actions=[] needs_confirmation=False
                                               "I understood the words but not the goal yet. …"
accessibility, measured in that process      -> GRANTED (answered_by pid 85185)
click/type with the sender replaced by a recorder
                                             -> 5 attempted sends, 0 events on the desktop
registry                                   -> click_desktop DESTRUCTIVE/confirm/non-reversible,
                                              type_desktop_text DESTRUCTIVE/confirm/non-reversible,
                                              get_desktop_control_state READ
```

Three findings worth stating plainly, because two of them contradict what this phase first assumed:

1. **The permission is not the gate on this machine.** Measured: Accessibility is `GRANTED` to the
   python process launched from a terminal, while the delivered `.app` reports
   `REQUIRES_MANUAL_SYSTEM_SETTINGS` for itself (read from `/permissions` earlier today). That is
   Phase 50's subject rule again — the answer belongs to a process, not to a product — and it means
   the thing actually standing between a sentence and a cursor is **the confirmation card**. The
   module docstring says this instead of the comforting version that was written first.
2. **The recorder was needed, and it changed what could be proven.** Because the permission is
   granted to this process, a live proof that ran the real sender would have moved the cursor in the
   user's session. So the proof wraps `_osascript` with a function that records and never runs, and
   what it measures is *the code's decision to send*, not an effect. Five decisions, zero events.
3. **That harness exposed a real weakness in the handler**, which is why the fix is in the code and
   not in the proof: the re-read trusted any string as an application name, so a canned
   `0, "not sent…"` became "…is the application in front now". §5 describes the bound that replaced
   it.

**No click and no keystroke has ever been executed by this code.** That is a choice, not a blocker:
demonstrating it means driving the user's own pointer inside their applications, which is
irreversible and is theirs to ask for. `BLOCKED_BY_USER_DECISION` would be the wrong label — nothing
is blocking it — the honest statement is *not demonstrated, deliberately*.

## 8. Tests — 25, in `tests/test_phase61_desktop_control_that_asks_before_it_touches.py`

tier and gate (2) · no examples on the actuations (1) · the registry-wide invariant still holds (1) ·
a sentence cannot put a click in the plan (1) · two routing sweeps over twelve phrasings (2) ·
permission refusals send nothing, in three states (3) · argument validation reaches nothing (7) ·
injection discipline with a negative control (1) · re-read verification, four ways (4) ·
the read tool, three states (3) · registration (1).

## 9. Browser verification: NOT APPLICABLE

No page changed. Nothing in the dashboard or phone client offers a click or a keystroke; the
capability is reachable only through the pipeline and its approval card. If a surface is ever added,
the phone is the wrong place for it first (58-B: a phone can already approve its own gated actions),
and that ordering is a decision to make in the open. Recorded as 61-C.

## 10. Numbers, re-measured

| | before (Phase 60 close) | after |
|---|---|---|
| Tests | 2355 · 0 failed | **2380 · 0 failed** (76.5 s) |
| Test modules | 87 | **88** |
| Tools / gated | 145 / 30 | **148 / 32** |
| Risk split | read 77 · low 38 · external 15 · destructive 12 · physical 3 | **read 78 · low 38 · external 15 · destructive 14 · physical 3** |
| Planner rules | 52 | **52** |
| Host HTTP routes / paths | 122 / 115 | **122 / 115** |
| Versioned lines | 74,233 | **74,892** |
| Shipped code / tests | 43,871 / 29,761 | **44,161 / 30,130** |
| `audit/` reports | 62 | **63** (this file) |

Four full-suite runs were made during this phase. Two carried the documented Vision-OCR load flake
(5 and 6 failures in `tests/test_vision.py`, all passing on a quiet re-run), one showed a single
arXiv network-gated skip, and the final clean run is the number above. The flake is reported as
measured, not attributed to this phase's code and not hidden.

## 11. Open items

- **61-A — the read reports a process's permission, and a person reads it as the product's.**
  `get_desktop_control_state` answers from the helper for *this* process; the app and a
  terminal-launched host disagree. The message should name which subject was asked, the way the
  permission card does since Phase 50.
- **61-B — the actuations are unreachable from a sentence by design, which makes them reachable
  only from a caller that builds an `Action` directly.** Nothing in the shipped product does that
  yet, so as delivered they are gated code with no front door. Whether one is ever given a door —
  and what it would look like (a coordinate picker on a screenshot? a named UI element?) — is a
  design decision the user owns, not a gap to close silently.
- **61-C — no surface shows or asks for desktop control**, deliberately, and 58-B (a phone can
  approve its own gated actions) has to be closed before any remote surface can.
- **61-D — `System Events`' `click at {x, y}` has never been executed here.** The syntax is what
  the Apple Scripting Dictionary describes; the only honest verification is a real send, which §7
  declines to perform. Stated rather than implied by a green suite.
- **61-E — a keystroke can never be verified from here.** Reported as unverified today; a real
  answer needs an accessibility-API read of the focused text element.

## 12. Delivery

Work commit `ddb52df` — *Give the laptop hands, and keep them behind the confirmation card*.
Close-out commit `—`: this file is inside that commit, so its hash is written by the next edit to
this line rather than guessed now.

Rebuilt from this tree, redeployed, verified with **GETs only** — no POST and no actuation reached
the user's real data directory, and no click was ever sent:

| check | result |
|---|---|
| `codesign --verify --deep --strict` on the deployed bundle | valid on disk, satisfies its Designated Requirement |
| `jarvis/tools/desktop.py` present inside the bundle and inside `Vantage.app.zip` after extraction | both, and the extracted copy re-verifies |
| `GET /health` on the delivered app | `status ok`, **148 tools**, 15 agents, scheduler running with a 30 s tick, `llm_enabled: false`, `ai_provider: openai` |
| `GET /tools` | **148** entries; `get_desktop_control_state` read/ungated, `click_desktop` and `type_desktop_text` destructive + `confirmation=True` — the gate is visible in what the shipped server publishes, not only in the source |
| `GET /tools` and `GET /health` with no token | **401** |
| the claim the shipped build wrote | `{"pid": 89131, "host": "0.0.0.0", "port": 8765, …}` |
| archives | `Vantage.app.zip` 1,172,479 B and `vantage-source.zip` 2,540,639 B, both rebuilt; the source archive scans clean for `.env`, tokens, key files and databases |
| suite | **2380 passed · 0 failed**, measured twice clean (83.1 s and 76.4 s). Earlier runs in the same phase showed the documented Vision-OCR load flake (5 and 6 failures) and one arXiv network-gated skip; both are reported as what they measured, not folded into the headline |

`READ ME FIRST.txt` gained section 8, which says what the hands are, that no sentence reaches them,
that the permission answer belongs to a process rather than to the product (and that this machine
grants one and refuses the other), and that **no click or keystroke has ever been executed by this
code**. Still hand-maintained in the delivery folder rather than generated (56-F).

Nothing was pushed.
