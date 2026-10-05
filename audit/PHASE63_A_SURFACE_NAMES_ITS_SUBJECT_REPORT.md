# Phase 63 — a surface has to say who it is talking about

| | |
|---|---|
| Phase | 63 (post-final-audit follow-up: open items 60-C and 61-A, plus the flake the final audit had not finished) |
| Date | 2026-10-05 |
| Starting commit | `02870f8` (cold-start addendum close) |
| Ending commit | `149412d` (the work) → `8b93485` (the close-out; its own hash is written by the next commit to touch this file) |
| Tests before | 2387 passed · 0 failed |
| Tests after | **2396 passed · 0 failed**, verified on **8 consecutive full-suite runs** including two slow ones (125 s, 167 s) |
| Tools / gated | 148 / 32 → **148 / 32** — no capability added |
| Planner rules | 52 → **52** |
| Host HTTP surface | 122 objects / 115 paths → **unchanged** |
| Browser verification | **DONE** — the panel changed; both states checked in a real browser at 1440×900, 0 console errors |
| Push status | **nothing pushed; local commits only** |

---

## 1. Two open items, one theme

Both are Phase 50's rule — *a state must name its subject* — applied to the two places that grew
after it.

**60-C.** The panel's badge read `model: cloud` or `model: rules`. After Phase 60 there are two
vendors behind the word "cloud" and the badge named neither: the smaller twin of the bug that phase
removed, a status element implying more precision than the system has.

**61-A.** `get_desktop_control_state` answered whether *the process that asked* may click. macOS
attributes a permission to the responsible process — Phase 61 measured GRANTED from a terminal and
`REQUIRES_MANUAL_SYSTEM_SETTINGS` for the app — so "Desktop control is available" was a sentence
about one thing being read as a statement about another. The permission service has carried the
answer since Phase 50 (`answered_by`, `about_bundled_host`, `status_note`); the desktop read simply
did not use it.

## 2. What the panel says now, and what it refuses to say

```js
const model = h.llm_enabled ? (h.ai_provider || "a configured model") : "rules (no key)";
```

The second half is the part with a decision in it: with no key, the badge does **not** print the
vendor that happens to be the configuration default. Naming a vendor nobody will call is the badge
lie Phase 60 measured, in a smaller font. `ai_provider` was already served by `/health` (Phase 60),
and a test asserts the server still carries the field the page reads, so the two cannot drift.

## 3. What the desktop read says now

Every answer carries the subject in the sentence and in the output (`about_bundled_host`,
`status_note`) — granted, refused, and unverified alike, because the subject is metadata on the
answer, not decoration on the happy path:

- `about_bundled_host: true` → `— read from Vantage.app itself`
- `false` → `— this is the answer for the process that asked, not the app in your Dock`
- unreadable → `— the reader could not say which process it describes`

The refusal wording keeps naming the pane to change, which is what makes it actionable rather than
merely honest.

## 4. The flake the final audit had not finished

`audit/FINAL_AUDIT_COLD_START_NOT_A_STALL.md` claimed the Vision flake was fixed on the strength of
eight clean runs. It came back the next time the suite was run. What the trace then showed is that
my fix had one hole and my test had one worse:

1. **The warm-up could poison the guard it was working around.** A warm-up call that exceeded its
   budget used the *shared* backend, so it set the 45 s cooldown itself — the cascade reappeared
   with a new entry point. Fixed by warming through a throwaway instance: the cold cost is per
   process, so a separate instance still pays it, and the shared guard state stays untouched.
2. **My own regression test poisoned the suite it was written to protect.** It repointed the
   **shared** backend's binary at `/usr/bin/true`. Every later real read in the session then
   answered `ok=True, lines=0` in 0.00 s — five failures that looked exactly like the flake under
   study, in a run that took 2 s instead of 45. It also passed a path that does not exist, which
   `read_text` refuses before the helper is ever asked.

Both are the same mistake I have now made twice in one day, and it is worth naming: *asserting a
property about a shared object by mutating it.* The test now asserts the opposite — that a stall on
one backend leaves `get_ocr_backend().cooling_down()` at zero and the shared `_binary` exactly as
found — so if anyone moves the guard to module state, or a test reaches for the singleton again,
this test says so instead of quietly corrupting its neighbours.

