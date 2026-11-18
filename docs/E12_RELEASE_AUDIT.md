# E12 — Release readiness audit

**Auditor:** Worker A6 · **Date:** 2026-11-18 · **Read-only audit** (the only file
written is this one).

**State at audit time:** branch `main`; HEAD moved three times during the audit
(`cb683c8` → `0c5dee5` → `ebc5028`) and `dist/Vantage-0.2.0.dmg` was **rewritten
by a concurrent build** (`build_dmg.sh`, pid 75176, started 12:45) while this
audit was running. Both DMG states were audited and are distinguished below.

**The one sentence to take away:** E12's acceptance criterion is still the
placeholder `"to be detailed as the epic is worked"`. There is no acceptance
list, so "is E12 met" is not answerable as written — this audit supplies one and
measures against it.

---

## 1. What was actually run

Every claim below has a command. Exit codes are recorded; `UNVERIFIED` means the
check was not possible here and why.

### 1.1 Artifact A — the DMG as it existed at session start

| check | command | result |
|---|---|---|
| size / mtime at start | `ls -la dist/` | 46,958,979 bytes, mtime 2026-11-18 09:19 |
| SHA-256 | `shasum -a 256 dist/Vantage-0.2.0.dmg` | `24d4a38b6ebfb7537d63a44cd98f99d03e3e569119cd073e51ba7a42d01447f7` |
| image integrity | `hdiutil verify dist/Vantage-0.2.0.dmg` | **VALID**, exit 0, CRC32 `$11EE3B37` |
| mount | `hdiutil attach -readonly -nobrowse -mountpoint /tmp/e12_audit_mnt` | exit 0, `/dev/disk7s1`, `read-only` |
| bundle id | `PlistBuddy -c "Print :CFBundleIdentifier"` | `com.jarvisforge.vantage` |
| short version | `PlistBuddy -c "Print :CFBundleShortVersionString"` | `0.2.0` |
| build version | `PlistBuddy -c "Print :CFBundleVersion"` | `1` |
| signature | `codesign --verify --strict --deep --verbose=2` | **valid on disk / satisfies its DR**, exit 0 |
| signing identity | `codesign -dvvv` | `Signature=adhoc`, `TeamIdentifier=not set`, CDHash `5cb2aaab…`, `Sealed Resources … files=2746` |
| main binary SHA-256 | `shasum -a 256 Contents/MacOS/Vantage` | `e9ed2b738c1efea8fe2e8c91c29fcb1dc3c880d5309cd317c76c4d31ceac4366` (25,485,424 bytes, arm64) |
| bundled executor SHA-256 (bare) | `shasum -a 256 …/desktop_executor/vantage-desktop` | `dfd0f4de961a08370ff5ba65efc66eb25bf504c331e9a91e82e408b867070920` |
| bundled executor SHA-256 (dev-bundle copy — what the app resolves) | `shasum -a 256 …/VantageDesktopDev.app/Contents/MacOS/vantage-desktop` | `f786a64ba1c88f975e000b052fb05a1a4d615ffdf3a4abd23fa215352d48f043` |
| bundled Python | `Contents/Resources/python/bin/python3.11 -c "import sys; print(sys.version)"` | **`3.11.18 (main, Nov 11 2026)`**, exit 0 |
| executor identity | `…/vantage-desktop identity` | `status=verified`, `signing=adhoc`, bundle `com.jarvisforge.vantage.desktopdev`, `executable_sha256` = the same `f786a64b…` |
| unmount | `hdiutil detach /tmp/e12_audit_mnt` | (see §1.2 — detached after the rebuilt image was mounted) |

**A leak from the concurrent build:** `python_path.txt` in both images records
`/Users/abhijeetmiskin/AppData/Vantage/macos/Vantage/Vantage.app/Contents/Resources/python/bin/python3.11`
— the **build machine's checkout path**, not a bundle-relative one. See §3.2.

