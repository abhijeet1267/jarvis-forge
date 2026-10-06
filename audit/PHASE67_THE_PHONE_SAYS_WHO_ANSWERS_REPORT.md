# Phase 67 — the phone says who answers, and no gate moved to let it speak

Date: 2026-10-05. Repo `jarvis-forge/`, HEAD at start `2d42ba6` (Phase 66).
Closes: **63-A** (the phone names no vendor) and **63-B** (`provider_paths` had no reader).

## 1. Measured first

| Check | Result |
| --- | --- |
| `grep -n "health\|llm\|model\|provider" dashboard/phone.html` | **nothing** — the page in a person's hand cannot say whether rules or a vendor answers, five phases after the vendor became selectable (Phase 60) and two after the laptop's panel grew a badge that names it (Phase 63) |
| `grep -rn "provider_paths" jarvis/ dashboard/ tests/` | published at `jarvis/api/app.py:645`; the only reader in the whole repository was a test |
| `PHONE_ALLOWED` | 8 paths + the `/confirm/` prefix; `/health`, `/audit`, `/privacy/delete` answer a phone session **403** (Phase 58's narrowing) |
| `provider_capabilities()` with an unusable `JARVIS_AI_PROVIDER` | returned **2 keys**; with a working one, **4** — so asking "and screen text?" on the refusal path got silence, which reads as "nothing to report" rather than "nobody looked" |

## 2. The design decision, taken in the open

The cheap way to close 63-A is one line: add `/health` to the phone allowlist. That was refused, and
the refusal is now a test. `/health` answers tools, agents, memory counts, security posture,
peripherals and the reachable URLs — a status line needs none of it, and a phone that can read it can
read whatever is added to it next. Phase 58 spent a whole phase making the phone's surface fixed at
code-mint time; a badge is not a reason to hand some of it back.

So the value travels the way the bootstrap code already does: **rendered into the HTML by the route
that serves the page** — the same document the phone is already allowed to load, which the service
worker already refetches from the network before it will consider a cache.

```html
<meta name="jarvis-bootstrap" content="__JARVIS_BOOTSTRAP__" />
<script type="application/json" id="jarvis-model">__JARVIS_MODEL_JSON__</script>
```

`model_status(settings)` builds it from the same `provider_capabilities()` the panel reads, with the
two rules Phase 63 established for badges: **no key → the line says rules and names no vendor nobody
will call**, and **an unusable selection is printed as the refusal it is** rather than falling back
to a vendor name. The JSON is `<`-escaped so no value, whatever a vendor's own error text contains,
can close the script element it sits in.

## 3. The change

- `jarvis/api/app.py` — `model_status()`, and a second placeholder replaced in `phone_client()`.
  **No new route** (`118` route objects / `111` paths before and after), and `PHONE_ALLOWED` is
  untouched at 8 paths.
- `jarvis/gateway/providers.py` — the refusal branch now describes all four tasks, adding
  `vision`/`stt` as `not asked -- the vendor could not be chosen`.
- `dashboard/phone.html` — a pure `modelLine(m)` renderer plus the wiring that fills `#modelline`,
  using the page's existing `.sub micline` classes (no new CSS, no new colour). It bails out if the
  element is absent and prints `answers: unknown (this laptop sent no model state)` if the data
  cannot be parsed: an unparseable status is reported, never guessed over.
- `dashboard/sw.js` — shell cache `jarvis-phone-shell-v2` → **v3**, because the shell gained an
  element and a phone holding the old cache would otherwise render the line from a page that has no
  place for it. `activate()` deletes every cache that is not the current name, which is what makes
  the bump mean anything.
- `tests/test_phase58_the_phone_is_not_the_laptop.py` — one assertion changed, and the reason is in
  its docstring: it read `assert "2" in names`, a literal for one day's shell, which failed the first
  time the shell legitimately advanced. It is now `max(names) >= 2` plus
  `assert "caches.delete" in body`. **This is not a weakening** — the version still has to exist,
  still has to be at least the shipped one, and the delete-the-old-caches branch is now required too.
  It is the same correction Phase 57 applied to the frozen `== 28` gate counts: a total freezes, an
  invariant holds.

## 4. Tests — 14, red first as 7 failures and 4 errors

| Group | What is pinned |
| --- | --- |
| `model_status` (3) | no key → `rules (no key)` and no vendor named; key + provider → the vendor that would be called; unusable name → the refusal, naming what it refused |
| `provider_capabilities` shape | four keys in the healthy **and** the refusal branch, and neither vision nor stt left blank |
| the served page (3) | the JSON block is present and parses to the six fields; `__JARVIS_MODEL_JSON__` never survives to a phone; the master token and `api_token`/`secret`/`Bearer` appear nowhere in the injected data |
| the gate (2) | `PHONE_ALLOWED` equals the set Phase 58 shipped, asserted as an **equality** with a diff in the failure message, and a real phone session minted from `/phone` → 200 on `/tools`, **403** on `/health`, `/audit`, `/privacy/delete` |
| the shipped renderer (4) | `modelLine` extracted from the page and run **under node**: rules state, vendor state with the boundary sentence, refusal state, and `{}` → `unknown` rather than blank |
| the shell (1) | the cache name has advanced to v3 and the old caches are deleted on activate |

**Full suite: 2433 passed, 0 failed in 140.11 s** (2419 + 14).

## 5. Live proof, and a browser pass at a real 375 px

Three isolated hosts (`JARVIS_HOME=$(mktemp -d)`, ports 8921/8922/8923, loopback, fake keys, the
user's home untouched). The injected block, read from what each actually served:

| Host | `answered_by` | vision / stt |
| --- | --- | --- |
| no key | `rules (no key)` | `openai-shaped, not provider-routed` |
| `JARVIS_AI_PROVIDER=gemini` + key | `gemini` | `openai-shaped, not provider-routed` |
| `JARVIS_AI_PROVIDER=mistral` + key | `refused: JARVIS_AI_PROVIDER='mistral' is not a provider this build can talk to. Known: gemini, openai` | `not asked -- the vendor could not be chosen` |

Neither of the two keyed hosts contacted anyone: the logs hold **0** references to
`generativelanguage` or `openai.com`, and both keys are fake strings. `PROVIDER_LIVE_VENDOR_CALL`
remains `NOT MEASURED`, as it has been since Phase 60.

**Browser verification: DONE.** Chrome, emulated `375x812x2,mobile`, the no-key host, page reloaded
under emulation, read from the live DOM:

```
answers: rules · no vendor on this laptop — screen text and voice are openai-shaped, not provider-routed
```

`window.innerWidth` 375, `documentElement.scrollWidth` **375** (no horizontal overflow), the line
visible at 13 px wrapping to two lines (38 px tall), 3 cards laid out, and **0 console messages**. A
screenshot was taken and inspected: the line sits under the mic row in the same muted style as its
neighbours, and the pair/refresh/sign-out buttons, the quick actions and the emergency stop are
unchanged. An earlier read at `resize_page` reported a 500 px viewport — the OS window's minimum —
so the mobile numbers above come from emulation, not from that window.

The phone session's gate was also re-proved against a running process rather than a TestClient:
`/tools` 200, `/voice/status` 200, and **403** on `/health`, `/audit`, `/memory`, `/permissions`,
`/metrics`, and `POST /privacy/delete`. All three hosts were stopped and their temp homes removed;
the delivered app was re-checked with one authenticated `GET /health` → 200.

## 6. Numbers

| Measure | Before | After |
| --- | --- | --- |
| Tests | 2419 | **2433** (14 new) |
| Test modules | 93 | **94** |
| Phase reports | 68 | **69** |
| Route objects / paths / authored decorators | 118 / 111 / 118 | **unchanged** — no route was added |
| `PHONE_ALLOWED` paths | 8 | **8** |
| Tools / gated / planner rules / tables | 148 / 32 / 52 / 32 | unchanged |
| Versioned lines | 76,144 | **76,467** (shipped 44,444 · tests 31,405) |
| Browser verification | Phase 66, laptop panel 1440×900 | **phone page, 375×812 mobile emulation, 0 console messages** |

## 7. What this phase did not do

- Did not widen any gate. `PHONE_ALLOWED` is pinned by equality, and the live 403s are in §5.
- Did not contact a vendor, and did not pretend otherwise.
- Did not delete the 6 `forged` audit rows — still waiting on an explicit yes; the chain still reads
  BROKEN, now with the reason and the coverage printed.
- Did not touch the device rows, as instructed.
- Still open on the phone: **58-C** (the session token lives in a page variable, so any script that
  ever runs in this page can read it — the mitigation is that it is never persisted), **58-B** (a
  phone can approve its own gated actions), **58-E** (no push). Those are the ones a native shell or
  an httpOnly-cookie session would address, and both change auth mechanics rather than a label.
- **67-A, new and deliberate:** the model line is rendered once, when the page loads. A laptop that
  gains a key while the phone sits open keeps showing the old answer until a reload. The alternative
  -- a polled endpoint -- is the surface this phase refused to add, and a status line is not worth a
  new reachable path.