Slow runs are the evidence that matters here: **8 consecutive full suites, 0 failures**, including
one at 125 s and one at 167 s. Those are the moments that used to produce the five-test cluster;
they now produce a passing suite that takes longer.

## 5. Tests — 9 new, and what each one is for

`tests/test_phase63_the_panel_says_who_answered.py` (8): the shipped `applyHealth` is extracted from
`dashboard/index.html` and **run under node** — a source grep would pass on a function that never
runs — asserting the vendor is named when a key exists, the word "cloud" is gone, `rules (no key)`
appears when none does *with no vendor name*, the tool count is still shown, and `/health` publishes
`ai_provider`. Plus three desktop-subject tests (granted-bundled, granted-not-bundled, refused) and
one for the unverified path.

`tests/test_vision.py` (1): the isolation property in §4.

## 6. Browser verification, in a real browser

Two isolated hosts, one per state, `dashboard/index.html` at 1440×900, page JS left to run 2.5 s
after load:

```
host with no key (8922)        #llm -> "model: rules (no key) · 148 tools"
host with ai_provider=gemini   #llm -> "model: gemini · 148 tools"
                               bounding box right edge 330 of 1440 -> no overflow
                               document.scrollWidth <= viewport -> no horizontal scroll
                               console errors: 0
```

The `gemini` state was produced by configuration only (`JARVIS_LLM_BASE_URL` at an address that is
not reachable, `JARVIS_LLM_API_KEY` a literal non-key): the badge reads `/health`, and `/health`
reports the selection rather than a probe. **No model call was made in this phase**, so nothing here
says a vendor answered — the same line Phase 60 held itself to.

## 7. Numbers, re-measured

| | before (cold-start addendum) | after |
|---|---|---|
| Tests | 2387 · 0 failed | **2396 · 0 failed** (77 s typical; 125 s and 167 s under load, both passing) |
| Test modules | 90 | **91** |
| Tools / gated | 148 / 32 | **148 / 32** |
| Planner rules / HTTP objects / paths | 52 / 122 / 115 | **unchanged** |
| Versioned lines | 75,250 | **75,515** |
| Shipped code / tests | 44,223 / 30,409 | **44,238 / 30,659** |
| `audit/` reports | 64 | **65** (this file) |

## 8. Open items

- **63-A — the phone client has no badge at all.** It shows pairing and quick actions; the model
  line is laptop-panel only. Not assumed to be wanted.
- **63-B — `provider_paths` is published and read by nobody.** Phase 60 put the vision/STT boundary
  on `/health`; no surface shows it. Same omission, different field.
- **63-C — the desktop read's subject is inferred from where the code lives**, the same evidence
  Phase 50 called "the best available", not a proof. macOS exposes no API for the responsible
  process, so this remains the strongest statement the product can make.

## 9. Delivery

Work commit `149412d` — *Say who a status answer is about, and finish the flake fix properly*.
Close-out commit `8b93485` — *Record the Phase 63 redeploy and the browser check of the delivered
badge* (its own hash was written by the commit after it).

`dashboard/index.html` ships inside the bundle, so the app was rebuilt, re-deployed over the old
copy (kept aside until the new one answered, then removed), and verified:

| check | result |
|---|---|
| `codesign --verify --deep --strict` on the deployed bundle | valid, satisfies its Designated Requirement |
| `rules (no key)` present in the staged `dashboard/index.html` | yes — and again in `Vantage.app.zip` after extraction into a temp dir, which also re-verifies |
| **browser** on the delivered app at 8765, `/dashboard` | `#llm` reads `model: rules (no key) · 148 tools`, box ending at 371 px with no overflow, **0 console errors** |
| `GET /health` | `status ok`, **148 tools**, 15 agents |
| `GET /tools` | **148** entries, risk labels as published |
| `GET /persistence` | `supported: true`, `installed: false` — the machine left as found |
| `GET /health` with no token | **401** |
| archives | `Vantage.app.zip` 1,173,807 B and `vantage-source.zip` 2,573,354 B, rebuilt from this tree |
| suite | **2396 passed · 0 failed** — nine consecutive clean full-suite runs |

`READ ME FIRST.txt` needed no change: it never claimed the badge said "cloud", which is the reason
this phase went and read it before assuming. Nothing was pushed.
