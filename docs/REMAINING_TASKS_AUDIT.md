# Remaining tasks — evidence-backed disposition

Audit of the 8 non-completed entries in `.autodev/tasks.json` (101 total: 93
completed, 5 blocked, 3 pending). Every claim below was checked against the tree
on 2026-11-18; where a check could not be run it says `UNVERIFIED`.

**Method.** Records were read with
`python3 -c "…json.load(open('.autodev/tasks.json'))…"` (the `tasks` value is a
**dict** keyed by task id, not a list). Targeted test files were run with
`.venv/bin/python -m pytest <file> -q -p no:cacheprovider`. The full suite was
**not** run (per the audit's constraints). The GUI was **not** launched; the one
running instance (pid 65474, backend pid 65478) was left alone.

## Summary

| Task | Stored status | Disposition | One-line basis |
| --- | --- | --- | --- |
| `V15-desktop-automation` | blocked | `BLOCKED_ON_USER_PERMISSION` | Real, but the blocker text is stale about *whose* grant is needed — `docs/DESKTOP_PERMISSION_MODEL.md:31-42` says granting the executor or dev bundle cannot ever work. |
| `R-release-macos-dmg-artifact` | blocked | `STALE_RECORD` | The owner decision was **made 2026-10-27**: the DMG ships the native app. `build_dmg.sh:26` `SHIP_NATIVE=1`, `docs/RELEASE_AND_BUILD.md:180`, pinned by `tests/test_r106_dmg_ships_either_artifact.py` (3 passed). |
| `R7-android-companion` | blocked | `GENUINELY_BLOCKED_EXTERNAL` | No `adb`, no emulator binary, no system-images, no device (`adb devices` → command not found). **Additionally** the build itself can no longer run: no JDK anywhere on this machine. |
| `E12-release` | pending | `NEEDS_WORK` | Its stated blocker is already resolved (same DMG decision); its acceptance is still the placeholder "to be detailed as the epic is worked", so the epic has never been scoped. |
| `E11-continuity` | pending | `STALE_RECORD` | Its "NOT DONE: architecture change" item was implemented before/at commit `b559bf3` (2026-11-14, ancestor of HEAD). `list_conversations` merges both stores; `tests/test_r22_shared_conversation.py` 25 passed. |
| `E7-calls-contacts` | pending | `COMPLETE_AS_RECORDED` | "Call preparation + contacts DONE" holds: `tests/test_r102_communications.py` 3 passed (adds a contact, prepares a call, never claims a call happened). The residual blocker (actually placing a call) is honest and real. |
| `R6.4c-email-organize-operations` | blocked | `BLOCKED_ON_OWNER_DECISION` | Verified: neither mail provider has an organize op (`jarvis/integrations/email_provider.py` — `LocalMailProvider` and `ImapSmtpProvider` expose only search/read/save_draft/send/describe). The honest decline is live (`jarvis/core/planner.py:1644-1662`). |
| `R6.4-capability-registry-gate-or-delete` | blocked | `BLOCKED_ON_OWNER_DECISION` | The architectural choice is genuinely the owner's; but the record is **stale in one detail** — `jarvis/security/providers.py.multimodal` no longer exists. |

Two records are flatly out of date (`R-release-macos-dmg-artifact`, `E11-continuity`)
and should not be carried as open work. One (`V15`) is genuinely blocked but its
stated remedy would not unblock it.

---

## V15-desktop-automation — `BLOCKED_ON_USER_PERMISSION`

**The block is real.** From a terminal the executor reports a full grant:

```
$ ./native/desktop_executor/vantage-desktop permissions
{"accessibility":true,"can_click":true,"can_move_cursor":true,"can_read_ui":true,
 "can_type":true,"post_events":true,"screen_capture":true,
 "executable":".../native/desktop_executor/vantage-desktop","status":"verified"}
(exit 0)
```

The same binary inside the app bundle answers the same way **from a shell**:

```
$ macos/Vantage/Vantage.app/Contents/Resources/native/desktop_executor/\
VantageDesktopDev.app/Contents/MacOS/vantage-desktop permissions
accessibility True / post_events True
```

That is exactly the trap `docs/DESKTOP_PERMISSION_MODEL.md:6-29` documents: a
shell-run query reports *the terminal's* grant, not the app's. The same file
records the app-side answer as `false` (`docs/DESKTOP_PERMISSION_MODEL.md:15-19`;
also `docs/PROJECT_STATUS.md:4444-4447`: "a never-granted binary reports
`accessibility: true` launched from this shell and `false` launched from
Finder"). **I did not re-measure the app-side value** — that needs a request from
the running app's backend and a token I do not have (`~/.jarvis-forge/token`
absent), and I was told not to disturb the running app. That specific number is
`UNVERIFIED` this session; the mechanism is documented and consistent with what I
did measure.

**Where the record is stale.** `blocked_on` says
`"Post-event access for native/desktop_executor/VantageDesktopDev.app"`. The
measured model says the opposite: `docs/DESKTOP_PERMISSION_MODEL.md:31-35` —
"Granting Accessibility to `VantageDesktopDev.app`, or to the executor binary,
does not help: neither is the responsible process when Vantage.app drives them.
The grant has to exist for **`macos/Vantage/Vantage.app`** itself." A prior
session evidently chased the dev-bundle grant and found it does not work. The
required action is a person granting `macos/Vantage/Vantage.app` in System
Settings → Privacy & Security → Accessibility, then relaunching from Finder
(`docs/DESKTOP_PERMISSION_MODEL.md:43-55`), and **not rebuilding afterwards** —
the bundle is ad-hoc signed, so a rebuild changes the CDHash and orphans the
grant (`codesign -dv macos/Vantage/Vantage.app` → `Signature=adhoc`).

**Surprise / contradiction.** The rest of the V15 record is careful and holds up
(T1 level 3, T2-T8 permission-blocked level 1, T10 409, defect about TextEdit
registration). Only the remedy names the wrong identity. That matters: a future
worker following `blocked_on` literally would ask the owner to grant the dev
bundle, which the project's own measurement says cannot work.

**Acceptance status.** Items 2, 3, 6 and the input half of 1 depend on posting
events; none of them can move until the app-level grant exists. Items 4, 5 and 7
are recorded PASS and I did not re-run them (the V15 test files were not part of
this audit; `not_verified` in the record is honest about that).

---

## R-release-macos-dmg-artifact — `STALE_RECORD`

**The owner decision is no longer open.** The record says the DMG "currently
packages the Tk-launcher bundle … The intended macOS GUI is the native SwiftUI
app … Two bundles by design. Not a defect — a product decision about which one
ships." That decision was made on 2026-10-27:

- `installers/macos/build_dmg.sh:26` — `SHIP_NATIVE=1`, with the comment at
  `:18-25` explaining the change ("This default used to be the Tk launcher …
  The native app is the product; the launcher is kept only for the TCC/permission
  path and as an explicit fallback").
- `installers/macos/build_dmg.sh:42-43` — native path runs
  `cd macos/Vantage && swift build -c release && ./assemble_app.sh release`;
  `SRC_APP="macos/Vantage/Vantage.app"`.
- `docs/RELEASE_AND_BUILD.md:180` — the artifact table lists
  `dist/Vantage-<ver>.dmg` as the **native app** (`com.jarvisforge.vantage`),
  "built + signature verified 2026-10-27"; `:190` states it as a decision.
- `tests/test_r106_dmg_ships_either_artifact.py` — 3 tests pin the default, the
  shipped-id print, and that the doc no longer says the launcher ships.
  **Ran: `3 passed`**, with `test_r88`/`test_r94`/`test_r95` in the same
  invocation: `36 passed, 1 warning in 3.89s`.

**Both bundles are real and distinguishable**, so the "two bundles by design"
half of the record is still true:

```
$ /usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" dist/Vantage.app/Contents/Info.plist
com.jarvisforge.app          # Tk launcher, Contents/MacOS/JarvisForge (75 KB)
$ /usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" macos/Vantage/Vantage.app/Contents/Info.plist
com.jarvisforge.vantage      # native, Contents/MacOS/Vantage (25 MB)
$ codesign -dv dist/Vantage.app            → Identifier=com.jarvisforge.app,   Signature=adhoc
$ codesign -dv macos/Vantage/Vantage.app   → Identifier=com.jarvisforge.vantage, Signature=adhoc
$ codesign --verify --strict <each>        → exit 0 / exit 0
```

`dist/Vantage.app` is current with the launcher: `dist/.../MacOS/JarvisForge` is
dated `2026-11-17 14:39`, `app/launcher.py` `2026-11-17 14:38` — the stale-record
evidence note from iteration 82 still holds for that bundle.

**What is genuinely missing** (and is *not* what the record says): no DMG exists
on disk today — `ls dist/*.dmg` → `no matches found`; `dist/` holds only
`Vantage.app/` and `build/`. The 2026-10-27 build is recorded
(`docs/PROJECT_STATUS.md:1640-1642`, `:1705`) but is a gitignored artifact
(`.gitignore:77` `dist/`), so it does not survive. `scripts/release_verify.sh`
still builds the **Tk launcher** and zips it (`release_verify.sh:21-35`), and it
is referenced only from two docs — not from `installers/build_release.sh`, which
calls `build_dmg.sh` (`:18`). So there are two release paths with different
artifacts, and that — not the DMG default — is the live inconsistency.

---

## R7-android-companion — `GENUINELY_BLOCKED_EXTERNAL`

**Device/emulator: blocked, as recorded.**

```
$ which adb        → adb not found
$ adb devices      → command not found
$ which emulator   → emulator not found
$ ls "$HOME/Library/Android/sdk/emulator"       → No such file or directory
$ ls "$HOME/Library/Android/sdk/system-images"  → No such file or directory
$ ls -d "$HOME/Library/Android"                 → No such file or directory
$ echo $ANDROID_HOME $ANDROID_SDK_ROOT          → unset / unset
```

**New finding the record does not capture — the build is now blocked too.** The
2026-10-28 evidence says `:app:testDebugUnitTest BUILD SUCCESSFUL` and
`:app:assembleDebug -> app-debug.apk (19 MB)`. That cannot be reproduced on this
machine as it stands:

```
$ java -version              → "Unable to locate a Java Runtime"
$ /usr/libexec/java_home -V  → "Unable to locate a Java Runtime"
$ ls /Library/Java/JavaVirtualMachines/*          → no matches
$ ls ~/Library/Java/JavaVirtualMachines           → No such file or directory
$ ls /opt/homebrew/opt/openjdk*                   → no matches
$ ls /opt/homebrew/Cellar | grep -i jdk           → (nothing)
$ ls android/app/build                            → No such file or directory
$ find . -name '*.apk' -not -path './.git/*'      → (nothing)
```

`android/gradle.properties:4` says "Set JAVA_HOME to a compatible JDK before
running any Gradle command"; `android/build_apk.sh:24-36` tries `java_home` then
`/opt/homebrew/Cellar/openjdk@17` — neither exists. `~/.gradle` survives (1.2 GB
of caches), so dependencies are not the problem; the **JDK** is. `adb` is part
of platform-tools, which is absent along with the rest of the SDK.

So the blocker is now two-level: (1) no device/emulator for runtime, and (2) no
JDK for the build or the tests. Both are external to the repo. The stored record
is honest about (1) and stale about (2) — it claims a build that this machine
can no longer perform.

`UNVERIFIED`: whether the 2026-10-28 build result was real when recorded. I did
not re-run Gradle (it would fail on the missing JDK, which proves the environment
but not the earlier claim).

---

## E12-release — `NEEDS_WORK`

**Its only stated blocker is already resolved.** `blockers` reads "Owner decision:
which macOS artifact ships in the DMG (native GUI vs Tk launcher)." That decision
was made 2026-10-27 and is pinned by tests and docs (see
`R-release-macos-dmg-artifact` above). E12 is therefore not blocked by the thing
it says it is blocked by.

**What is genuinely unverified vs merely unrecorded:**

- *Merely unrecorded* — the artifact decision. It is recorded in
  `docs/RELEASE_AND_BUILD.md:180-200` and `tests/test_r106…`, just not in E12's
  own record. Its `evidence` is `{}`.
- *Genuinely unverified* — E12's `acceptance` is the placeholder `"to be detailed
  as the epic is worked"`. The epic has **never been scoped**, so there is no
  acceptance list against which "release ready" could be claimed. The pieces that
  exist are individually verified: native app builds and signs
  (`codesign --verify --strict` exit 0), the DMG script defaults to it and
  reports the shipped bundle id, and the release pipeline tests pass
  (`3 passed`). What does not exist is an end-to-end, current, on-disk release
  artifact: no DMG and no APK are present (`ls dist/*.dmg` and `find . -name
  '*.apk'` both empty).
- *Genuinely blocked externally* — the Android leg of "end-to-end release
  readiness" inherits `R7`'s device/JDK block.
- *Contradiction that should be resolved while scoping E12* — two release paths
  disagree about the macOS artifact: `installers/build_release.sh:18` calls
  `build_dmg.sh` (native), while `scripts/release_verify.sh:21-35` builds and
  zips the Tk launcher. Both are live scripts; nothing invokes either from CI
  (`grep dmg .github/workflows/ci.yml` → no hits).

Disposition `NEEDS_WORK`: not blocked on the owner any more, but not closeable —
it needs a real acceptance list and a current artifact before it means anything.

---

## E11-continuity — `STALE_RECORD`

**Three of its four acceptance items were already recorded DONE**, and its fourth
— "The macOS app and the dashboard share one conversation store (NOT DONE:
architecture change)" — **has since been implemented**.

`jarvis/core/conversation.py:272-370` (`list_conversations`) now merges both
stores, and its own docstring explains precisely the problem the record says is
unsolved: "`GET /ai/chats` used to list only `ai_chats` … so a conversation held
in the macOS app (which speaks `/command` and writes `messages` keyed by
`session_id`) existed on the server and was unreachable from any listing." The
merge joins `ai_chats` rows with `messages` rows grouped by `session_id`, marks
each entry `surface: chat | pipeline | both`, and de-duplicates on the id
(`:300-368`). The macOS app does write through this store
(`macos/Vantage/Sources/Views/CommandCenterView.swift:709` posts `/ai/chats`,
`:716` posts messages; its command turns carry `sessionID` at `:809`).

Provenance: the merge is in `b559bf3` ("Add mission contracts, ProofGraph, and
repair the CI foundation", 2026-11-14 18:56 +0530), which
`git merge-base --is-ancestor b559bf3 HEAD` confirms is an ancestor of HEAD. The
E11 record itself is dated 2026-11-13 — **one day earlier**. So the record was
accurate when written and was overtaken within a day.

Tests, run now:

```
$ .venv/bin/python -m pytest tests/test_r22_shared_conversation.py -q -p no:cacheprovider
25 passed, 1 warning in 0.33s
```

`tests/test_r22_shared_conversation.py:399` asserts an entry's
`surface == "pipeline"` — i.e. it pins exactly the merged-listing behavior E11
lists as not done.

**Not re-verified this session:** the three items recorded DONE on 2026-11-13
(resume-on-load, `GET /ai/chats` + History panel, text-not-markup). The endpoint
and dashboard calls are present (`jarvis/api/app.py:6293`, `dashboard/ai_chat.html:678,712`);
the recorded artifacts (`artifacts/ai-chat-history-panel.png`) were not opened,
and `tests/test_dashboard_browser_flows.py` was not run.

Disposition `STALE_RECORD`: the one open acceptance item has landed; E11 should be
re-scoped to what remains (if anything) rather than carried as pending.

---

## E7-calls-contacts — `COMPLETE_AS_RECORDED`

The record says "Call preparation + contacts DONE; actually placing a call needs
an approved telephony integration or the user opening their calling app." Both
halves hold.

**Preparation is a read, and says so.** `tests/test_r102_communications.py:1-4`:
"Preparation is a *read*: it finds the contact, open tasks that name them, recent
mail from them, and returns a briefing. It never claims a call happened." Ran:

```
$ .venv/bin/python -m pytest tests/test_r102_communications.py -q -p no:cacheprovider
3 passed, 1 warning in 6.05s
```

One of the three drives the real page (`/operator`, fills `#contact-name` and
`#contact-address`, submits `#contact-form`, asserts the contact appears in
`#contacts`) — a real-browser check, not a mock.

**Placing a call** is genuinely not implemented: `grep` for `call_prep|prepare_call|dial|telephony`
across `jarvis/` finds only unrelated uses of "dial" (the autonomy dial,
`jarvis/security/autonomy.py:1-68`; endpoint dialling in `jarvis/transport/endpoint.py:67`).
No telephony integration exists. Opening the user's calling app is inherently the
user's action. That residual blocker is honest.

`acceptance` is `"to be detailed as the epic is worked"` — the same placeholder
shape as E12, but here the work and its evidence exist, so the missing acceptance
text is a bookkeeping gap, not an unscoped epic.

---

## R6.4c-email-organize-operations — `BLOCKED_ON_OWNER_DECISION`

**Verified: neither mail provider implements an organize operation.**

`jarvis/integrations/email_provider.py` defines the abstract surface
(`EmailProvider`, `:146-179`: `available`, `search`, `read`, `save_draft`, `send`,
`describe`) and two implementations — `LocalMailProvider` (`:293-416`) and
`ImapSmtpProvider` (`:419-…`). Neither adds `archive`, `move`, `trash`, or
`delete`; a grep for `def archive|def move|def trash|def delete` in that file
returns nothing, and no `EXPUNGE` / `UID MOVE` / `STORE.*Deleted` appears anywhere
under `jarvis/`.

**Verified: the honest-answer half is live and explicit.**
`jarvis/core/planner.py:1644-1662` (`_email_not_supported`) returns an intent
whose note is "I can't {action} a message — this mail backend has no such
operation. I can search for it, read it, summarise it, or draft a reply." Its
docstring names the exact misuse it prevents: "'archive that email', 'move my
invoice email to work', 'delete that email' have no real operation to call, so a
file route must not turn them into a file mutation and the planner must not
invent one."

The record's test reference is stale in count only: it says
`tests/test_r95_an_email_is_not_a_file.py (12)`; the file now has **17** tests
(`grep -c "def test_"`), and all pass (part of the `36 passed` run above).

**The decision is genuinely the owner's.** Option (a) — add a real IMAP
`MOVE`/`\Deleted`+`EXPUNGE` plus a maildir equivalent — adds a new capability and
a new confirmation surface for a destructive, externally-visible operation.
Option (b) — keep the boundary — is what ships today. An agent should not choose
between "add a destructive mail mutation" and "do nothing" on the owner's behalf.
Disposition `BLOCKED_ON_OWNER_DECISION`.

---

## R6.4-capability-registry-gate-or-delete — `BLOCKED_ON_OWNER_DECISION`

**The danger the record cites is real, and I reproduced it:**

```
$ .venv/bin/python -c "from jarvis.security.providers import CapabilityRegistry, CapabilityType; \
  print(CapabilityRegistry().can_handle_request('9router','ag/gemini-2.5-flash',[CapabilityType.TEXT]))"
False
```

A provider that answers the assistant's questions right now is judged incapable
of a text request, because the registry is only told about capabilities
explicitly and nothing in the live path tells it
(`jarvis/security/providers.py:211-228`, whose docstring says it is
"**not** wired as a request gate"). Wiring this as a gate would refuse real
traffic.

**Verified: the gate is still dead.** `check_ai_capability` is defined at
`jarvis/core/orchestrator.py:168-170` and has **no callers** — a grep across
`jarvis/`, `tests/`, `dashboard/` and `macos/` finds only the definition and the
test/doc references. `find_compatible_providers` is likewise uncalled outside
its own module and the R88 test. The R88 assertions pin this
(`tests/test_r88_dead_capability_code_is_legible.py:98-110`), and
`tests/test_r94_verified_capability_report.py:252` goes further, asserting
`check_ai_capability(` does **not** appear in the AI-providers route.

**Verified: the reporting half is live.** `GET /ai/providers/{provider_id}/health`
(`jarvis/api/app.py:1317`) calls `capability_report` (`:1353`), which derives
`verified` from the route-health ledger rather than from declarations
(`jarvis/security/providers.py:230-282`); `:1384` comments "A report, not a
gate." Ran `tests/test_r94_verified_capability_report.py`: **11 passed**.

**One stale detail in the record.** Its blocker says "Deleting the tracked module
or the owner's untracked `providers.py.multimodal` copy is not an agent
decision." That file **does not exist**:

```
$ ls -la jarvis/security/providers.py.multimodal  → No such file or directory
$ ls jarvis/security/                             → 12 .py files, no .multimodal
$ git status --short jarvis/security/             → only " M jarvis/security/policy.py"
```

So half of the stated reason not to act has evaporated: there is no longer an
untracked owner file at risk. What remains — wire the gate (dangerous) vs delete
a tracked module (an architectural call) — is still the owner's. Disposition
`BLOCKED_ON_OWNER_DECISION`, with the record's rationale needing an update.

---

## Cross-cutting notes

- **Three records describe a world that has moved** (`R-release`, `E11`,
  `R6.4-capability`'s `.multimodal` detail). The tracker is otherwise accurate:
  the `completed` entries I sampled while chasing these (R2.2, R8, R8.9) matched
  the tree.
- **The environment has regressed since the Android evidence was recorded** — no
  JDK, no SDK, no `adb`, no APK. Any plan that assumes "just re-run the build to
  confirm" will fail for an environmental reason, not a code reason.
- **Two macOS release paths persist** (`installers/build_release.sh` → native
  DMG; `scripts/release_verify.sh` → Tk launcher zip). E12 should resolve this
  when it is scoped; it is the substantive release inconsistency left, and it is
  not mentioned in any of the eight records.
- **Untouched, per instruction:** `.autodev/tasks.json` was not modified, and
  nothing under `jarvis/`, `macos/` or `native/` was written. Read-only
  subprocess queries (`vantage-desktop permissions|identity`, `codesign -dv`,
  `PlistBuddy`) were the only executions against those trees; no input was
  posted and no GUI action was taken.