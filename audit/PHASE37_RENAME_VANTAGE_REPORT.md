# Phase 37 — Vantage

**Date:** 2026-10-02 · **Closes:** the user's rename request
**Deliberately does not close:** the code-level names (see §4) — that was the option declined

"Change project name to another diff new name." Asked and answered: **Vantage**, applied to the
places a person reads it, with the wiring names left alone on purpose.

## 1. How the rename was done, and how it was bounded

Two text patterns carried almost all of it, and both required a space between the two words:

```
"JARVIS Forge" -> "Vantage"
"Jarvis Forge" -> "Vantage"
```

That constraint is the whole safety of the operation: every identifier in this codebase is
underscore-, hyphen- or camel-cased (`JARVIS_HOME`, `jarvis-forge`, `com.jarvisforge.app`,
`X-JARVIS-Token`, `__JARVIS_BOOTSTRAP__`), so none of them can contain the two-word phrase with a
space. A regex for the remaining standalone visible word — `JARVIS` not adjacent to a letter,
digit, `_` or `-` — handled the rest in `jarvis/api/app.py` (OpenAPI descriptions, which render on
`/docs`) and `app/launcher.py` (the wifi dialog and its docstrings).

29 files changed in the first pass; the standalone-word pass touched 2 of those again
(`jarvis/api/app.py`, `app/launcher.py`); four more visible strings were edited by hand in
the client pages. 31 distinct files carry the rename, plus this report.

**Verified by counting, not by eyeballing.** Before and after, the wiring names were counted
across the tree and the two snapshots diffed:

