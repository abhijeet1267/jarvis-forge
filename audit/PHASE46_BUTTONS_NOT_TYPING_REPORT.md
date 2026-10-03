# Phase 46 — Buttons, not typing

Asked for: *"make ui better with buttons and more features"*.

Closed 2026-10-03. Every number below was re-measured after the change; the ones that
moved are marked, and the two that were already wrong in the README are corrected here
rather than quietly re-published.

---

## 0. What the screens actually looked like first

Both pages were opened in a real browser and captured before anything was edited
(`phone-before-375.png`, `dash-before-1440.png`).

| Measured before | Value |
|---|---|
| Phone page: controls on screen | 3 (`pair`, `refresh`, `send`) |
| Phone page: named colour tokens | 0 — colours hard-coded (`#0d1117`, `#161b22`, `#238636`, `#6e2a2a`, `#30363d`, `#adbac7`) |
| Phone page: `:hover` / `:focus-visible` / `prefers-reduced-motion` rules | 0 / 0 / 0 |
| Dashboard: quick actions | 0 |
| Dashboard: devices listed (this machine) | 13, of which 8 are residue of my own live test drives (`phone_pair_a`, `phone_twice`, `phone_perm`, `phone_tool`, `phone_remove`, `phone_gone`, `phone_hmac`, plus a `phone_main` key file) |
| Dashboard: way to dismiss a device | none |
| Dashboard: way to find one of 134 tools | read the table |
| `/tools` payload | 134 entries, no `tags` field |

So the honest reading of "make it better with buttons" was not taste. One text box and a
wall of empty space, on the device a person is actually holding, with 134 capabilities
reachable only by inventing a sentence — and a device list that was showing my own
leftovers with no way to clear them.

## 1. The defect found while measuring, disclosed first

While probing which endpoint a Forget button could use, I ran this against **the user's
running app** (`:8765`, real `~/.jarvis-forge`):

```
POST /devices/laptop_main/revoke  →  200 {"state":"revoked"}
```

`laptop_main` is the record for the laptop the server is running on. After the call it read
`auth_state: revoked`, `key_ref: ''`, `online: false`. Nothing in the pairing layer or the
route checked that the device being revoked was the host, and no client can undo it — the
one screen that could list the laptop is the laptop's own.

**What it took to undo:** the write had gone through `registry.set_auth_state()`, which
persists, so the row needed restoring, not just re-reading.

1. `UPDATE devices SET auth_state='unpaired', online=1 WHERE device_id='laptop_main'`
   against `~/.jarvis-forge/jarvis.db` (WAL, `.timeout 8000`, 1 row changed).
