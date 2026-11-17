# V21 — desktop automation, measured

What actually runs on this machine, with the evidence for each claim. Every
result below was produced by observing the target's own state, not by trusting an
API response.

## Fixture acceptance

```bash
tools/input_probe/build.sh && tools/input_probe/vantage-probe &
python3 benchmarks/v21_desktop/accept.py
```

| Test | Result | Evidence |
|---|---|---|
| T2 left click | **PASS** | fixture counter 0 → 1 at (180, 220) |
| T3 double click | **PASS** | doubles 0 → 1, event `click_count` 1 then 2 |
| T4 right click | **PASS** | context menu opened (0 → 1) |
| T5 typing | **PASS** | field reads `'Vantage real input acceptance'` |
| T6 shortcut | **PASS** | `cmd+a` selected 23 of 23 characters |
| T7 scroll | **PASS** | clip-view offset 0 → 348.5 via the scroll bar's AXValue |
| T8 drag | **PASS** | drop target received `'vantage-drag-payload'` |

**7/7.** The suite re-asserts focus before every step and exits rather than
sending input to another application when it cannot hold focus.

## Complete user workflow

```bash
python3 benchmarks/v21_desktop/textedit_finder.py
```

**8/8.** TextEdit opened, a document created, the editor clicked and cleared,
`Hello from Vantage — real desktop automation verified.` typed with real
keycodes, `cmd+s` opening the real Save panel, the path typed into it, and the
file written by TextEdit itself. The saved RTF contains the note; Finder opens at
it. No step used a direct file write.

## Mission path

```bash
python3 benchmarks/v21_desktop/mission_desktop.py
python3 benchmarks/v21_desktop/studio_replay.py
python3 benchmarks/v21_desktop/takeover.py
```

- **MissionRunner** — a desktop click ran through the real runner with a real
  approval; the fixture counter moved 3 → 4.
- **Automation Studio** — a recorded `desktop_click` draft replayed through the
  mission runner; counter 4 → 5.
- **Takeover** — 17 workers paused and all four desktop action kinds (move,
  click, type, scroll) refused with HTTP 409.

A step reporting `done` only means the executor accepted the request, so both
mission scripts check the target's own counter instead.

## Defects fixed, each found by measuring

1. **Coordinate conversion off by 32 points.** The reason clicks appeared to work
   intermittently was never permissions: every click landed on the text label
   directly beneath the button. Found by adding a receiver-side `NSEvent` trace
   that reports which view was hit. Three earlier hypotheses — a modal dialog,
   occlusion, wrong window — were all wrong, and the trace is what settled it.
2. **Double-click `clickState`.** Both presses carried `clickState = 2`, so macOS
   saw no 1-then-2 sequence and controls discarded the pair.
3. **Shift never released.** A `CGEvent` starts from the live modifier state, and
   only shifted characters had `.flags` assigned, so lowercase letters inherited
   Shift: `Hello from Vantage` arrived as `HELLO FROM VANTAGE`.
4. **Keystroke timing.** 2 ms between a key's down and up and 6 ms between keys
   produced interleaved text (`L iprobessu`); 12 ms arrives in order.
5. **Scroll.** Synthetic wheel events never reached the application — line, pixel,
   continuous and phased units all measured zero. `AXScrollDownByPage` returned
   `kAXErrorUnsupported`. Setting the scroll bar's `AXValue` works.
6. **Focus lost mid-run.** Activating once at the start was insufficient; focus
   moved to another application between steps and every action missed. Each step
   now re-asserts and verifies focus first.

## What does not work yet

- **Standalone packaging.** `/Applications/Vantage.app` is a signed native Swift
  app, but its backend and native executor are not inside the bundle — it looks
  for `scripts/backend_lifecycle.py` beside itself and finds nothing. The desktop
  work above is verified through the development checkout only.
- **Permission attribution.** The executor inherits Accessibility from the
  process that launches it (measured: a never-granted binary reports
  `accessibility: true` when launched from this shell, and `false` when launched
  from Finder). A Finder-launched app therefore gets nothing until it is granted
  in its own right, which requires the packaging work above to be meaningful.
- **Direct File menus.** Some applications expose menus that synthetic clicks do
  not open reliably; the TextEdit workflow uses keyboard shortcuts for exactly
  that reason, which is also how a person would drive it.