### 1.2 Artifact B — the DMG as rebuilt mid-audit (current on disk)

| check | command | result |
|---|---|---|
| current hash | `shasum -a 256 dist/Vantage-0.2.0.dmg` | `ea44a440c19f0ef1f869f7074dcb8a39e51897b9730f8706d95ad2e8ec1d0ba1` |
| size / mtime | `stat` | 49,193,446 bytes, 2026-11-18 12:46:55 |
| image integrity | `hdiutil verify` | **VALID**, exit 0, CRC32 `$DF4C9F88` |
| mount | `hdiutil attach -readonly` → `/tmp/e12_audit_mnt2` | exit 0 |
| plist | `PlistBuddy` | id `com.jarvisforge.vantage`, short `0.2.0`, build `1` — identical to A |
| signature | `codesign --verify --strict --deep` | **valid / satisfies DR**, exit 0; CDHash `7f3d4c4e…`, adhoc |
| main binary SHA-256 | `shasum` | `d7e4f67b00cc0ec7e416138254343759ef151c9c4e960b7fc79f7ebeca3ffb09` |
| bare executor SHA-256 | `shasum` | `4c7d2a7c5368bbcd51705366375feb47b70c05d0e2e4436146d9d287b7807c5a` |
| dev-bundle executor SHA-256 | `shasum` | `709aa01b96a382c61733a9b996e21515b0f7407c091762277a0c560ddb894e73` |
| bundled Python | `-c "import sys; print(sys.version)"` | `3.11.18 (main, Nov 11 2026)` |
| backend imports | `PYTHONPATH=Resources:Resources/site-packages …/python3.11 -c "import fastapi, uvicorn, pydantic"` | `fastapi 0.162.7`, `uvicorn 0.54.0`, `pydantic 2.14.3`, exit 0 |
| `import jarvis` | same interpreter | exit 0 |
| main binary vs checkout bundle | `shasum` both | **byte-identical** to `macos/Vantage/Vantage.app/Contents/MacOS/Vantage` (`d7e4f67b…`) |

### 1.3 What the source tree says (provenance)

- The **current** DMG is built from the **working tree, not HEAD**. Four modified
  files compared directly:

  | file | HEAD | worktree | DMG (`Resources/<file>`) |
  |---|---|---|---|
  | `jarvis/config.py` | `2abfbd2f…` | `c0971ca7…` | `c0971ca7…` |
  | `jarvis/core/orchestrator.py` | `fe0d3c94…` | `d25355b4…` | `d25355b4…` |
  | `jarvis/core/planner.py` | `179a0aa0…` | `18b3ae04…` | `18b3ae04…` |
  | `jarvis/security/policy.py` | `4a3f8437…` | `f4e44195…` | `f4e44195…` |
  | `jarvis/gateway/selection.py` | `bcb4fb83…` | `51cd29f6…` | `51cd29f6…` |

  `jarvis/config.py` is a **security-relevant file** (`jarvis/security/policy.py`
  likewise). The artifact therefore ships uncommitted, possibly-unreviewed code
  whose commit sha cannot be named.
- `macos/Vantage/Vantage.app` is **gitignored** (`.gitignore:49`) and untracked
  (`git ls-files` → 0 files) — the app the DMG is staged from is build output,
  rebuilt in place, with no commit pinning it.
- **`installers/macos/build_dmg.sh:96`** signs the **DMG** ad-hoc (`codesign
  --force --sign -`) and **`installers/macos/assemble_app.sh:136`** never passes
  `--options runtime`; no `codesign` call adds the hardened runtime. No
  `notarytool`/`stapler` step exists in either script.

---

## 2. E12 acceptance table (supplied and measured)

E12's stored acceptance is `["to be detailed as the epic is worked"]`
(`.autodev/tasks.json`, `E12-release`). That is not a criterion. The list below
is the one an end-to-end release epic implies; **it is this audit's proposal, not
the tracker's.** Each row states what was measured, not what a report claims.