2. The running process kept its own cache — `DeviceRegistry` is process-wide and
   `refresh()` is only called at construction — so `/devices` still reported `revoked`
   after the store said otherwise. Quit the app (`osascript -e 'quit app "Vantage"'`, both
   processes gone in ~3 s, which is Phase 40b's shutdown hook doing its job), reopen it,
   and the record read `unpaired` / `online: true` again.
3. Verified from the API, not from the file: `{'auth_state': 'unpaired', 'online': True,
   'key_ref': ''}` — identical to a fresh `DEFAULT_DEVICES` row.

The audit chain still contains the `device.revoke` success row for that call. It is left in
place: it happened.

**Then the hole was closed red-first** (§2), because "I will be careful" is not a fix.

## 2. The host cannot be forgotten

Written first, observed failing (`14 failed, 2 passed` at that point):

```
test_the_device_list_says_which_device_is_the_host
test_the_revoke_route_refuses_the_laptop_and_changes_nothing
test_pairing_refuses_to_revoke_the_host_directly
test_a_paired_device_can_still_be_revoked
test_an_unknown_device_is_still_404          (already green: 404 for an unknown id was right)
```

The guard sits under the route, in `PairingManager.revoke()`, because the route is not the
only caller — `remove_phone` calls the same layer, and escapes the hole only by accident
(it refuses any device that is not paired, and the laptop never was).

```python
class HostDeviceError(ValueError): ...        # the tool path already turns ValueError into
                                              # a spoken failure, so it refuses for free
if device_id == HOST_DEVICE_ID:
    raise HostDeviceError(f"{device_id} is the machine Vantage runs on. …")
```

`HOST_DEVICE_ID` lives in `jarvis/registry/devices.py` next to the row it names. The route
catches `HostDeviceError` **before** `ValueError`: every `ValueError` used to become 404,
which would have answered "no such device" about a device the same page was listing. It now
answers 409 and writes a `denied` ledger row, so a refusal is in the chain and not only on
screen.

The page does not decide which row is the laptop. `/devices` gained `is_host`, so the rule
is data from the only component that can know it.

**Proved live** (isolated instance, fresh `JARVIS_HOME`, port 8799):

```
POST /devices/laptop_main/revoke → HTTP 409
  {"detail":"laptop_main is the machine Vantage runs on. Forgetting it revokes the host,
    not a device, and no client can undo that from here."}
after the attempt: {'auth_state': 'unpaired', 'online': True, 'is_host': True}   ← unchanged
POST /devices/not_a_device_here/revoke → 404                                     ← still 404
forgetting a paired phone: state=revoked, key file gone, row reads revoked        ← still works
```

The refusal also appears in the dashboard's Recent Actions as `remove_phone · denied`
(visible in the 1440 capture). Note for the record: the ledger names the tool
`remove_phone` even when the target is the laptop — the HTTP route attributes the action to
the tool that does that job. Open item 46-D.

## 3. The buttons, and the rule they are built to

`QUICK` is a declared list of sentences, rendered through `QUICK.filter(...)` against the
tool names `/tools` returned. A button exists only while the capability does, so the
interface cannot advertise something the system has since lost.

A tap **types the sentence and runs the same `send()` as typing does**. It is deliberately
not a second path to a tool endpoint: permission, confirmation, verification and the audit
row all live on the `/command` path, and a second path is a second set of gates to keep in
sync — which is how a UI ends up advertising a capability the system no longer grants.

| Dashboard (9) | Phone (6) |
|---|---|
| Battery · Storage · Open project · Find a file · Open a site · Explain something · What I remember · Screenshot · Time | Battery · Storage · Open project · Find a file · Open a site · Explain |

A `{slot}` in a sentence is the one word a tap cannot know, so the page asks for it and
sends nothing if the answer is blank — a literal `open {site}` reaching the planner would
have the browser visit a domain made of punctuation.

**The guard that mattered was not "does the tool exist".** Two tests check that, and both
passed for a button that did not work: the first draft of the Ask button said
`ask about {subject}`, which routes to `unknown`. So every button's sentence is now parsed
by the real `RulePlanner` in the test suite:

```
test_every_button_says_a_sentence_the_planner_routes[index.html]
test_every_button_says_a_sentence_the_planner_routes[phone.html]
```

Two candidates were then measured against the planner on this build:

| sentence | parse result |
|---|---|
| `what is {subject}` → "what is a firewall" | `ask_question`, topic `a firewall` |
| `explain {subject}` → "explain what a load balancer does" | `ask_question`, topic kept whole |
| a free question: "what trains run from pune to mumbai" | `unknown` / `not_understood` |

The shipped button says `explain {subject}`: it survives a person's wording, and its prompt
says what it wants (one topic, up to 8 words — the planner's own limit).

## 4. The mic button, and the two gates in front of it

Phase 43 made `can_listen` truthful; this is the control that reads it. The button is
disabled unless **both** hold:

1. the laptop has a speech backend that actually hears (`/voice/status → stt.can_listen`), and
2. this browser will hand the page a microphone (`navigator.mediaDevices` + `MediaRecorder`,
   which a non-secure origin does not have).

Measured in a real browser, both gates separately:

| Origin | `isSecureContext` | `mediaDevices` | `can_listen` | Button | Line shown |
|---|---|---|---|---|---|
| `127.0.0.1:8799` (fresh home) | true | present | true | enabled | "tap to speak, tap again to stop" |
| `127.0.0.1:8799` + TCC marker | true | present | **false** | disabled | "the laptop has no working microphone." + *how to give this laptop ears* |
| `172.20.169.185:8798` (LAN) | **false** | **absent** | true | disabled | "…this browser will not hand the page a microphone on an insecure address" |

The third row is the state a real phone is in, and it is why the button exists now even
though the laptop still has no working ears: the two causes need different fixes, and a
single "unavailable" would send someone to the wrong settings pane.

The reason text comes from the server. `describe_stt()` gained `advice` — the same
`ears_advice()` sentence Phase 43 wrote for the failed-transcription path, which was
previously only reachable *after* an attempt, so a disabled button had nothing to say.

**Then the browser rejected my own design.** The first version printed that sentence inline:
measured at 375 px, `#micline` was **530 characters tall-rendered as 101 px of text** under a
disabled button — a paragraph a reader has to scroll past to reach the box they were using.
Now the reason is one line and the remedy is a `<details>` disclosure (38 px closed, 142 px
open). Opening it then pushed the page to **627 px inside a 375 px viewport**, because the
advice names a marker path and a path is one unbreakable word; `overflow-wrap: anywhere` +
`min-width: 0` on the flex item brought `scrollWidth` back to 375. Both are pinned by tests
(`test_the_disabled_mic_states_the_reason_without_dumping_the_remedy`,
`test_long_server_text_cannot_widen_the_phone_page`).

## 5. One palette, two devices

The phone page now declares every colour as a token, taking the dashboard's **dark** values
verbatim (`--bg #0d1117`, `--panel #161b22`, `--ink #e6edf3`, `--muted #9198a1`,
`--line #21262d`, `--accent #4c8dff`, `--ok #3fb950`, `--warn #d29922`, `--bad #f85149`,
`--radius 10px`, `--focus #6ea8ff`, …). The guard removes the token block from the
stylesheet and fails if any colour literal survives anywhere else in it — value whitelists
can be satisfied by coincidence, absence cannot.

Two colours are the phone's own and are named rather than switched: `--go #238636` and
`--on-go #ffffff`, the green its send button shipped with. Changing a primary action's
colour is a design decision and this page has no reviewer, so it is recorded as open
item 46-B instead of being quietly "unified".

The phone also gained what the dashboard already had and it did not: `:hover`,
`:focus-visible`, `:disabled` (with `cursor: not-allowed`), `prefers-reduced-motion`,
`aria-label` on every control, and 44 px minimum touch targets.

## 6. Two more defects the browser found

* **A revoked device read `OFFLINE`.** After the Forget button worked and the API confirmed
  `auth_state: revoked`, the row still said OFFLINE — the card's state word only consulted
  `online`, so "not here right now" was printed over "untrusted until it pairs again". The
  revoked branch now comes first, and the test checks the **order**, not the presence of the
  word, because the fall-through is the bug.
* **`108 routes` in the README matched no measurement.** Recounted three ways: 112 route
  objects, 112 path+method pairs, 106 distinct paths. The published figure was neither. It
  now says 112 routes / 106 paths, with the convention written next to it.

## 7. Numbers, re-measured after the change

| | before (published) | after (measured) | how |
|---|---|---|---|
| Test suite | 1856 passed · 0 failed | **1884 passed · 0 failed · 0 xfailed · 0 skipped** (53.2 s) | `JARVIS_HOME=$(mktemp -d) .venv/bin/python -m pytest -q` |
| Phase 46 tests | 0 | **28** in one new module | `tests/test_phase46_buttons_not_typing.py` |
| Test modules | 72 | **73** | `ls tests/test_*.py | wc -l` |
| Phase reports | 47 | **48** (this file) | `ls audit/*.md | wc -l` |
| Lines (py/html/js) | ~63.0k | **62,513** — 38,619 source · 23,894 tests | `wc -l` over `jarvis app dashboard tests` |
| Tools | 134 | 134 (unchanged) | `build_default_registry()` |
| Planner routes | 47 | 47 (unchanged) | `len(JarvisCore().planner._rules)` |
| HTTP routes | "108" (unmatched) | **112 routes · 106 distinct paths** | `len(app.routes)` / distinct `r.path` |
| Quick actions | 0 | **9 dashboard · 6 phone**, all routing to real tools | the two planner tests |
| Phone controls at 375 px | 3 buttons | 6 quick actions + send + mic + 2 device buttons | browser snapshot |
| Horizontal overflow at 375 px | not measured | **0** (`scrollWidth` 375 = `clientWidth` 375, disclosure open) | browser |
| Console errors | not measured | **0** on both pages, both origins | browser |
| Delivered bundle seal | valid | **valid** after rebuild (`valid on disk`, `satisfies its Designated Requirement`) | `codesign --verify --deep --strict` |

Live end-to-end through the new buttons, in a real browser:

```
phone 127.0.0.1  → tap Battery → "what is my battery" → "Your laptop battery is at 100%
                   (fully charged)." · action battery_status
phone LAN 375 px → tap Battery → same reply, over the wifi address
dashboard 1440   → tap Storage → VERIFIED · "Storage: 31.3 GiB free of 228.3 GiB on /
                   (28% used, /dev/disk3s3s1)"
dashboard filter → "storage" → "1 of 134 match" → check_storage
               → "zzzznothing" → "no tool matches “zzzznothing”. Clear the filter to
                  see all 134."
dashboard Forget → confirm → note "phone_main forgotten." → row reads REVOKED
phone slot       → tap Explain → prompt "Explain what? (one topic, up to 8 words)"
                   → "what is what a load balancer does" → real Wikipedia answer
```

## 8. Open items

* **46-A — `can_listen` is a claim about backends, not about hearing.** Measured the same
  day: a fresh home reports `can_listen: true` for `macos_speech`, and the delivered app
  reports `false` because a `.tcc_blocked` marker exists in its home. The first real attempt
  is what writes that marker, so a first tap on a machine that has never been asked can
  enable a button that then fails. The button self-corrects on the next status read; the
  initial state can still be wrong.
* **46-B — one accent per surface.** The phone's primary action is green (`--go #238636`),
  the dashboard's is blue (`--accent #4c8dff`). Both are now named; neither was changed.
  Someone with an opinion about which is right should decide, not a phase that found it
  while fixing something else.
* **46-C — eight test devices are still in `~/.jarvis-forge`.** They are mine, from live
  drives against the user's app. The Forget button now exists and they can be cleared from
  the dashboard in eight taps; they were not deleted here because they are the user's data
  and the ask was a UI, not a cleanup.
* **46-D — the ledger calls a host revoke `remove_phone`.** The HTTP route attributes the
  action to the tool that does that job, so the audit row for a laptop-targeted refusal
  names a phone tool. The `denied` result and the `detail.reason` are correct; the tool
  label reads wrong for the target. Renaming a registered tool is a state-carrying change
  (see the rename boundary), so it is recorded, not done.
* **46-E — the mic still has no working backend on this machine** (43-A unchanged). The
  button is built, gated and explained; it needs either the Speech Recognition permission
  plus the marker deleted, or whisper.cpp, or a key.
* **46-F — `tags` were not added to `/tools`.** The plan for this phase assumed a
  capability taxonomy would be needed to generate buttons. It wasn't: 9 buttons worth of
  sentences came from what was actually asked for, and annotating 134 tools to power a
  filter the filter box already solves without them would have been a taxonomy invented
  after the fact. The registry filter uses `name` + `description`, which are already real.

## 9. Delivered

`~/Downloads/Vantage/` rebuilt from this commit: `Vantage.app` (running, seal valid),
`Vantage.app.zip`, `vantage-source.zip`, and `READ ME FIRST.txt` updated with what the
buttons are, why the Mic button is disabled on a phone and not on the laptop, that the
laptop itself cannot be forgotten, and where the leftover test devices came from.
