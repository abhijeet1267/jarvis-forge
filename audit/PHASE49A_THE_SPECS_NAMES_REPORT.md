# Phase 49a — The spec's names, without losing what the old ones carried

Opened 2026-10-04 on a new input: `VANTAGE_JARVIS_PROJECT_SPEC.md`, attached by the user.
Asked what to do with it, the answer was **"adopt the spec's names too"** — so this phase is a
rename, and in this codebase a rename is a state problem before it is a spelling problem.

Lock screen (§6.1) was offered and **declined** by the user, so it is not built and
`lock my Mac` still returns an honest non-answer.

---

## 1. The spec was measured against the code, not assumed

Every factual claim in §4 and §39 was checked before anything was written.

| Spec claim | Measured |
|---|---|
| 134 tools | **134** ✓ |
| 32 SQLite tables | **32** ✓ |
| `RulePlanner` with 47+ intent handlers | 47 ✓ (48 after this phase) |
| Tri-state verification, `PermissionEngine` sole authority, hash-chained audit | all present ✓ |
| 108 routes | **112 route objects / 106 distinct paths** — the spec repeats the stale figure this README carried until Phase 46 corrected it |
| §39 items 2–8 ("build these seven tools") | **all seven already exist**, under `open_app`, `battery_status`, `laptop_snapshot`, `list_apps`, `open_project_folder`, `get_time`, `open_url` |
| §39 item 12, `POST /api/v1/command` | absent — the route is `/command` (not built in this phase; see 49b) |
| §10–§13 `AIProvider` / `OpenAIProvider` / `GeminiProvider` | absent — one OpenAI-compatible `ModelRouter` exists |
| §37's model IDs (`gpt-6.1-sol`, `gpt-realtime-2.1`, `gemini-3.8-live`) | **not verified by me** — I did not check them against a provider catalogue, and the spec's own rule (configuration owns model selection) makes repeating them as fact the only real risk |

The spec's §6.1 Basic command set was then run through the real planner as its own acceptance
check — 32 of the spec's utterances, **14 fail**. That list is §6 of this report and it is the
argument for Phase 49b.

## 2. Why a rename is a state problem here

Five places store a tool name:

```
permissions.tool    a standing revocation, looked up with  WHERE subject=? AND tool=?
confirmations.tool  a pending approval, matched when the user answers
action_logs.tool    the history a person reads back
task_steps.name     a step of a scheduled job that may not have run yet
scheduled_jobs.name the job itself
```

Renaming by substitution would therefore have **silently deleted a revocation**: a user who
blocked `open_app` finds `open_application` allowed, with no error anywhere in the system. That
is a privilege escalation wearing a refactor, and it is the one thing this project's rules say a
change may never do — "never weaken a permission gate to make tests pass", extended to "never
accidentally weaken one to make a rename pass".

So the rename is an alias. `ToolSpec.aliases` (a tuple, because a frozen field holding a list can
still be appended to in place — the Phase 26 lesson), the registry resolves through it, and every
stored-name lookup widens to match both spellings:

```python
def spellings(self, name: str) -> List[str]:
    """Every name that refers to this capability, canonical first."""
```

| canonical (spec) | alias (what shipped for 48 phases) |
|---|---|
| `open_application` | `open_app` |
| `get_battery_status` | `battery_status` |
| `get_machine_snapshot` | `laptop_snapshot` |
| `get_running_applications` | `list_apps` |

`open_folder` was deliberately **not** aliased to `open_project_folder`: the spec means "open
Downloads", the existing tool means "open this workspace in the editor". Two capabilities that
share a word are not two spellings of one thing, and aliasing them would have made an
over-broad guess look like a rename.

Aliases are not tools: `/tools` still returns **134** rows, `registry.names()` lists canonical
names only, and the risk spread is unchanged (71 read · 35 low · 13 external · 12 destructive ·
3 physical, 28 gated) — a test pins each of those, because the easy way to "support both names"
is to register the capability twice.

## 3. What the rename test found that was not about names