| Identifier | Before | After |
|---|---|---|
| `JARVIS_*` environment variables | 493 | 493 |
| `X-JARVIS-Token` | 59 | 59 |
| `jarvis-bootstrap` / `__JARVIS_BOOTSTRAP__` | 10 / 6 | 10 / 6 |
| `jarvis.device` (the phone's stored pairing key) | 4 | 4 |
| `com.jarvisforge.app` (bundle identity) | 4 | 4 |
| `.jarvis-forge` (data directory) | 64 | 64 |
| `from jarvis` / `import jarvis` | 244 | 244 |
| `jarvis/` paths | 886 | 886 |

Identical, so nothing that carries state changed spelling.

## 2. What changed name

`README.md`, `PROJECT_DETAILS.md`, `dashboard/index.html`, `dashboard/phone.html`,
`dashboard/manifest.webmanifest` (`name`, `short_name` — the label on the phone's home screen),
`dashboard/sw.js`, `app/launcher.py` (window title, dialogs, permission notes),
`app/make_icon.py`, `build_app.py` (`APP_NAME` → the bundle is now `dist/Vantage.app`, with
`CFBundleName`/`CFBundleDisplayName` "Vantage"), the three `NS*UsageDescription` strings macOS
shows when asking for microphone, speech and Apple Events, `install_app.sh`, `requirements.txt`,
`run.py`'s startup banner, `jarvis/api/app.py`'s FastAPI title (`Vantage Core`) and its OpenAPI
descriptions, `.env.example`, `.github/workflows/ci.yml`, and the tests that assert visible text.

`docs/PHONE_AGENT_PLAN.md`, `STATUS.md`, `BASELINE_REPORT.md` and every `audit/*.md` report were
excluded: they are dated evidence of what was true when written, and rewriting them would be
editing the record rather than naming the product.

## 3. Two findings the rename produced

**A guard that silently stopped running.** `test_bundled_launcher_has_not_drifted_from_source`
compares `app/launcher.py` against the copy inside the built bundle. After the rename the suite
went from `1710 passed, 0 skipped` to `1709 passed, 1 skipped` — the guard's hardcoded
`dist/Vantage.app` path had been rewritten by the same pass that renamed the bundle, but the
bundle on disk was still the old one, so the file "did not exist" and the check *skipped itself*.
A guard that stops running reports nothing wrong, which is worse than a guard that fails. Fixed at
the root: the test now imports `APP_NAME` from `build_app`, so the path cannot drift from the
thing it is checking. Verified both directions — in sync it passes, and appending one line to the
bundled copy makes it fail again.

**A test that asserted the wrong file.** The new boundary test failed on its first run because it
looked for the literal `X-JARVIS-Token` in `jarvis/api/app.py`, which only *imports* the constant;
the string is defined once, in `jarvis/security/auth.py:33`. The premise was wrong, not the code —
the assertion was moved to the defining file rather than dropped.

## 4. What kept its old name, and why that is not laziness

| Kept | Why renaming it would cost the user something |
|---|---|
| `com.jarvisforge.app` (CFBundleIdentifier) | This is the identity macOS recorded the microphone and Speech Recognition grants against. A new identifier is a new app to TCC: permissions silently reset, and the microphone note in the panel would say "allowed" about an app that is no longer the one it was asked about. |
| `~/.jarvis-forge` | The memory database, the hash-chained audit log, the device registry and the API token live there. A new path leaves an installed user with an empty assistant and an orphaned ledger. |
| `jarvis.device` in localStorage | The phone's stable identity ("pairing a phone twice should not create two devices"). Renaming it un-pairs every handset that had paired. |
| `X-JARVIS-Token`, `__JARVIS_BOOTSTRAP__`, `jarvis-bootstrap` | Wire protocol between the shipped pages and the server; renaming one side in a later edit would produce a dashboard that silently never authenticates. |
| `import jarvis`, `JarvisCore`, `JARVIS_*` env vars | The declined option — a code-wide rename of 207 files and 89 configuration variables, which changes no user-visible thing and breaks every existing `.env`, shell and script. |
| the wake word "Jarvis" | `run.py`'s acceptance phrases and `JARVIS_WAKE_WORDS` default. It is what the user says out loud; changing it is a user decision, not a rebrand side effect. |

Four of these are pinned by `test_the_names_that_carry_state_kept_their_spelling` — five
parametrised cases asserting the exact spellings survive in the files that define them. Proven
non-vacuous: renaming `jarvis.device` to `vantage.device` makes it fail.

## 5. Live, after the rename

`JARVIS_LAN_MODE=true JARVIS_HOST=0.0.0.0 python run.py`, then Chrome:

```
banner      : Vantage core       -> http://127.0.0.1:8801/dashboard
              Phone client           -> http://127.0.0.1:8801/phone
/dashboard  : <title>Vantage — Control Dashboard</title>   (h1 "Vantage", badge "core",
                                                            "model: rules · 128 tools")
/phone      : <title>Vantage — phone</title>               (h1 "Vantage on this laptop")
/manifest   : {"name": "Vantage on this laptop", "short_name": "Vantage", ...}
bundle      : dist/Vantage.app built, ad-hoc signed, codesign verify: ok
```

Screenshots taken of both pages; the dashboard's device/peripheral/confirmation panels render
unchanged apart from the name.

## 6. Regressions and re-measured numbers

```
full suite            1717 passed · 0 failed · 0 skipped · 41 s   (was 1710; +7 boundary/brand)
§45 safety matrix     6 of 6
tools / routes        128 / 108   (unchanged — no behaviour was touched)
test modules          60          reports 39
```

The README's own drift guard failed once during this phase, correctly: it had been told the
repository ships 39 evidence reports before the 39th file existed. It was satisfied by writing
this file, not by editing the claim back down.

## 7. Carried forward

* **37-A** the project is now half-renamed by design: product "Vantage", code `jarvis`. A reader
  arriving from the README will hit `import jarvis`, `JARVIS_HOME` and `JarvisCore` and has to
  reconcile them; the note at the top of the README is the mitigation, and it is a mitigation, not
  a fix.
* **37-B** the voice wake word is still "Jarvis". Renaming it changes what the user says out loud
  and is theirs to decide; `JARVIS_WAKE_WORDS` already accepts alternatives without a code change.
* **37-C** searching this repository for "JARVIS" still returns hits — the historical reports and
  the wiring names. Anyone auditing "is the rename complete?" will get a misleading answer from a
  plain grep, which is why §1 counts identifiers instead.
* **37-D** `dist/Jarvis Forge.app` was deleted as a stale build artifact of this session's own
  earlier build. Nothing was installed under `/Applications` (checked: neither name is present),
  so no user-facing install was touched. A previously installed copy elsewhere would need
  re-installing under the new name; `install_app.sh` does that.
* Unchanged from before: 36-A…E, 35-A…C, 34-A…C, and the `-rs` CI change that would finally name
  the runner's six skips.

## 8. Git

Parent `09a1ee6` (Phase 36 verdict). Committed: the 31 files above (including `tests/test_voice.py`, whose
guard now derives `APP_NAME`, and `tests/test_phase36_two_applications.py`, which carries the
boundary tests),
`README.md`, `benchmarks/*.json`, `assets/*.svg`, this report.

**CI verdict:** to be appended once the run exists — not before.
