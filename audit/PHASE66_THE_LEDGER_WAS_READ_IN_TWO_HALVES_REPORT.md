# Phase 66 — the ledger was being read in two different halves

Date: 2026-10-05. Repo `jarvis-forge/`, HEAD at start `1fcedeb` (Phase 65).
Carried over from: the "check all is working" sweep and the user's instruction to fix everything
except the device rows.

## 1. Measured first, on the delivered host

`GET /metrics` with the master token, and the same numbers read straight out of SQLite:

| What                                                    | Before                                                                                                                                                                                                                                                                                                    |
| ------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| rows in `action_logs`                                   | **525**                                                                                                                                                                                                                                                                                                   |
| `/metrics.audit.sampled`                                | 500 — `recent(limit=500)`, `ORDER BY created_at DESC` → the **newest** 500                                                                                                                                                                                                                                |
| `/metrics.audit.chain_checked`                          | 495 — `verify_chain(limit=500)`, `ORDER BY created_at ASC` → the **oldest** 500                                                                                                                                                                                                                           |
| rows never chain-verified                               | **25**, not stated anywhere                                                                                                                                                                                                                                                                               |
| rows never counted in `by_result`                       | the oldest 25, not stated anywhere                                                                                                                                                                                                                                                                        |
| chain verdict                                           | `chain_intact: false`, no reason, no row id, nothing about which half was looked at                                                                                                                                                                                                                       |
| `action_logs` rows whose `result` is not a status token | **8** — 6 with `result='forged'` and 2 holding a whole response sentence from an early build (`"Your laptop battery is at 82% (charging)."`, `"Opening Visual Studio Code on your macos machine."`); the 2 sentence rows sit older than the newest-500 window, so **no published count ever showed them** |

Two surfaces, one table, different halves. The dangerous part is not the split, it is that a green
word ("intact") was produced by a walk that had not read the newest quarter of the ledger — which is
exactly where a tamper a week later would live.

## 2. The change

- **`jarvis/security/audit.py`**
  - `verify_chain(limit=None)` now walks the **whole** ledger by default; `total` stays "what this
    walk covered" and three new fields make the difference explicit: `verified_of` (the size of the
    ledger), `unverified` (what a bounded walk did not reach), and `break_details` (per-break
    reason + `log_id` + position, next to the existing `breaks` count).
  - `result_value_counts()` asks the table for every distinct `result` value, because
    windowing a _count_ is how the 2 sentence rows went invisible.
  - `seal_chain(force=False)`: a plain call is unchanged (returns 0 on an already-sealed ledger, so
    `test_security.py::test_seal_chain_is_idempotent` still holds). `force=True` re-hashes and then
    **writes its own event row**, because a re-sealed chain proves only that nothing changed after
    the re-seal, and a reader who cannot see that date would credit the ledger with a longer
    guarantee than it has.
- **`jarvis/api/app.py`** — `_audit_summary` counts from the table rather than a window, keeps the
  honest bucket name `legacy_message` for the sentence rows (it does not rename them into a status
  that was never recorded, and does not drop them), publishes `unexpected_result_values`, and passes
  through `chain_verified_of`, `chain_unverified`, `chain_breaks`, `chain_reason`, `chain_first_bad`.
- **`dashboard/index.html`** — a new pure `auditChainLine(a)` and its use:

  ```
  intact · 525 of 525 verified
  intact · 100 of 525 verified · 425 not verified
  BROKEN · 5 records · 520 of 525 verified · record hash mismatch (a record was edited)
  ```

  plus, only when non-zero, `· result values that are not statuses 8`. A colour was never an
  explanation; Phase 50's rule is that a status has to say what it measured.

## 3. Tests — `tests/test_phase66_the_ledger_has_two_blind_spots.py`, 11

Red first: 7 failed and 3 errored before the change, and the failures were the interesting ones.