The safety test was written to prove the alias widening worked. Two of its neighbours failed for
reasons that have nothing to do with renaming:

**A device-less revocation revoked nothing.** `revoke(tool)` writes `device = NULL`; the lookup
required that column to equal the *action's* device. Measured before the fix:

```
revoke("open_app")                          -> row written, device NULL
evaluate(open_application @ laptop_main)    -> EXECUTE, reason "low-risk action"
revoke("open_app", "laptop_main")           -> BLOCK   (the only form that worked)
```

The natural thing to write is the form that does nothing.

**And it is a documented decision, not an oversight** — `test_phase10_revocation.py` said so in
plain text: *"Turning a NULL row into a wildcard would widen the reach of a standing grant to
every device, so it is a scope decision, not a tie fix, and is left to be made deliberately."*

So the fix is asymmetric, and the asymmetry is the whole argument: a NULL **grant** stays inert
(its own test still passes, unchanged), because promoting it would hand every device an authority
granted against no device at all. A NULL **denial** binds everywhere, because a denial can only
remove authority. Widening one direction is escalation; widening the other is a lock that finally
locks.

Honest limit, recorded in the code comment as well as here: **no product surface calls
`PolicyStore.revoke` today.** There is no route, tool or dashboard control that reaches it — only
*device* revocation is wired. So this is a trap closed ahead of its first caller, not a live bug
the user was suffering. My first draft of that comment claimed the user "clicked revoke and it
kept executing"; measured, that click does not exist yet, and the comment was corrected.

## 4. The renamed tool nobody could ask for

`get_machine_snapshot` — reachable by no sentence at all, measured:

```
'how is my laptop'  'laptop status'  'machine snapshot'  'snapshot the laptop'   -> []
'give me a system status'  'system status'   -> ['system_status']
```

The registry has two overlapping status tools (`system_status`: load/CPU/memory/disk;
`get_machine_snapshot`: CPU/memory/disk/battery/uptime) and only one of them was wired to
language. Renaming a tool does not make it reachable — that was Phase 28's defect class and it
survived 21 phases inside a tool that had been renamed once already in this session.

A `_machine_snapshot` route now answers the machine-condition phrasings ("how is my mac",
"machine status", "computer status", "laptop condition" …) and plain "system status" stays with
`system_status`. The overlap itself is left as open item 49b-4: two tools, one concept, and the
spec names only one.

## 5. `/tools` was hiding the mapping

The route builds its own dict field by field, so `aliases` did not appear in the payload —
`aliases: None` measured over live HTTP. A reader comparing an audit row that says `open_app`
against today's registry had no way to know they are the same capability, which is precisely the
confusion the alias exists to preserve. Added to the payload; the test now asserts on the
response rather than the object, and it also pins the 134 count from the client's side.

(That test initially failed for a reason worth keeping: a `TestClient` built without
`base_url="http://127.0.0.1:…"` gets refused by the host gate, so it read a `{"detail": …}` dict
and iterated strings. The gate working correctly looks exactly like a broken assertion.)

## 6. What the spec still asks for and this phase did not build

The same 32 utterances, so the gap is measured rather than estimated:

| utterance | today | needed for the spec |
|---|---|---|
| `Open Downloads`, `open desktop`, `show downloads`, `open my home folder` | not_understood | an `open_folder` tool over standard user folders, with verification |
| `open Contacts`, `open System Settings` | not_understood | the app names in the launch registry |
| `open Mail` | **declined** | the app-vs-email collision: "Mail" is being read as the email agent |
| `open Calendar` | routes to **`open_url`** — opens the Google Calendar *website* | app-vs-site disambiguation, the Phase 41 class |
| `is Safari open?` | not_understood | an app-status question route |
| `search Google for FastAPI` | not_understood | `open_search_url` (§6.1 lists it as EXTERNAL) |
| `what is my charge`, `time now`, `machine status`(→now fixed), `how is my mac`(→fixed) | mixed | paraphrase coverage |
| `POST /api/v1/command` with `{request_id, intent, status, verification{state,observed,notes}}` | absent | a versioned surface over the same pipeline |
| §29 Test 5: unknown app → "I could not find an installed application named FooBar123." | `not_understood` | an honest message naming the app it looked for |