| # | requirement | status | evidence |
|---|---|---|---|
| 1 | DMG image is not corrupt | **MET** | `hdiutil verify` exit 0, CRC32 `$11EE3B37` (A) / `$DF4C9F88` (B) |
| 2 | DMG carries an app with the expected identity and version | **MET** | PlistBuddy: `com.jarvisforge.vantage`, `0.2.0`, `1` — both images |
| 3 | App inside the DMG verifies its own signature | **MET** | `codesign --verify --strict --deep` exit 0 on both; ad-hoc, `TeamIdentifier=not set` |
| 4 | The app is **self-contained** (ships Python, backend, dashboard, executor) | **MET (with §3.2 caveat)** | `Resources/` holds `python/bin/python3.11` (runs, reports 3.11.18), `jarvis/`, `dashboard/`, `run.py`, `site-packages/` (89 entries; fastapi/uvicorn/pydantic import), `native/desktop_executor/` |
| 5 | Executor is the fixed one, and the DMG's copy is the one measured | **MET** | `target_ambiguous` present in both copies; `identity` on the DMG's own executor reports `status=verified` and its own SHA-256. **But see §3.1 — the tracker's recorded hash is wrong** |
| 6 | A user can install it with a documented path and a rollback | **NOT MET** | `installers/macos/install_app.sh` exists (validate → timestamped backup → atomic swap → verify → restore on failure) but **is never mentioned in a user-facing install instruction**; the README (`README.md:589`) points at a **different, older root `install_app.sh`** that builds the Tk launcher and has **no signature check and no post-install verification**. The DMG contains no README. §5 below |
| 7 | The artifact runs when installed (RUNNING) | **UNVERIFIED / EXTERNAL** | `/Applications/Vantage.app` is a different, older app (§4). Replacing it interrupts the user; this audit was forbidden to install or launch |
| 8 | Gatekeeper accepts the app on a fresh Mac (the actual install experience) | **NOT MET** | `spctl --assess --type execute` → `rejected`, exit 3, on **both** the DMG's app and the installed one |
| 9 | The artifact is notarized / notarizable here | **EXTERNAL_BLOCKED** | `xcrun notarytool history` → `Error: Must provide credentials`; `security find-identity -v -p codesigning` → **0 valid identities**; `xcrun stapler validate` → `does not have a ticket stapled to it`, exit 65. No Developer ID exists on this machine |
| 10 | The artifact is traceable to a commit | **NOT MET** | Built from the dirty worktree, not HEAD (§1.3); `build_dmg.sh` writes no manifest or provenance file — no `git_commit`/`built_at`/SHA record alongside the DMG. `scripts/release_verify.sh` writes a manifest, but only for the Tk launcher it builds |
| 11 | Every documented build path produces the shipping artifact | **NOT MET** | `installers/build_release.sh` → `build_dmg.sh` (native, correct). `scripts/release_verify.sh` → refuses by default (correct) but with `VANTAGE_VERIFY_LAUNCHER=1` builds the launcher; `README.md:589` still instructs `./install_app.sh` → Tk launcher. Two live user-facing paths, two different products. **Neither is invoked from CI** (`grep -E "release_verify\|build_release" .github/workflows/*.yml` → exit 1, no hits) |
| 12 | The Android leg of "end-to-end release readiness" | **EXTERNAL_BLOCKED** | `adb` not found, `emulator` not found, `java` cannot locate a runtime, `/opt/homebrew/opt/openjdk@17` absent, `~/Library/Android/sdk` absent, no `.apk` anywhere in `dist/` |
| 13 | An uninstall is documented | **NOT MET** | `grep -rn -i uninstall docs/` → only `scripts/autodev/uninstall-login-agent` (removes the dev supervisor plist). **No user-facing uninstall instructions anywhere** in `docs/`, `README.md`, or the DMG |
| 14 | Rollback after install exists | **PARTIALLY MET** | `installers/macos/install_app.sh:48-54` copies the installed app to `/Applications/Vantage.app.backup-<timestamp>` and restores it on any post-install verification failure; 15 such backups exist on disk (`/Applications/Vantage.app.backup-*`). The root `install_app.sh` uses `Vantage.app.previous` and deletes the older backup. **The rollback script is not referenced by any install instruction a user would follow** |