| Test                                                                            | Pins                                                                                                                                                                                         |
| ------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_verification_reaches_the_newest_record`                                   | Edits the **last** record — the one the old ASC window never walked — and requires `ok=False` with that `log_id` named. This is the regression that matters most and it was invisible before |
| `test_the_verifier_reports_what_it_checked_and_what_it_did_not`                 | A bounded walk is allowed only if it admits the bound: `verified_of` is the ledger, `unverified` is 425 for a 100-row window                                                                 |
| `test_a_break_carries_the_reason_on_the_surface`                                | A break reports its reason and its row id                                                                                                                                                    |
| `test_early_rows_that_stored_a_sentence_are_counted_whole_ledger_not_by_window` | The 2 sentence rows are labelled `legacy_message`, counted, and never invented into a status                                                                                                 |
| `test_no_surface_invents_a_status_for_a_value_it_does_not_know`                 | `forged` stays `forged` in the buckets; the row is not smoothed into `success`                                                                                                               |
| `test_seal_chain_without_force_still_refuses_to_touch_a_sealed_ledger`          | The old contract, kept                                                                                                                                                                       |
| `test_a_forced_reseal_leaves_the_ledger_verifying`                              | 525 re-hashed, and `verified_of` is then 526 because the seal wrote its own event row                                                                                                        |
| `test_a_forced_reseal_is_recorded_as_an_event`                                  | The re-seal date is _in_ the ledger, not only in a log file                                                                                                                                  |
| three `chain_render` tests                                                      | The shipped `auditChainLine`, extracted from `dashboard/index.html` and **run under node** with the exact payload keys `/metrics` publishes, including the "not verified" case               |

The fixture isolation note matters: `get_db()` is a process singleton pointed at conftest's session
database, so a test that writes 525 rows there would pollute every later module and count rows it did
not create. The fixtures build `Database(path=tmp_path/"jarvis.db")` and swap it in with
`monkeypatch`, which hands the singleton back -- the same trap as Phase 33 and Phase 63, entered
deliberately and avoided by construction.

**Focused:** 11 passed. **Full suite: 2419 passed, 0 failed in 86.12 s** (2408 + 11).

## 4. Live proof on the delivered host

`GET /metrics` after the redeploy:

```
chain_intact        False
chain_checked       520      chain_verified_of 525      chain_unverified 0
chain_breaks        5        chain_reason  record hash mismatch (a record was edited)
chain_first_bad     LEGACY-TEST
ledger_rows         525      unexpected_result_values 8
by_result … legacy_message 2 · forged 6
```

The same walk that used to report 495 of an unstated 500 now reports 520 of the real 525, names the
first bad row, and admits that nothing is unverified any more. The verdict is still BROKEN — correctly,
because the tamper rows are still there (§6).

**Browser verification: DONE**, because a page changed. A real Chrome page at **1440×900** on the
delivered app read from the live DOM:

```
Audit chain BROKEN · 5 records · 520 of 525 verified · record hash mismatch (a record was edited)
Actions recorded 525
· result values that are not statuses 8
```

`document.documentElement.scrollWidth` 1425 against a 1440 viewport (no horizontal overflow), card
box ending at 1294 px, and **0 console messages**. The tab was left open (the tool refuses to close
the last page) and nothing was clicked.

## 5. A wrong claim of mine, corrected the hard way

My previous summary said: _"the bundle loads the repo source, so one restart closes it — no rebuild
required."_ That is the pre-Phase-36 behaviour, and it was wrong: `launcher.resolve_project_dir()`
returns `Contents/Resources/app` (its own staged copy) **before** it consults `project_path.txt`.
The restarted app answered `GET /openapi.json` → **200, 82,556 bytes** for exactly that reason.

So the Phase 64 fix was rebuilt and redeployed: `build_app.py` → ad-hoc re-signed →
`codesign --verify --deep --strict` ("valid on disk", "satisfies its Designated Requirement") →
replaced `~/Downloads/Vantage/Vantage.app` (previous bundle kept at `/tmp/Vantage.app.pre-phase66`).
Proof this time was taken from inside the deployed bundle, not from the repo:
`grep -c doc_routes ~/Downloads/Vantage/Vantage.app/Contents/Resources/app/jarvis/api/app.py` → 2,
`api_docs` in `config.py` → 1, `auditChainLine` in `dashboard/index.html` → 2.

Then the live host, no token:

| Request                                                          | Before this phase                      | Now                                                                                   |
| ---------------------------------------------------------------- | -------------------------------------- | ------------------------------------------------------------------------------------- |
| `GET http://127.0.0.1:8765/openapi.json`                         | 200, 82,556 B                          | **404, 22 B**                                                                         |
| `GET http://172.20.168.82:8765/openapi.json` (the Wi-Fi address) | 200, 82,556 B                          | **404, 22 B**                                                                         |
| `/docs`, `/redoc`, `/docs/oauth2-redirect`                       | 200                                    | **404**                                                                               |
| `GET /`                                                          | 200, body advertised `"docs": "/docs"` | 200, `{"name":"Vantage Core","version":"0.1.0","dashboard":"/dashboard","tools":148}` |
| `GET /health` with the token                                     | 200                                    | 200, `tools 148`, `provider openai`                                                   |

The hole the "check all is working" sweep found is now closed on the machine that had it open.

**The archive was verified by launching it, not by listing it.** `Vantage.app.zip` was re-made with
`ditto -c -k --keepParent` (1,176,964 B) and `vantage-source.zip` with the project's usual exclusions
(2,614,391 B). Extracting the app archive with `unzip` and running `codesign --verify` reported
_a sealed resource is missing or invalid_ -- which was the extraction tool, not the artifact: with
`ditto -x -k` the same archive verifies silently. The proof that mattered came next: the
**ditto-extracted copy in `/tmp`**, launched with its own recorded interpreter against a fresh
`JARVIS_HOME` on port 8918, answered

```
GET /health       (no token)      401
GET /openapi.json (no token)      404, 22 bytes
GET /health       (master token)  200, tools 148
```

so a copy of the delivered archive carries the closed door and still requires a credential, in a home
that touches nothing. That instance was stopped and both temp directories removed.

One loose end to name rather than hide: a dashboard tab was left open in the running Chrome because
the browser tool refuses to close the last page. Nothing in it was clicked.

