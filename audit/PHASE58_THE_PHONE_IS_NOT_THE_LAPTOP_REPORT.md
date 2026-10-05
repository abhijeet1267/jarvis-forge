# Phase 58 — the phone is a surface, not a second copy of the laptop

| | |
|---|---|
| Phase | 58 (the roadmap's "native mobile shell", re-scoped by a measured blocker — see §1) |
| Date | 2026-10-05 |
| Starting commit | `16468e6` (Phase 57 close-out) |
| Ending commit | `dd63bf2` (the work) → the close-out commit named in §9 |
| Tests before | 2273 passed · 0 failed · 0 skipped |
| Tests after | **2305 passed · 0 failed · 0 skipped** (70.2 s) |
| Tools / gated | 145 / 30 → **145 / 30** — no capability added, one gate narrowed |
| Planner routes | 52 → **52** |
| HTTP surface | 119 / 113 paths → **122 / 115** (sign-out, session list, revoke-one) |
| Push status | **nothing pushed; local commits only** |

---

## 1. Why this phase is not a native app, said plainly

The roadmap asks for an iOS/Android shell with keychain, biometrics, push and deep links. Before
planning anything, the machine was asked whether it can build one:

```
xcodebuild            -> requires Xcode; the active directory is /Library/Developer/CommandLineTools
xcrun --sdk iphoneos  -> SDK "iphoneos" cannot be located
security find-identity -v -p codesigning -> 0 valid identities found
which adb             -> not found
~/Library/Android/sdk -> absent
a handset to verify on -> none
```

**`BLOCKED_EXTERNAL_DEPENDENCY`.** A store app cannot be compiled, signed or *verified* here, and
this project's rule is that an unverified artifact is not a delivered one. The user chose the
alternative explicitly: harden the web client, keep going, and never relabel `/phone` as native.
A guard now enforces the naming part — `phone.html`, `sw.js` and `manifest.webmanifest` may not
contain "native app" — and it caught my own comment doing exactly that within minutes of being
written.

## 2. The hole the measurement found

`dashboard/phone.html` calls **ten** endpoints, grepped out of the file rather than remembered. A
phone that finished `POST /auth/session` held a token that reached **every** protected route:
`/privacy/delete`, `/audit`, `/health`, memory, workspaces, schedules. `source: "phone"` was
metadata, not authority. `clear_dashboard_credentials()` existed with no production caller, and no
session could be ended selectively — so a handset being wiped, sold or handed to a child stayed
authorised for eight hours with nothing anyone could press.

## 3. The design

**The surface is fixed when the code is minted, not when it is spent.** `/phone` mints a
`phone`-scoped bootstrap, `/dashboard` mints a `dashboard`-scoped one, and the session inherits
that. The redeem body's `surface` field is ignored — tested, because that is the difference between
a gate and a suggestion. `/auth/token` re-issues into the *caller's* surface, so a phone cannot
launder a wider token through its own narrow one.

Enforcement sits in `require_auth`, in one place, before the handler. The phone's allowlist is the
measured ten paths plus `/confirm/{id}` as a prefix; `/auth/session` and `/auth/token` are exempt
because neither act can widen a surface, and a phone that cannot sign itself out is a phone with no
escape hatch. The refusal names the surface, never the credential, and writes a `denied` audit row.

**The dashboard was left unrestricted on purpose, and that is recorded as 58-A rather than tidied
away**: the panel is the operator's console on the machine itself; the phone is remote, small and
losable. Narrowing a console is a decision with a reason attached, not a detail to change in
passing.

## 4. Three defects, all found by pressing things in a browser

**4.1 The deep link landed nowhere.** `/phone?focus=approvals` scrolled at parse time, but the
quick-actions row is rendered from `/tools` *after* that, pushing the approvals card down. In a
320×568 viewport: `scrollY` 31, card top 590 — off screen, while the DOM insisted it had focused
the element. Fix: apply the landing at parse *and* after the first render settles, with the
one-shot flag set only by the settled call. Re-measured: `scrollY` 179 (the page's maximum), card
on screen, focused. Negative control: the same page without the parameter stays at `scrollY` 0 and
unfocused, which proves the parameter did the work.

**4.2 A guard that could never run.** The first version of "stop polling once the credential is
refused" sat in `refresh()`'s `catch`. But the two awaited calls are `call("/devices").catch(() =>
[])` — they swallow their own rejections, so `refresh()` never threw, the catch never ran, and the
guard was dead code that passed its own source-level test. Measured in the browser: after signing
out, console errors went **12 → 129** while the page cheerfully reported "not paired". The check
moved above the awaits, where the calls it prevents have not happened yet. After the fix: **3**
errors in 22 seconds and one honest line — "signed out or expired on this phone — reload to use it
again". The test now asserts the guard's position relative to `Promise.all`, not merely that the
words exist.

**4.3 The cache version was a lie waiting to happen.** `sw.js` shipped one cache name with no
upgrade story; a phone that installed the old shell keeps it under the old name. Since the page
changed, the name became `jarvis-phone-shell-v2` and `activate()` deletes anything else — with a
test that fails if the version is not bumped, so the next shell change cannot silently skip it.

## 5. Live, on an isolated home and port

Nothing here touched `~/.jarvis-forge` or the delivered app. Tokens were never printed.

```
exchange the phone code for a session        -> HTTP 200, 43-char session (not shown)
phone session GET /tools /confirmations /devices /voice/status   -> 200, 200, 200, 200
phone session POST /command ("what time is it")                  -> 200, answered
phone session POST /emergency_stop                               -> 200, verified
phone session GET  /health                   -> 403 "This phone is not allowed to reach /health…"
phone session GET  /audit                    -> 403, same shape
phone session POST /privacy/delete           -> 403
   memory rows before / after the refusal    -> 1 / 1  UNCHANGED
phone session GET /auth/sessions             -> 403 "A session cannot manage sessions."
scope refusal rows in the audit              -> 3, result=denied, no credential in any row
GET /auth/sessions with the master token     -> [{"session_ref":"VTw6hQMBaDg","surface":"phone","expires_in":28799}]
   any row contains a usable token?           -> False
DELETE /auth/session (this phone)            -> {"revoked":true}
   that session GET /tools afterwards         -> 401
   a second live session                      -> still 200, untouched
redeem a phone code claiming surface="dashboard" -> GET /health 403  (the body did not win)
```

## 6. Browser verification (the phone page changed)

Real page, real server, at 320×568 and 375×812: sign-out control present and working; deep link
lands with a negative control; no horizontal overflow (`scrollWidth - clientWidth` = 0); console
errors 0 before signing out, 3 bounded errors after. The panel was not changed this phase, so no
panel claim is made here.

## 7. Numbers, re-measured

| | before | after | how |
|---|---|---|---|
| Test suite | 2273 passed | **2305 passed · 0 failed · 0 skipped** (70.2 s) | `JARVIS_HOME=$(mktemp -d) .venv/bin/python -m pytest -q` |
| Phase 58 tests | 0 | **32** | `tests/test_phase58_the_phone_is_not_the_laptop.py` |
| Test modules | 84 | **85** | `ls tests/test_*.py \| wc -l` |
| Phase reports | 59 | **60** (this file) | `ls audit/*.md \| wc -l` |
| Lines | 70,190 — 41,887 · 28,303 | **70,880 — 42,191 source · 28,689 tests** | the Phase 57 `find` command, unchanged |
| Tools / gated | 145 / 30 | **145 / 30** | registry; no capability added |
| Planner routes | 52 | **52** | `len(planner._rules)` |
| HTTP surface | 119 / 113 paths | **122 / 115 paths** | `len(app.routes)` |

## 8. Open items

* **58-A — the asymmetry is a decision, not a setting.** Dashboard sessions still reach
  everything. Narrowing them means listing what the console legitimately calls, which is a real
  piece of work with a real chance of breaking a panel card; it should be done deliberately or not
  at all.
* **58-B — a phone can approve its own gated actions.** The approvals list is on the phone, so a
  losable handset can confirm a destructive command it just sent. The mitigation today is the
  pairing key and the eight-hour TTL; neither is a biometric. This is a governance question the
  roadmap's "biometrics" item was really about.
* **58-C — the credential is still readable by page script.** Scoping limits what a stolen session
  *can do*, not how it can be read. Moving to an `HttpOnly` cookie is the answer and is a separate
  change with CSRF consequences that need their own design; doing it halfway would be worse.
* **58-D — `BLOCKED_EXTERNAL_DEPENDENCY`, the headline.** No Xcode, no iOS SDK, zero signing
  identities, no Android SDK, no handset. A store build is not produced, not signed, and not
  verified by this phase, and nothing shipped here claims otherwise.
* **58-E — no push, so the deep link only helps if something else tells you.** The link is the
  receiving half of a notification that does not exist yet.
* **58-F — the session list shows a reference and a countdown, not an origin.** No device id, no
  IP, no created-at, because the session store never learned which device spent which code. Telling
  two handsets apart today means guessing from `expires_in`.
* **58-H — the laptop can manage sessions through the API and not through the panel.** There is no
  Devices-card row for "three dashboard sessions and one phone, signed in at 09:12, ending in
  4 hours". Revoke exists; the surface to reach it calmly does not, and `READ ME FIRST.txt` now says
  so rather than implying a button.
* **58-G — sessions are in-memory, so a server restart signs everyone out** and the page re-mints a
  bootstrap on reload. It self-heals, but "the laptop restarted at 3am and my phone quietly lost
  its session" is a state nobody is told about.

## 9. Delivered

Rebuilt from `dd63bf2` (`codesign verify: ok`), the previous copy moved aside and removed only
after the new one answered, deployed to `~/Downloads/Vantage/Vantage.app`, relaunched, and both
archives rebuilt — `Vantage.app.zip` re-verified **after extraction** ("valid on disk, satisfies its
Designated Requirement"), which is the check Phase 40 established.

Verified on the delivered app, and **with GETs only**:

```
GET /health          -> status ok | tools 145 | llm_enabled false
GET /auth/sessions   -> [{"session_ref":"H7b...","surface":"dashboard","expires_in":28763}]
GET /phone           -> ships id="signout"
GET /sw.js           -> serves jarvis-phone-shell-v2 (3 sites)
bundle source        -> PHONE_ALLOWED present in the staged jarvis/api/app.py
audit rows before/after these checks -> 525 / 525   nothing was written
```

The session line is the useful proof: the panel's own browser session is published as
`surface: dashboard` by the shipped build, so the tagging works outside the test suite. A refusal
was deliberately **not** exercised against the delivered app — a scope refusal writes a `denied`
audit row into the user's real history, which is the mistake Phase 54's §11a records; the refusal
path was proven on the isolated instance instead (§5).

No login item was installed, and the user's own instance was left running.