**Count (14 rows):** MET 5 (rows 1-5) · NOT MET 5 (rows 6, 8, 10, 11, 13) ·
PARTIALLY MET 1 (row 14) · EXTERNAL_BLOCKED 2 (rows 9, 12) · UNVERIFIED 1 (row 7).

---

## 3. The two findings that matter

### 3.1 The tracker's executor hash is wrong, and the hash chain in the docs has split

E12's own `evidence[0]` states the bundled executor is `SHA-256 93f53967…`,
"byte-identical to the binary verified to refuse ambiguous targets".
`docs/EXECUTOR_PROMOTION.md:75,88,105` repeats `93f53967…` for the DMG.

Measured:

| artifact | SHA-256 |
|---|---|
| `EXECUTOR_PROMOTION.md` / E12 tracker claim | `93f539676104ebeae1119f95…` |
| DMG at session start (A), dev-bundle copy | `f786a64ba1c88f975e000b05…` |
| `docs/V23_5_RELEASE_REPORT.md:27` claim for the same DMG | `f786a64b…` ✓ matches A |
| current DMG (B), dev-bundle copy | `709aa01b96a382c61733a9b9…` |
| checkout `macos/Vantage/Vantage.app`, dev-bundle copy | `709aa01b…` ✓ matches B |
| checkout `native/desktop_executor/vantage-desktop` | `4c7d2a7c5368bbcd51705366…` |