Two of those are the interesting ones for the next phase: `open Mail` being **declined** and
`open Calendar` opening a **website** are not missing capabilities — they are wrong routes, the
same family as Phase 41's `open github.com` → `read_file`. A missing feature says "not yet";
those two say something false with confidence.

## 7. Numbers, re-measured

| | before | after | how |
|---|---|---|---|
| Test suite | 1915 passed · 0 failed · 0 skipped | **1939 passed · 0 failed · 1 skipped** (1940 collected, 76.4 s) | `JARVIS_HOME=$(mktemp -d) .venv/bin/python -m pytest -q -rs` |
| The skip | — | `test_research.py:423` — "arXiv could not be reached: The read operation timed out" | `-rs`, named rather than smoothed |
| Phase 49a tests | 0 | **25** | `tests/test_phase49_the_specs_names.py` |
| Test modules | 75 | **76** | `ls tests/test_*.py | wc -l` |
| Phase reports | 50 | **51** (this file) | `ls audit/*.md | wc -l` |
| Lines | 63.5k | **63,805** — 39,082 source · 24,723 tests | `wc -l` |
| Tools / risk spread / gated | 134 · 71/35/13/12/3 · 28 | **unchanged** | registry, asserted in tests |
| Planner routes | 47 | **48** | `len(JarvisCore().planner._rules)` |
| Test files touched by the rename | — | 18 (assertions follow the canonical name) + 1 deliberate behaviour change (`test_phase10_revocation`) | `git status` |

Live, on an isolated instance (fresh home, port 8803 — nothing written to the user's data):

```
GET  /tools            -> 134 rows; open_application / get_battery_status /
                           get_machine_snapshot / get_running_applications present;
                           no old name present as a row; aliases: ["open_app"]
POST /command {"text":"what is my battery"}
                       -> "Your laptop battery is at 100% (fully charged)."
                          actions: [('get_battery_status', 'success')]
```

## 8. Open items

* **49a-A — the alias widening is only as good as the registry being loaded.**
  `PolicyStore` asks `self.tools.spellings(tool)`. A policy row written for a tool that has
  since been *removed* (not renamed) has no spec to widen from and matches only its own
  spelling. Nothing today detects a stored name that resolves nowhere.
* **49a-B — `confirmations.tool` and `action_logs.tool` were not widened.** A confirmation
  created before the rename stores the old name; the resolve path matches on it. Untouched
  here because no live row crosses the rename on this machine — the pending queue is empty —
  but a deployment that upgrades in place with a queued approval would hit it.
* **49a-C — no product surface calls `PolicyStore.revoke` at all** (measured). The permission
  engine can be revoked from Python and from tests; a person cannot revoke anything. The
  spec's §33 "one permission policy" is implemented as an engine with one door and no handle.
* **49a-D — the spec's own §6.1 Basic command set is 14 utterances short**, listed in §6 above,
  with two wrong routes (`open Mail` declined, `open Calendar` → website) that are worse than
  the gaps.
* **49a-E — `system_status` and `get_machine_snapshot` overlap** and the spec names only the
  latter. Left alone: merging them would break a stored name for no user-visible gain.
* **49a-F — `/api/v1/*` (§22) is not built.** Deliberately deferred with the rest of the
  surface work rather than added as a second entry point, which is the thing §5 of the spec
  forbids: one pipeline, not two that can drift.

## 9. Decision recorded for the next reader

The spec's names are now canonical and the old names are permanent aliases, because the user
chose "adopt the spec's names too" over keeping the repo's. What was *not* done, in the same
choice: renaming `open_project_folder` to `open_folder`, moving to the spec's §23 directory
layout, or adding a second command pipeline at `/api/v1` — each of those is a different
capability or a different authority, not a different spelling of one.