## 6. The ledger repair was prepared, then stopped — and that is the right outcome

Everything was staged: the app stopped cleanly, `jarvis.db` + `-wal` + `-shm` copied to
`~/.jarvis-forge/repair-2026-10-05/`, and the 8 affected rows exported to
`affected_rows.pre-repair.json`. The repair itself — delete the 6 `result='forged'` rows, then
`seal_chain(force=True)` — was **blocked**, and I did not work around the block, because deleting
historical audit rows needs an explicit yes rather than an inference from "fix all errors".

What it would do, precisely, if you say go:

| `log_id`           | created                   | tool     | intent        |
| ------------------ | ------------------------- | -------- | ------------- |
| `LEGACY-TEST`      | 2020-01-01T00:00:00+00:00 | get_time | legacy        |
| `LOG-c21a56032ad3` | 2026-09-25T14:01:49+00:00 | get_time | tamper-target |
| `LOG-088c35e5a397` | 2026-09-25T14:03:04+00:00 | get_time | tamper-target |
| `LOG-26a595850d0c` | 2026-09-25T14:05:50+00:00 | get_time | tamper-target |
| `LOG-277759cfc1fc` | 2026-09-25T14:18:23+00:00 | get_time | tamper-target |
| `LOG-d610138a63b6` | 2026-09-25T14:27:44+00:00 | get_time | tamper-target |

All six are `UPDATE`s written by `tests/test_security.py` against the default home, before conftest
gave each session its own — `tests/conftest.py` isolates `JARVIS_HOME` today, so this cannot recur.
They are not actions your assistant took. Deleting them costs 525 → 519 rows and forces the re-seal,
after which the chain verifies but proves only what happened _from that moment_; the re-seal writes
its own ledger row and the backup directory keeps the pre-repair bytes. The 2 `legacy_message` rows
are **real actions mislabelled by an old build** and I would not touch them: the honest fix already
shipped — they are now named, counted, and visible — and rewriting them would invent a status that
was never recorded. `~/.jarvis-forge/audit.jsonl`, the off-box mirror, keeps every original line
including these six regardless of what you decide about the database.

### 6.1 Executed on 2026-10-06, after being asked directly

The user chose **delete and re-seal**. Run against the real ledger, app stopped, with a second backup
taken immediately before (`jarvis.db.pre-repair-2`):

| Step                     | `verify_chain()`                                                                                           |
| ------------------------ | ---------------------------------------------------------------------------------------------------------- |
| before                   | `ok=False`, checked 520 of 525, 5 breaks, first bad `LEGACY-TEST`                                          |
| 6 rows deleted           | `ok=False`, checked 513 of 519, **6 breaks**, `previous-hash mismatch (a record was removed or reordered)` |
| `seal_chain(force=True)` | **`ok=True`, checked 520 of 520, 0 breaks, 0 unverified**                                                  |

The middle row is the point of the whole exercise being done in one go: deleting a row does not leave
a shorter valid chain, it leaves a _broken_ one, because the successor's `prev_hash` names the row that
is gone. The re-seal wrote its own event -- `audit chain re-hashed 519 records on
2026-10-06T03:18:53+00:00` -- so the ledger itself records that its guarantee starts there.

The 2 `legacy_message` rows were left exactly as they are, as predicted: they record actions that
really happened, and the honest fix was the one that shipped in this phase -- name them, count them,
show them. `/metrics` now reads `unexpected_result_values: 2` and the panel
`· result values that are not statuses 2`.

One thing the repair revealed that no test had: a dashboard tab left open across it kept rendering
`BROKEN · 5 records · 520 of 525 verified` until reloaded, because Chrome throttles timers in a
background tab. The server was correct the whole time; the stale view was the artifact. Worth knowing
before anyone treats an unwatched panel as a monitor.

## 7. Numbers, re-measured

| Measure                                              | Before                     | After                                                                    |
| ---------------------------------------------------- | -------------------------- | ------------------------------------------------------------------------ |
| Tests                                                | 2408                       | **2419** (11 new)                                                        |
| Test modules                                         | 92                         | **93**                                                                   |
| Phase reports                                        | 67                         | **68**                                                                   |
| Ledger rows / chain coverage on the live host        | 495 of a hidden 500 window | **520 of 525, unverified 0, reason published**                           |
| Route objects / distinct paths / authored decorators | 118 / 111 / 118            | **unchanged** — `/metrics` gained fields, not a route                    |
| Tools / gated / planner rules / DB tables            | 148 / 32 / 52 / 32         | unchanged                                                                |
| Browser verification                                 | n/a                        | **DONE**, 1440×900, live DOM text above, 0 console messages, no overflow |

## 8. What this phase did not do

- Did not delete the forged rows (§6, needs your explicit yes).
- Did not touch the device rows, per your instruction: 13 pairing rows, 6 still `paired`, all dated
  2026-09-25, left exactly as found.
- Did not resolve 65-A (one slow OCR read still silences that backend for 45 s) — a product decision.
- Did not push anything: these commits are local.
- Did not establish the Vision stall trigger; Phase 65's `NOT ESTABLISHED` stands.