So: **the `93f53967…` claim never matched any artifact on disk.** It is not a
stale value from an older build — the older V23.4 report used `f786a64b…` and the
V23.5 report also used `f786a64b…`. `93f53967…` is not reproducible from this
tree at all. The executor *fix* (`target_ambiguous` string present, `identity`
reporting `verified`) is real in every copy; the **provenance record is false**,
which is exactly the class of claim `EXECUTOR_PROMOTION.md` was written to
prevent ("Recorded so a later report cannot claim 'the app has the fix' without
the hashes to prove which binary it means"). The record does not do its job.

Additional inconsistency: `EXECUTOR_PROMOTION.md` and `V23_4_RELEASE_REPORT.md`
both place the app-bundle executor at CDHash `3eb8faf8` / `4afafcc5` — different
values for the same claimed byte-identical binary.

### 3.2 `python_path.txt` records the build machine's absolute path

`Contents/Resources/python_path.txt` (identical bytes in A and B) reads:

```
/Users/abhijeetmiskin/AppData/Vantage/macos/Vantage/Vantage.app/Contents/Resources/python/bin/python3.11
```

`scripts/bundle_python.sh:273` writes `${RUNTIME}/bin/python3.11` where
`RUNTIME="$(cd "${APP}/Contents/Resources" && pwd)/python"` (`:128-129`) — an
**absolute path resolved at build time**, inside the staging bundle. Because the
staging bundle is at the same path as the source bundle on this machine, that
absolute path happens to be *correct here* and **wrong on any other Mac**.

`macos/Vantage/Sources/Services/BackendManager.swift:838-844` reads that file
first and uses it if `isExecutableFile`. On another Mac it is not executable, so
the chain falls to `.venv/bin/python` (absent in a bundle, `:845-848`), then
`/usr/bin/python3`. Measured on this machine, `/usr/bin/python3` is **3.9.6**, and
loading the bundle's `site-packages` under it fails:

```
File ".../site-packages/anyio/_lazyimport.py", line 131
    match stmt:
SyntaxError: invalid syntax
```

The bundle's own interpreter (`3.11.18`) imports the same tree cleanly. The
comment at `bundle_python.sh:270-272` states the requirement — "It must be the
bundle-relative runtime, not the build machine's `.venv`" — and the code writes
an absolute path anyway. **On a second Mac the app's backend start would fail
before binding, which is the exact failure the comment at
`BackendManager.swift:833-837` documents as already measured once.**

This audit did **not** verify the two-Mac path empirically (no second machine).

---

## 4. Installed app vs the DMG — different products, plainly

| | `/Applications/Vantage.app` | DMG app |
|---|---|---|
| bundle id | `com.jarvisforge.vantage` | `com.jarvisforge.vantage` |
| short version | `0.2.0` | `0.2.0` |
| built | all files 2026-11-15 15:59 | 2026-11-18 12:46 |
| size | 18 MB (3 files total) | 106 MB (2,749 files) |
| `Contents/Resources/` | **`Vantage.icns` only** | python, jarvis, dashboard, native, site-packages, scripts |
| main binary SHA-256 | `7a22f1629fc664516da592371352826fd8e69be6e317e8aec62951be74d95c74` (18.4 MB) | `d7e4f67b00cc0ec7e416138254343759ef151c9c4e960b7fc79f7ebeca3ffb09` (24.3 MB) |
| bundled Python | **none** (`…/python/bin/python3.11` missing) | present, runs |
| bundled executor | **none** | present |
| signature | ad-hoc, verifies | ad-hoc, verifies |
| Gatekeeper | `spctl` **rejected** | `spctl` **rejected** |
| running? | no | — (the running process is the checkout's, pid 65474) |

**They share a bundle id and a version string and nothing else.** The installed
app is a bare SwiftUI shell with no runtime, backend or executor: it cannot run
standalone. `mdfind` resolves `com.jarvisforge.vantage` to exactly this install
(`/Applications/Vantage.app`). Replacing it is a user action that interrupts the
running app, which is E12's stated blocker and is real.

Also note `dist/Vantage.app` (present, mtime 2026-11-17 14:39) is a **third**
bundle: `com.jarvisforge.app`, `Contents/MacOS/JarvisForge` — the Tk launcher.

---

## 5. Rollback and install documentation — what exists

**Exists and is good:**
- `installers/macos/install_app.sh` — validated, backed up, atomic-swapped,
  verified, restored on failure. Documented in `docs/RELEASE_AND_BUILD.md:119-132`
  under "Installing over an existing copy". That section, however, is a *build
  document* for this repo, not an install instruction for a DMG recipient.
- 15 real backups on disk from past installs (`/Applications/Vantage.app.backup-2026*`),
  so the mechanism has been exercised.

**Does not exist:**
- **No uninstall documentation at all.** No doc, no README section, no script.
  The only `uninstall` hit is `scripts/autodev/uninstall-login-agent`.
- **No install instructions inside the DMG.** The image root holds `Vantage.app`
  and an `Applications` symlink; no README, no license, no "drag to Applications"
  text beyond the symlink itself. `build_dmg.sh:114` prints the instruction to
  the builder's terminal, not to the recipient.
- **The README's install path is the wrong one.** `README.md:589` says
  `./install_app.sh` moves the app into `/Applications` and "keeps any previous
  install as `Vantage.app.previous`". That is the **root** `install_app.sh`, which
  runs `build_app.py` (the **Tk launcher**, `com.jarvisforge.app`), performs **no
  signature check** and **no post-install verification**, and **deletes the
  older backup** before moving (`install_app.sh:31-37`). The safe, native-bundle
  installer at `installers/macos/install_app.sh` — which the README does not
  mention — is the one that checks and rolls back. A user following the README
  gets a different product with a weaker install.

**Rollback verdict:** a rollback **mechanism** exists and is sound; a **documented
rollback path for a DMG recipient** does not. On the "would a user who installed
this DMG know how to undo it" question the answer is no.

---

## 6. Notarization readiness — classification

| check | command | result |
|---|---|---|
| Developer ID identity present | `security find-identity -v -p codesigning` | **0 valid identities** |
| authority on the app | `codesign -dvvv` | `Signature=adhoc`, `TeamIdentifier=not set` — no `Authority=` line |
| Gatekeeper (app) | `spctl --assess --type execute --verbose=4` | `rejected`, exit 3 |
| Gatekeeper (DMG) | `spctl --assess --type open --context context:primary-signature` | `rejected`, exit 3 |
| notarization credentials | `xcrun notarytool history` | `Error: Must provide credentials.` — **reported, not worked around** |
| notary credentials in env | `env \| grep -iE "notary\|apple_id\|ASC_\|AC_PASSWORD"` | none |
| keychain profiles | `security dump-keychain \| grep -i notary` | none |
| ticket stapled | `xcrun stapler validate` (app and DMG) | `does not have a ticket stapled to it`, exit 65 |
| hardened runtime in build | `grep -- '--options runtime'` in `assemble_app.sh` / `build_dmg.sh` | **no hits** — a prerequisite for notarization is also absent |

**Classification: IMPOSSIBLE HERE, and not yet PREPARED either.**
- *Credentialed*: no — zero codesigning identities, no notary credentials.
- *Prepared*: no — the build does not pass `--options runtime`, which notarization
  requires; nothing signs with a Developer ID.
- *Impossible here*: yes — both the Developer ID certificate and the App Store
  Connect credentials are owner-held, external assets.

Owner action to close: obtain a Developer ID Application certificate, add
`--options runtime` to the app's `codesign` invocation (and a `notarytool
submit`/`stapler staple` step after `build_dmg.sh`), then re-run the four checks
above. Not an agent decision.

---

## 7. The four blocked tracker tasks — is each blocker real, and what closes it

Re-read from `.autodev/tasks.json`; every one was checked against the tree.

### `R6.4c-email-organize-operations` — blocker: **REAL (owner decision)**
Neither `ImapSmtpProvider` nor a local maildir path implements an archive/move
operation. Closing it needs the owner to choose between (a) implementing a real
gated IMAP `MOVE` / `\Deleted`+`EXPUNGE` (plus a maildir equivalent) or (b)
declaring the boundary permanent with the honest decline that is already live
(`evidence.honest_answer`: "DONE (r95)"). Nothing in the tree has changed this.

### `R6.4-capability-registry-gate-or-delete` — blocker: **REAL (owner decision)**
Architectural: wiring `can_handle_request` as a live gate would refuse a working
provider; deleting a tracked module or the owner's untracked copy is not an agent
call. The reporting half (`R6.4a2`) is done. Closes on one written owner
decision.

### `R7-android-companion` — blocker: **REAL and independently re-measured**
`adb not found`, `emulator not found`, `java` → "Unable to locate a Java Runtime",
`/opt/homebrew/opt/openjdk@17` absent, `~/Library/Android/sdk` absent, and no
`.apk` under `dist/` or `android/app/build/outputs/`. This is now **two**
blockers (no device *and* no JDK), not one. Closes when a JDK 17–21, an Android
SDK, and a device or emulator are present; build + unit tests alone do not close
it.

### `V15-desktop-automation` — blocker: **REAL (external permission)**
`Accessibility grant for macos/Vantage/Vantage.app itself` — macOS grants to the
responsible process; granting the dev bundle or the executor was measured to be
ineffective. Closes with a user-granted Accessibility (and post-event) permission
for the app's own bundle, which no build step can create.

### `E12-release` — blocker as recorded: **REAL but incomplete**
"Installed-app replacement is a user action … plus an Accessibility grant." Both
true. But two of E12's *own* gaps are not blockers at all and are not on its
record: **no acceptance list**, and the §3.1 provenance failure.

---

## 8. The single most important gap

**E12 cannot be closed as an epic because it has no acceptance criterion, and the
evidence record that would stand in for one is false.**

Concretely: `.autodev/tasks.json` stores `acceptance: ["to be detailed as the
epic is worked"]`, so no claim of "release ready" is falsifiable; and E12's
`evidence[0]` states the DMG's executor is `93f53967…`, a hash that matches
**no binary this tree can produce** (the DMG has shipped `f786a64b…` and then
`709aa01b…`; the checkout has `4c7d2a7c…`). A release gate whose recorded hash
cannot be reproduced is not a gate.

The second-most-important gap, if the first is fixed, is §3.2: the DMG's
`python_path.txt` points at this machine's checkout, so "self-contained" is
proven only on the Mac that built it.

---

## 9. Recommendation for E12's final status

**Recommend `blocked` — not `completed`, and not `pending`.**

Reasoning:

1. **Not `completed`.** Of the fourteen supplied requirements, five are NOT MET,
   one is only PARTIALLY MET, two are EXTERNAL_BLOCKED and one is UNVERIFIED. The
   ones that matter for a user: Gatekeeper rejects the
   app on both images (`spctl` exit 3); there is no uninstall documentation and no
   documented install path that leads to *this* artifact; the artifact is not
   traceable to a commit because it was built from a dirty tree; and the recorded
   provenance hash is wrong.
2. **Not `pending`.** The remaining work is not one more engineering iteration.
   The two hardest items — the Accessibility grant and replacing the installed
   app — are **user actions**, and notarization is **impossible without owner
   credentials**. That is the definition the tracker already uses for `blocked`.
3. **The blocker should be rewritten.** Today it names the installed-app
   replacement and the Accessibility grant but omits: (a) `acceptance` must be
   filled in first; (b) the provenance record must be corrected and the build
   made reproducible (`git_commit` + `built_at` + SHA manifest emitted by
   `build_dmg.sh`, and a build from a clean tree); (c) `python_path.txt` must be
   bundle-relative; (d) a user-facing install/uninstall section must exist and
   must point at `installers/macos/install_app.sh`, not the root launcher
   installer. (b)–(d) are **agent-doable** and should not be recorded as blocked
   alongside the user actions.
4. **Ready-to-ship is not the same as ready-to-distribute.** As an artifact *for
   this Mac's owner*, the current DMG is intact, signed ad-hoc, self-contained,
   carries the fixed executor, and comes from a build that verifies. As a release
   *to anyone else*, it is not: ad-hoc signing, no notarization, no hardened
   runtime, no uninstall, no manifest, and a build path that still points a
   reader at a different product.

**Suggested disposition:** `E12-release` → `blocked`, with the record split into
an agent-doable sub-task (provenance + reproducibility + `python_path.txt` +
install/uninstall docs — none of which are blocked) and the two owner/external
items (notarization credentials; installed-app replacement + Accessibility grant).
Do not mark it `completed` on the strength of `hdiutil verify` and
`codesign --verify` alone; those say the artifact is *well-formed*, not that it
is *releasable*.

---

## Appendix — read-only compliance

- No file under `jarvis/`, `macos/`, `native/`, `tests/` or `.autodev/` was
  modified. `git status --short` before and after the audit shows the same six
  modified files and one untracked test, none touched by this audit.
- Nothing was installed; `/Applications/Vantage.app` was not replaced; no GUI was
  launched. The running process (pid 65474, from `macos/Vantage/Vantage.app`) was
  read, not started.
- The test suite was not run.
- Two `hdiutil attach` mounts were made read-only and both were detached:
  `/tmp/e12_audit_mnt` (artifact A) and `/tmp/e12_audit_mnt2` (artifact B).
- The concurrent rebuild observed mid-audit (`build_dmg.sh`, pid 75176) was **not
  started by this audit** and was not interrupted. Its output is audited as
  artifact B because it is what currently sits at `dist/Vantage-0.2.0.dmg`.