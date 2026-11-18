# Desktop executor promotion — provenance

Recorded so a later report cannot claim "the app has the fix" without the hashes
to prove which binary it means.

## The defect being promoted

`locate()` in `native/desktop_executor/main.swift` took `hits.first` when several
differently-described Accessibility elements matched a keyword, while a comment
above it claimed ambiguity was "reported rather than guessed" and a
`target_ambiguous` status went unused. Measured: `keyword="Click"` against the
input fixture matches **three** elements — the button, its counter label and the
right-click target — and the old binary pressed whichever the AX walk reached
first, reporting success.

## Promotion chain (each step verified)

| stage | artifact | CDHash | SHA-256 (first 24) | has fix |
|---|---|---|---|---|
| source | `native/desktop_executor/main.swift` @ `e657218` | — | — | yes |
| staged | `native/desktop_executor/vantage-desktop.staging` | `e2eac2c7` | — | yes |
| promoted | `native/desktop_executor/vantage-desktop` | `e2eac2c7` | — | yes |
| dev bundle | `VantageDesktopDev.app/Contents/MacOS/vantage-desktop` | `3eb8faf8` | `93f539676104ebeae1119f95` | yes |
| **app bundle** | `Vantage.app/Contents/Resources/native/desktop_executor/VantageDesktopDev.app/…` | `3eb8faf8` | `93f539676104ebeae1119f95` | yes |

The app-bundle copy is **byte-identical** to the verified dev bundle — same
SHA-256 — so the binary tested at the fixture is the binary the app ships.

- Architecture: `arm64`
- Signing: `Signature=adhoc` (so any Accessibility grant is bound to the CDHash
  and does not survive a rebuild — see `docs/DESKTOP_PERMISSION_MODEL.md`)
- Source revision at promotion: `e657218`

The bare `native/desktop_executor/vantage-desktop` was backed up to
`/tmp/executor-backup-pre-promote` before being replaced.

## Acceptance matrix — run against the **candidate bundle's own executor**

Not the staging copy, and not the checkout's dev bundle:

| case | call | expected | observed |
|---|---|---|---|
| ambiguous | `read_element keyword="Click"` | `target_ambiguous` | `target_ambiguous`, 3 matches |
| role-filtered, still ambiguous | `+ role="AXButton"` | `target_ambiguous` | `target_ambiguous`, 2 matches |
| unambiguous | `keyword="probe.field"` | resolves | `verified`, `probe.field` |
| no side effect | after the ambiguous calls | clicks unchanged | `clicks = 0` |

And the candidate's backend resolves its **own** bundled executor:

```
/tmp/vantage-rc/Vantage.app/Contents/Resources/native/desktop_executor/…/vantage-desktop
exists: True
```

## What this does and does not settle

**Settled:** the SwiftUI app now ships an executor that refuses an ambiguous
target instead of pressing an arbitrary one, and the resolution path from the
app's backend to that binary was exercised, not assumed.

**Not settled:** whether the *running, Finder-launched* app can post input at
all. macOS grants Accessibility to a responsible process; the same binary reports
`accessibility: true` from a terminal and `false` under `Vantage.app`. That is a
user grant, not a build step. See `docs/DESKTOP_PERMISSION_MODEL.md`.

## Shipped in the DMG

`dist/Vantage-0.2.0.dmg` (46,239,089 bytes), built by
`installers/macos/build_dmg.sh` with `SHIP_NATIVE=1`, mounted read-only and
inspected:

| check | value |
|---|---|
| bundle identifier | `com.jarvisforge.vantage` — the native SwiftUI app |
| executor SHA-256 | `93f539676104ebeae1119f95…` — **identical to the verified binary** |
| ambiguity fix present | yes |
| mount/unmount | clean |

The same binary verified at the fixture is the one inside the DMG: same SHA-256,
byte for byte.

### Release gate status for the executor fix

| stage | status |
|---|---|
| IMPLEMENTED | yes — `native/desktop_executor/main.swift` @ `e657218` |
| STAGED | yes — `vantage-desktop.staging`, CDHash `e2eac2c7` |
| BUNDLED | yes — app bundle and DMG, SHA-256 `93f53967…` |
| RUNNING | **yes for the dev bundle path; NO for the installed app** — the app the user has open predates the rebuild and has not been replaced |
| VERIFIED | yes as a binary: refuses ambiguity, resolves unambiguously, no side effect — measured against the DMG's own executor |

"RUNNING" is the one stage not closed, and it is a user action: replacing the
installed app interrupts whatever they are doing. The artifact is ready.

## The installed copy is a different, older, non-functional app

Inspected while checking what the user actually runs:

| | `/Applications/Vantage.app` | `macos/Vantage/Vantage.app` (running, pid 65474) |
|---|---|---|
| bundle id | `com.jarvisforge.vantage` | same |
| built | **2026-11-15 15:59** | 2026-11-18 08:29 |
| `Contents/Resources/` | **`Vantage.icns` only** | python, jarvis, dashboard, native, site-packages |
| bundled Python | **none** | `python/bin/python3.11` |
| bundled executor | **none** | present, SHA-256 `93f53967…`, has the fix |

The installed copy is a bare SwiftUI shell with no runtime, no backend and no
executor — it cannot run standalone and predates the executor entirely. The app
that is currently running was launched from the checkout, which is why it works
and the installed one would not.

This is not something a build fixes silently: replacing `/Applications/Vantage.app`
would interrupt whatever the user has open. The DMG carries the complete app;
installing it is their step.
