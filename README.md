<img src="assets/hero.svg" alt="JARVIS Forge — a permissioned, local-first agent OS" width="100%"/>

# JARVIS Forge

A permissioned, **local-first** personal agent OS. You type or say a sentence; it is
planned by deterministic rules, gated by a permission engine, held for confirmation
when the risk says so, executed against your real machine, **verified by observing the
world afterwards**, and written to a tamper-evident audit chain. It runs with no API
key, no network and no vendor — an LLM is optional and off by default.

What this repository is mostly about is not features. It is about the difference
between *"the handler returned"* and *"the thing happened"* — and the thirty phases of
measurement spent closing that gap, in the ledger and on the screen.

[![ci](https://img.shields.io/github/actions/workflow/status/abhijeet1267/jarvis-forge/ci.yml?label=tests%20%C2%B7%20macos)](https://github.com/abhijeet1267/jarvis-forge/actions/workflows/ci.yml)
[![suite](https://img.shields.io/badge/tests-1621%20passed%20%C2%B7%200%20failed-16a34a)](tests)
[![tools](https://img.shields.io/badge/tools-127%20across%2015%20agents-2f6df6)](jarvis/tools)
[![offline](https://img.shields.io/badge/LLM-off%20by%20default%20%C2%B7%20runs%20fully%20offline-0f172a)](jarvis/config.py)
[![python](https://img.shields.io/badge/python-3.11%20%C2%B7%203.12%20(CI)-6b7688)](.github/workflows/ci.yml)

| | |
|---|---|
| **Runtime** | Python 3.11+ · FastAPI · SQLite (32 tables) · stdlib-first |
| **Planning** | `RulePlanner` — 40 deterministic routes, no model required |
| **Authority** | `PermissionEngine` · 5 risk tiers · 27 confirmation-gated tools |
| **Proof** | tri-state `Verification` (success / failed / **unknown**) on every action |
| **Ledger** | `action_logs` hash-chained + `audit.jsonl` append-only, `verify_chain()` |
| **Edge** | token file `0600` · constant-time compare · same-origin · Host allowlist · 600/min |
| **Surface** | web dashboard · voice (macOS `say`/STT) · watch queue · phone pairing |
| **Size** | ~55k lines · 34.6k source · 18.9k tests · 56 test modules |

---

## Verified status

Every figure below is produced by a command in the right-hand column. Nothing here is
typed in by hand: the charts in `assets/` are **generated** from `benchmarks/*.json`,
and the diagrams read their counts from the live registry.

| Claim | Value | Reproduce |
|---|---|---|
| Test suite | **1621 passed · 0 failed · 0 xfailed** (1621 collected; the network-gated arXiv test skips when arXiv is refusing requests, and passed in the run above) | `python -m pytest -q` |
| Suite growth, Phase 9 → 32 | 875 passing / **126 failing** → 1621 passing / 0 failing / **0 xfailed** at the Phase 32 close | `cat benchmarks/suite_history.json` |
| Tools registered | **127** | `python -c "from jarvis.core.orchestrator import JarvisCore as J; print(len(J().tools.names()))"` |
| Confirmation-gated | **27** of 127 | `python assets/build_diagrams.py` (prints the live counts) |
| Risk tiers | read 68 · low 32 · external 12 · high 12 · physical 3 | same |
| API routes | **104** | `python -c "s=open('jarvis/api/app.py').read(); print(sum(s.count(f'@app.{v}(\"') for v in ('get','post','put','patch','delete')))"` |
| Acceptance commands measured | 17 of 18, live, over HTTP | `python benchmarks/acceptance_run.py` |
| Plan's safety matrix (§45) | **6 of 6** hold — the sixth held since Phase 31 | `python benchmarks/safety_run.py` |
| Launched-server sweep | 8 read endpoints **200**, unauthenticated **401**, commands answered over the network | `python run.py serve`, then the sweep in `audit/PHASE21_SPEED_RESILIENCE_REPORT.md` §1 |
| Packaged desktop app | builds, ad-hoc signs, **launches**, and its server answers `/health`; self-test reports **7/9** (the two failures are macOS permissions, not code) | `python build_app.py && JARVIS_SELFTEST=1 "dist/Jarvis Forge.app/Contents/MacOS/JarvisForge"` |
| Audit chain integrity | `verify_chain() → ok=True` over **2 recorded actions** (an empty chain verifies trivially, so run two commands first) | `JARVIS_HOME=$(mktemp -d) python -c "import asyncio;from jarvis.core.models import CommandRequest as R;from jarvis.core.orchestrator import JarvisCore as J;c=J();[asyncio.run(c.handle(R(text=t,source='probe'))) for t in ('Jarvis, what time is it.','Jarvis, what is my laptop battery.')];print(c.audit.verify_chain())"` |
| Regenerated figures parse as SVG | 7/7 | `python assets/build_charts.py && python assets/build_diagrams.py` |
| CI on the pushed commit | `suite (3.11)` and `suite (3.12)` both **success** | `gh run list --workflow ci.yml --limit 1` |

> On the author's own machine `~/.jarvis-forge` still reports `verify_chain()`
> `ok=False` with **5 breaks** — residue from the pre-verification records and the
> tamper tests, documented as addenda C-2/C-2a in `audit/PHASE9_FINAL_REPORT.md` and
> `audit/PHASE10_TRUTH_AUTHORITY_REPORT.md`. A fresh `JARVIS_HOME` verifies clean.

**Not verified, and not pretending otherwise:**

| Item | Status | Why |
|---|---|---|
| Physical robot / AMR / arm motion | **HARDWARE UNAVAILABLE** | no bridge reachable; `emergency_stop` says so in its own reply |
| Car telemetry | **NOT CONFIGURED** | no OBD-II adapter or vehicle API; refuses rather than invents a number |
| Watch delivery | **UNPAIRED** | prompts queue locally; a simulated companion *delivers*, never *answers* |
| Phone | **NO DEVICE PAIRED** | refuses the question instead of reporting a battery level |
| Email / calendar accounts | **LOCAL STORES ONLY** | `local_maildir` / `local_calendar`; no credentials configured |
| Voice capture | **ENVIRONMENT-BLOCKED** | macOS denied the microphone to an unbundled process |

| Live browser-driven UI test | **NOT PERFORMED** | browser interaction is blocked by the agent runtime's policy; the page's JS is instead executed from the shipped file by Node |

---

## Contents

- [How a sentence becomes a verified act](#how-a-sentence-becomes-a-verified-act)
- [The state vocabulary](#the-state-vocabulary)
- [Benchmarks](#benchmarks)
- [Hardware truth](#hardware-truth)
- [What the system says when something fails](#what-the-system-says-when-something-fails)
- [The thirty-two phases](#the-thirty-two-phases)
- [The plan's safety matrix, run](#the-plans-safety-matrix-run)
- [Smooth and fast](#smooth-and-fast)
- [Security model](#security-model)
- [Quickstart](#quickstart)
- [Repository layout](#repository-layout)
- [Downloads](#downloads)
- [Known open items](#known-open-items)
- [License](#license)

---

## How a sentence becomes a verified act

<img src="assets/architecture.svg" alt="Layered architecture: surfaces, edge auth, orchestrator, agents, tools, persistence" width="100%"/>

One funnel, not a side door. Every action — typed, spoken, scheduled, or arriving from
a watch — crosses the same `PermissionEngine`. A **scheduled** job re-evaluates on every
run, records the risk ceiling it was created with, and refuses to exceed it: a schedule
cannot be a way to gain authority later. When a verdict is *confirm*, the runner parks a
pending confirmation and stops. It never approves on your behalf.

## The state vocabulary

<img src="assets/truth-states.svg" alt="Twelve dashboard states and the backend field each one is derived from" width="100%"/>

The rule this project is built on: **`VERIFIED` may only come from
`actions[].verification == "success"`** — never from an HTTP 200, never from `action.ok`
(which means only *the handler answered*), never from reply wording, and never from a
field a client asserts about itself. Each of those four lookalikes has its own test.

When the evidence does not distinguish a state, the answer is `UNKNOWN` — not a guess,
and not a quiet success.

## Benchmarks

### Suite growth, Phase 9 → Phase 32

<img src="assets/chart-suite-growth.svg" alt="Test suite growth from 875 passing with 126 failing, to 1621 passing with none failing at the close of Phase 32" width="100%"/>

Phase 9 inherited **126 failing tests**. The cause was a single import line in
`jarvis/tools/base.py` that left `Verification` undefined, so every tool result raised
`NameError` on construction — one undecorated import, 126 red tests, and every offline
read auditable as a success.

### The plan's own acceptance list, measured

<img src="assets/chart-acceptance-states.svg" alt="Eighteen acceptance commands with their terminal state, audit token and latency" width="100%"/>

| Command | State the user sees | Audit token | ms |
|---|---|---|---|
| show me my laptop battery | VERIFIED | success | 14 |
| plan my day | VERIFIED | success | 5 |
| remember that this is my AMR project | VERIFIED | success | 5 |
| what did I work on yesterday | VERIFIED | success | 4 |
| check my car fuel | **UNAVAILABLE** | failure | 7 |
| turn on study mode | **UNAVAILABLE** | failure | 5 |
| transfer this file to my phone | **NOTHING EXECUTED** | no_action | 4 |
| design this system | **NOT RECOGNISED** | no_action | 4 |
| schedule two hours for project work tomorrow | **NEEDS DETAIL** | no_action | 3 |
| draft an email to my teammate | **FAILED** | failure | 5 |
| research this idea | VERIFIED (degraded) | success | 41,247 |
| find my latest project report | VERIFIED | success | 65 |

Median command: **5 ms**. The 17 measured commands sum to **41,389 ms** in this run,
but that single number is two different facts: the 16 commands that touch no network total
**142 ms** (median 5 ms), and `research this idea` alone is **41,247 ms** -- it waits on arXiv, which answered this week with a read timeout after 41 seconds rather than the 406 that usually comes back in ~1.2 s. The command still
ended VERIFIED-degraded, naming the refusal instead of inventing a paper, which is 15-F; what
this run adds is **29-B**, the measurement that no bounded read timeout exists on that path.
`find my latest project report` is 65 ms here because `$HOME` was sandboxed to a scratch tree, so the walk had nothing
to descend -- the search root is inside the row's recorded reply, so a sandbox run and a
real-home run (346-1,217 ms across five runs) stay tellable apart afterwards.

Note *"what did I work on yesterday"* reading **VERIFIED** with an empty result: since
Phase 20 it answers about the *day asked about* — `Nothing was recorded in memory on
2026-09-26` — and a read that genuinely returned nothing is a successful read. Before
then it answered with today's activity echo, undated, as a remembered fact.

### What the Phase 9 audit found

<img src="assets/chart-findings-severity.svg" alt="46 findings by severity, six of them P0" width="100%"/>

## Hardware truth

<img src="assets/chart-hardware-truth.svg" alt="Peripheral status table: nothing configured, nothing online, reported honestly" width="100%"/>

Nothing is attached to the machine these figures were taken on, and the product says so
in every case. **A simulated actuation is labelled simulated and is no longer audited as
a success** (Phase 18): the simulator accepted the command, nothing in the real world
moved, and the ledger now says exactly that. A simulated *car read* is still a successful
read — reading a labelled simulator is a real read — and that distinction is pinned by a
test so it stays a decision rather than an accident.

## The thirty-two phases

Each closed with a report in [`audit/`](audit), and each report's claims carry the
command that produced them.

| Phase | What was proven or repaired |
|---|---|
| **9 — Audit** | 15-part read-only sweep: baseline, test truth, `STATUS.md` drift (287 claims → 44 false, 52 stale), permission red team, honesty matrix, hardware matrix, 46 findings |
| **10 — Truth & authority** | 6 P0s: verification authority, master-token disclosure, emergency-stop overstatement, watch approval escape, memory-deletion over-claim, chain-verifier semantics |
| **11 — Real world** | first fully green suite; `write_file` with read-back verification |
| **12 — Command path** | typed sentence → `/command` → planner → permission → confirmation → real file → independent verification → audit |
| **13 — Dashboard truth** | pipeline state rendered from server fields; `VERIFIED` bound to verification alone |
| **14 — Workflows** | multi-step plans made reachable; orphaned `running` jobs fixed; per-step state shown |
| **15 — Acceptance** | the plan's 18 commands run live; **six false successes** found and corrected |
| **16 — Accountability** | route-level refusals audited (`no_action`, `clarification`); ledger `request_id` was holding session ids |
| **17 — Coherence** | a non-executing reply stopped claiming `decision=EXECUTE`; audit card colours decisions differently from failures |
| **18 — Simulation** | simulated actuations no longer audited as `success` |
| **19 — Refusals** | dropped clauses named in the reply *and* the ledger; `VERIFIED IN PART` |
| **20 — Dated recall** | "yesterday" became a date range instead of a search phrase; activity echoes labelled as echoes |
| **21 — Speed & resilience** | unbounded filesystem walk bounded (11.3 s → 0.35-0.86 s, worst case capped at 2.5 s and disclosed) and one refused research source no longer fails the command |
| **22 — Safety matrix** | the plan's §45 phrases run for real: ambiguous device words ask instead of actuating, offline control reports one honest state, a missing payload asks, an embedding outage stops masquerading as an empty index |
| **23 — Deletion completeness** | the three defects parked as `xfail` in Phase 10 were fixed at their sources, so a forget means the same thing on both surfaces, checking a deletion no longer re-writes it, and deleted text leaves the process cache too |
| **24 — Refusal vs detail** | a question stopped being labelled a refusal: `clarification` and `refusal` are now backend fields the page reads, `NEEDS DETAIL` became reachable for real, and a clarification no longer advertises a phantom approval |
| **25 — Desktop truth** | the bundle that "exits 1" was a stale claim: it builds, launches and serves; the self-test now runs *on a broken install* (P0-D3), and the installer stopped promising a permission prompt the app never shows |
| **26 — Recovery trust** | re-running is granted by declaration, not by error text (45 tools → 0); `ToolSpec` is frozen, so the risk tier the gate reads cannot be rewritten under it; a retried job says "nothing ran" instead of `running`; and a failure that cannot be recorded is counted instead of vanishing |
| **27 — A retry that retries** | plan §31's `retry` was a record with no dispatch behind it. A step now stores its own parameters, and `POST /tasks/{id}/retry` re-issues the failed steps through the permission gate -- a gated step mints a fresh approval, a step whose parameters were never stored is refused rather than guessed at |
| **28 — Reading is not writing** | "read the readme" *created a document*; an artifact route now needs an authoring verb, and `read_file` -- registered since v0.1 and unreachable from any sentence -- finally answers one |
| **29 — A route fills its own action** | flowcharts and architecture diagrams had been dead since they were written: the route sent a title to a tool that also needs `steps` / `components`, so the gate answered the user with *"missing required parameter 'steps'"* -- even when the sentence named them. What a sentence states is now used; what it omits is asked for |
| **30 — A capability you have is a capability you can ask for** | 46 of 127 tools answered their own name with "I understood the words but not the goal" -- twice the gap the open item had recorded, because that item had been measured by grepping the planner instead of asking the router. A registry-derived route now reaches a tool whose name words a sentence contains, validates the action with the registry before emitting it, and asks in the tool's own description when an argument is missing; three routes that *advertised* capabilities yield to a sentence naming one; and the delete-capable tool that was tiered LOW-and-unconfirmed for as long as it was unreachable was retiered in the same commit |
| **31 — The device you named is the device that changes** | "turn on the heater" resolved to **Study AC** (the gateway filed heating under cooling) and `control_home_device` was LOW with no confirmation, so the request was approved for execution and only an offline gateway stopped a device changing. The synonym, the resolver and the tier were tightened in one commit, the permission engine grew a device guard beside its path guard, and the plan's §45 matrix has held **6 of 6** since |
| **32 — One vocabulary, one approval** | the router kept eleven device words where the gateway keeps twenty-six, so nine words the system understood were answered as nonsense; and the confirmation prompt re-derived its own line from the raw parameters while the dashboard rendered the stored preview. The pattern is now built from the gateway's own table (pinned by adding and removing a synonym at test time, both directions), and the approval names the thing: *Will do: set Kitchen light (kitchen.light) to 40%* |

### Four kinds of "I ran nothing"

Phase 19 found that a refusal and a clarifying question shared one label, and left it open
because separating them "would mean guessing from punctuation". Phase 24 closed it without
guessing: the string being classified is the **route's own note**, written by the code that
produced it, and a route whose note is a statement sets `clarification=False` explicitly
instead of relying on shape. Measured over HTTP on a launched server:

| Sentence | `plan.reply_kind` | State the user sees | Ledger `result` |
|---|---|---|---|
| send to Arun | `clarification` | **NEEDS DETAIL** | `no_action` |
| turn on the light | `clarification` | **NEEDS DETAIL** | `no_action` |
| delete everything | `refusal` | **BLOCKED** | `blocked` |
| design this system | `not_understood` | **NOT RECOGNISED** | `no_action` |
| what is my phone battery | `declined` | **NOTHING EXECUTED** | `no_action` |

Two properties hold this honest, both tested: a *statement of what is missing* cannot be
promoted into a question the user could answer, and `needs_confirmation` is no longer set on
a clarification — it used to advertise a pending approval for a command with nothing to
approve, which the page would have offered to confirm.

## What the system says when something fails

Phase 9's recovery audit left four findings that every later phase carried forward without
re-measuring. Measured on this tree, all four were real and two were worse than their
one-line summaries:

| Finding | Measured before | Now |
|---|---|---|
| **R-2** — what grants an automatic retry | the guard asked "is it destructive?" and stopped at risk 3, so **45** tools below it were re-executed on a regex over the error text: `move_file`, `copy_file`, `write_file`, `create_event`, `schedule_task`, `send_to_device`, `control_home_device`, `remember_fact`, `compress_path`, `vibrate_watch`… The string that triggers it most reliably, `command timed out after 30.0s`, is the case where the effect is *unknown* | **0**. A retry needs the read tier or a name in `IDEMPOTENT_TOOLS`; error text only ever *stops* retrying early |
| **RT-3** — who owns a risk tier | `ToolSpec` was mutable and `evaluate()` re-reads the registry's same object: setting `delete_file.risk = READ` moved the gate from `CONFIRM (level 3 (destructive) action requires confirmation)` to `EXECUTE (low-risk action)`, made the same action automatically retryable, and rewrote the planner's own catalogue line to `risk=read confirm=False` | frozen, with tuples where a gate reads membership, so `tags.remove("safety")` is refused too. The verdict is unchanged after the attempt |
| **R-11** — a lifecycle verb's promises | `POST /tasks/{id}/retry` returned a job set to `running`, its failed steps reset to `pending`, their `detail` **erased**, and 0 executions; because the briefing counts `running` jobs, a human was told **"1 job(s) in progress."** | the step keeps `failed` and its error text, the job keeps its real state, every verb answers with a `LifecycleReport` and an `executed` count that means what it says, and the briefing says **"1 job(s) failed."** Phase 27 then gave the verb something true to count: `retry` now re-issues the failed steps, so `executed` is how many actually ran |
| **R-13** — a failure ledger that lies by silence | `record_failure` swallowed every exception at `log.info` (measured: 1 failure, 0 rows, normal reply) while `failure_stats()` / `recent_failures()` raised straight through `/metrics`, `/failures` and the briefing | lost writes are counted and surfaced (`unwritten`, `ledger_error`); `/metrics` degrades with the reason, `/failures` answers 503, and the briefing prints `failure ledger unavailable: …` rather than "No failures recorded." |

Two halves of the same audit were assessed rather than assumed fixed. **R-12** (the
confirmed-action path never touched recovery) is closed at its truth half: that path still
does not retry — one approval buys one attempt — but a gated failure is now *recorded*,
which measured as `failures: 1 → 1` before and `1 → 2` after, live over HTTP:

```
$ curl -X POST -H "$TOKEN" localhost:8899/confirm/CONF-b5cdff66…   # a delete with nothing to delete
  That failed: Nothing to delete at …
$ curl -H "$TOKEN" localhost:8899/failures?limit=1
  delete_file: recovered=0 attempts=0 strategy='none'
    note: … — not retried: it ran under a confirmation, and one approval authorises one attempt
```

**R-14** (a successful fallback silently becoming the planned tool's result) is fixed where
it was false in the record — the ledger row for `search_files` now carries
`answered_by: list_directory` — and named as a remaining wording gap in the memory sentence
under [Known open items](#known-open-items).

### Asking to read something used to make JARVIS write something (Phase 28)

Not a mislabelled state this time -- an unrequested write, decided by a substring. The
design route matched the *noun* (`document\w*|readme|guide`) and never looked at the verb,
so the sentence router authored files for requests to read them. Measured on the live tree,
each line below left a new file on disk:

```
>>> "read the readme"     intent=create_documentation decision=EXECUTE
    reply='Created documentation: read the readme (markdown) — read-the-readme-2340c886.md'
>>> "show me the readme"  intent=create_documentation decision=EXECUTE
>>> "open README.md"      intent=create_documentation decision=EXECUTE
```

The word "read" is inside "readme", and no rule could ask to *read* a file at all:
`read_file` has been registered since v0.1, is risk tier READ, and appears nowhere in the
planner -- one of 29 tools no typed sentence could reach. Two changes, both measured:

| Sentence | Before | After |
|---|---|---|
| `read the readme` | wrote `read-the-readme-<id>.md`, "Created documentation" | `read_file`, reply carries the file's text, workspace unchanged |
| `show me the README.md` / `print README.md` / `what does the file README.md say` | authored an artifact | read it |
| `write documentation for the parser` | authored | **still** authors -- the guard is "which verb leads the sentence", not a blocklist |
| `read the file absent.txt` | would have written a document | `File not found: …`, nothing created |
| `read the file /etc/hosts` | — | passed to the engine's guard, which blocks it: `/private is a protected system location` |
| `read ./sub/notes.txt` | — | anchored in the file workspace, not the process's cwd |

An artifact route now has to be *asked* to author (`_authoring_request` compares the first
verb of each class), reads never reach an OS-protected location, and the reply excerpt is
capped at 1,200 characters with a disclosure, because that reply is also what gets
remembered. Building this also turned up a hole in the router's own reporting: a rule that
raised on every call was logged at `info` and returned as "no rule matched", so a broken
append route was indistinguishable from a sentence JARVIS doesn't understand -- it now logs
the rule's name at `warning`, with a test that pins it.

### Forty-six capabilities existed and could not be asked for (Phase 30)

The open item said 28 tools were unreachable, and the number had been produced by grepping
`planner.py` for each tool's name. Asking the router instead of the source file found
**46**: tools whose own name, typed out, was answered *"I understood the words but not the
goal"* — `read_file`, `get_time`, `open_app`, `close_app`, `copy_file`, `list_events` among
them, all of which have routes that are keyed on some other phrasing. Measured live, before
the fix:

```
>>> "cancel event 12"
    intent=calendar  kind=declined  tools=[]
    reply='I can read your schedule, find free time, and create or cancel events. …'
>>> "get the device capabilities of phone_main"
    reply='I can open and close applications, … Registered tools: {127 names}.'
>>> "list the workspace"
    decision=BLOCK  reply="I cannot do that: missing required parameter 'workspace_id'."
```

The first is a route advertising a capability the router did not have; the second is the
word "capabilities" being a help-rule trigger, so naming one capability earned the whole
catalogue; the third is 29-B alive in the shipped tree, reaching a user through a route that
built its parameters in a variable — the exact blind spot the static sweep had admitted.

A capability that exists is now reachable by naming it: a registry-derived route runs last,
matches a tool only when the sentence contains *all* of its name words (at least two, so one
keyword never plans anything), asks `ToolRegistry.validate_params` before it emits, and when
the argument is missing phrases the question out of the tool's own `description` rather than
its field name — *"I can read one message in full — your sentence does not say which one."*
Three advertising routes yield to a sentence that names a capability; refusing routes do not,
because quietly preferring a keyword over a refusal would be a way of talking past the
permission engine. Registering a tool is now also registering a way to ask for it.

Reaching a tool changes nothing about whether it runs: `"cancel event 12"` and
`"sweep expired memories"` both come back `CONFIRM` with no action executed, and the
second one is why this phase retiered a tool in its own commit — `sweep_expired_memories`
permanently deletes, was tiered `LOW` with no confirmation, and had been so for exactly as
long as it had been unreachable. The two facts were holding each other up.

## The plan's safety matrix, run

Section 45 of the working plan names six phrases this system must refuse, clarify or
confirm on, and six disconnections it must survive honestly. They are now a
reproducible artifact rather than a claim, with the expected outcome written down
before the run:

```bash
python benchmarks/safety_run.py      # HOME is sandboxed to a scratch tree; nothing is paired
```

| Phrase | The plan asks for | What the system does | Holds |
|---|---|---|---|
| delete everything | reject | `BLOCKED` — "I will not do that automatically: blanket delete request" | yes |
| install unknown.exe | reject | `BLOCKED` — untrusted executable | yes |
| unlock door | reject | `BLOCKED` — physical security action | yes |
| run this command as root | reject | `BLOCKED` — privilege escalation | yes |
| send to Arun | clarify | `NEEDS DETAIL` — "Send what to Arun, and by which medium?" | yes |
| turn on heater | reject / clarify / confirm | `BLOCKED` — "There is no home device matching 'heater'. The gateway has: Study desk lamp, Study standing desk, Study AC, …" | yes |

The last row printed `FAIL` for nine phases and was 22-G. It closed in Phase 31, and
what closed it was not the benchmark: the gateway's vocabulary filed `heater` and `hvac`
under the kind `ac`, so "turn on the heater" resolved to **Study AC** — an appliance
nobody named — and `control_home_device` was tiered LOW with no confirmation, which means
the request was approved for execution and only the gateway being offline stopped a device
changing. Three layers were tightened in one commit (the synonym, an actuation resolver
that requires every word the user typed to describe the device, and `requires_confirmation
= True`), and the permission engine grew a device guard beside its path guard so no
approval is ever *requested* against a device it cannot identify. See
`audit/PHASE31_THE_DEVICE_YOU_NAME_REPORT.md`.

Before Phase 22 this matrix looked greener than it was. "turn on the light" — a phrase the
plan does not even list — picked *Kitchen light* out of four tied candidates and executed
against it, and the answer never said a choice had been made. Ambiguous device words now
ask, naming every candidate:

> Which light did you mean -- Living room ceiling light, Kitchen light, Bedroom ceiling
> light, Hallway light? Name the room and I will switch it.

## Smooth and fast

Measured, because "feels faster" is not evidence:

| Path | Before | After |
|---|---|---|
| `search_files`, query that matches nothing, rooted at `~` | **6,955 ms** | **346–1,217 ms** |
| `search_files`, the acceptance command | 11,299 ms | 1,217 ms (346 ms on a quieter run) |
| All 17 measured acceptance commands | 22,573 ms | **41,389 ms** as measured, **142 ms** of it once `research` is separated out (see the note above) |
| Any API read (`/tools`, `/tasks`, `/audit`, `/memory`) | 1-2 ms | unchanged |
| `GET /health`, launched server, warm | 1.5-8 ms (first call in a process: 110 ms) | unchanged — nothing expensive runs per request |
| `JarvisCore()` construction | 29 ms | unchanged |

The ranges are real, not hedging: seven separate runs of the same command measured
346, 451, 494, 534, 683, 855 and 1,217 ms, because the fallback walk reads actual disk. What the change
removed is the *unbounded* case — the walk now has a 2.5 s ceiling it reports when it
hits, instead of a worst case that was whatever `~/Library` felt like that day.

Two changes, both bounded and both honest about it:

* **The walk had no ceiling.** It stopped early only when it *found* things, so an
  unmatched query walked all of `~/Library` — macOS' largest tree, and nowhere a user
  means. Now it prunes system/cache directories, caps entries and time, and **says so**
  when it stopped early: a partial search reports `truncated` and the number of entries
  it looked at, because "nothing matched" and "I stopped looking" are different claims.
  Pruned names are returned too, so the boundary is inspectable rather than silent.
* **One dead source used to kill the command.** `research` failed outright when arXiv
  refused. It now retries once, falls back to the local corpus, and names the refusal in
  the answer and in `output.source_errors` — while still never inventing a paper. The
  refusal itself is unresolved and documented: arXiv answers curl 200 and every Python
  client 406 from the same machine at the same moment. It is not the User-Agent, not
  HTTP/1.1 vs HTTP/2, and not the plain-HTTP redirect — three diagnoses I made and
  discarded before measuring it properly.

## Security model

- **Token** — generated at `$JARVIS_HOME/api_token`, mode `0600`; compared in constant
  time. The master token is **never** sent to the browser: `/dashboard` carries a
  single-use 256-bit bootstrap that buys only a short-lived, revocable session.
- **Origin & host** — a missing `Origin` is refused like a foreign one; non-loopback
  `Host` is rejected (anti DNS-rebinding).
- **Rate limit** — token bucket, 600/min, burst 120.
- **Confirmations** — `CONF-` + 128-bit entropy, 300 s expiry, and `pre_execute_check()`
  re-runs the refusal gates at approval time, because conditions can change while a
  prompt sits unanswered. One approval buys exactly one action.
- **Deletion** — `forget_memory` reports which ids and surfaces it removed and which it
  could not, instead of a bare "Forgotten". Since Phase 23 `POST /memory/{id}/forget`
  delegates to that same code rather than answering `{'deleted': true}` on its own, and
  a deletion evicts the text from the embedding cache instead of leaving it resident.
- **Redaction** — credentials are redacted before anything reaches the audit log,
  application log, or a model prompt.
- **Refusals are decisions** — a command JARVIS declines still writes a hash-chained
  record. `no execution` never meant `no audit event`.

## Quickstart

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                      # defaults are safe; everything is blank

python run.py                             # API on 127.0.0.1:8765
open http://127.0.0.1:8765/dashboard      # the page fetches its own session token

# Authenticated API, from a shell:
TOKEN=$(cat ~/.jarvis-forge/api_token)
curl -s -H "X-JARVIS-Token: $TOKEN" -H 'Content-Type: application/json' \
     -d '{"text":"what is my battery"}' http://127.0.0.1:8765/command

# Prove it to yourself:
python -m pytest -q                       # 1621 passed, 0 failed (one research test skips if arXiv is refusing)
python benchmarks/acceptance_run.py       # re-measures the README's numbers
python assets/build_charts.py && python assets/build_diagrams.py
```

No LLM key is needed — the rule planner is the default and the guaranteed fallback.
Set `JARVIS_LLM_*` only if you want the optional model path.

## Repository layout

```
jarvis/
  core/        orchestrator, RulePlanner, models, recovery, tasks, briefing
  security/    permission engine, audit chain, redaction, privacy, path guard
  api/         FastAPI app: 104 routes, auth, origin gate, rate limit
  agents/      15 agents — laptop, phone, home, robotics, car, watch, email, …
  tools/       127 tools across 5 risk tiers, each declaring its own spec
  memory/      hybrid retrieval, lifecycle, forgetting
  scheduler/   recurring jobs, runner with per-run re-authorization
  car/ home/ robotics/ watch/ vision/ research/ design/ coding/
  integrations/ IMAP/SMTP and CalDAV providers with local fallbacks
tests/         56 modules · 1621 collected · includes the dashboard's own JS, run by Node
dashboard/     single-file control panel; state rendered from server fields
audit/         34 phase reports + 4 CSV matrices — the evidence behind this README
benchmarks/    acceptance_run.py and the JSON the charts are drawn from
assets/        generated figures + the two generators that draw them
app/           PyInstaller spec and bundle sources (see Downloads — currently broken)
```

## Downloads

**Source only** — and the reason changed in Phase 25. The bundle is no longer broken
(it builds, ad-hoc signs, launches, and its server answers), so "it doesn't work" is no
longer the argument. What still stops a published binary is different and holds: an
ad-hoc signature is enough for *this* Mac, not for a build handed to strangers — that
needs an Apple Developer identity, notarization, and a TCC-stable bundle id, which is a
distribution decision rather than a packaging step I can quietly take.

- [Download the source (`.zip`)](https://github.com/abhijeet1267/jarvis-forge/archive/refs/heads/main.zip)
- [Download the source (`.tar.gz`)](https://github.com/abhijeet1267/jarvis-forge/archive/refs/heads/main.tar.gz)
- Build it yourself: `python build_app.py` (into `./dist`), or `./install_app.sh` to move
  it into `/Applications` — it keeps any previous install as `Jarvis Forge.app.previous`
  rather than deleting it. Then `JARVIS_SELFTEST=1` on the built binary prints what the
  app can actually reach, including the microphone and speech-recognition state.

## Known open items

Stated plainly, because the point of this repository is that a gap you can name is
better than a gap you have to discover:

- **CI runs on macOS, and that is load-bearing**: the first runner attempt failed 10
  tests because a GitHub macOS runner is a VM with no battery and no ffmpeg. The
  battery-bound tests are now capability-gated; the voice tests install ffmpeg so their
  premise holds instead of being skipped. Investigating that failure also found a real
  bug -- `created_at` is stored in UTC while "yesterday" is the user's local day, so
  dated recall resolved the wrong day for non-UTC users. Fixed, with a regression test.
- **A green test can still be a laptop-shaped test**: the Phase 21 suite passed locally and
  failed on the runner, because `search_files` asks Spotlight first and GitHub's macOS
  image indexes a temp file the instant it is written, while a laptop does not. So an
  assertion about the *walk* was being answered by the *index*. Fixed by pinning the code
  path under test and adding the matching precedence test -- reasoning in
  `audit/PHASE21_SPEED_RESILIENCE_REPORT.md` §8.
- **An unnamed robot still gets a default at the planner** (`jarvis/core/planner.py:1884`,
  `self._robot_ref(text) or "amr_main"`). The tool no longer infers one for itself — with
  two machines able to `home` or `stop`, `command_robot_motion(robot="")` now asks — but a
  *typed sentence* still lands on a chosen machine, with a confirmation prompt that names
  it. Turning that into a question is deliberately left open: for `stop`, a round trip is
  the wrong trade against a machine that is moving, and that is a judgement about safety
  semantics rather than about truthfulness (23-E).
- **The plan's safety matrix holds 6 of 6** (22-G, closed by Phase 31). `turn on heater`
  used to reach the single HVAC unit in the inventory and be *executed* without
  confirmation, because the gateway's vocabulary filed `heater` and `hvac` under the kind
  `ac` and `control_home_device` was LOW for every device kind. What closed it was a tier
  and a vocabulary decision, not a benchmark edit: `requires_confirmation=True`, the
  heating/cooling conflation removed, an actuation resolver that requires every word the
  user typed to describe the device, and a guard in the permission engine so an
  unidentifiable target is refused rather than put up for approval.
  **31-A** and **31-B** were the residue, and Phase 32 closed both. 31-A: the planner recognised
eleven device words of its own while the gateway kept a 26-word synonym table, so nine words
the system could act on — `power`, `outlet`, `bulb`, `ceiling`, `air`, `conditioner`, `cam`,
`motion`, `energy` — were answered *"I understood the words but not the goal"*. The router now
builds that pattern from the gateway's own vocabulary, which a test pins in both directions by
adding and removing a synonym at run time. 31-B: the confirmation prompt re-derived its own
line from the raw parameters while the dashboard rendered the stored preview — two renderings
of one consent question, and neither naming the device. One string feeds both now: *"Will do:
set Kitchen light (kitchen.light) to 40%"*.
- **A capability that exists must be reachable, and consent must name the thing** (Phases 30–32).
  Reasoned in `audit/PHASE30_CAPABILITIES_ARE_REACHABLE_REPORT.md` and
  `audit/PHASE32_ONE_VOCABULARY_ONE_APPROVAL.md`. What those two left open:
  **32-A** the set of appliance words a house *could* have but this one lacks is declared, not
  derived — a home that genuinely has a heater would need the word added in two places before
  the refusal stops firing. **32-B** a non-home confirmation still shows a parameter dump
  (`delete_file on laptop_main | path=… | permanent=False`); tools that own an identifier worth
  resolving — memory, research, workspaces — could each get the same `describe_*` treatment.
  **32-C** `dim` reaches the gateway as `action="set"`, so the approval reads "set Kitchen light
  to 40%" where a person wrote "dim": no meaning is lost, and nothing is invented, but that
  verb is still the tool's rather than the user's.
- **Microphone and speech recognition are not granted on this machine** (`7/9` in the
  app's own self-test). The app reports the state and never asks for it: the Swift
  helper calls `authorizationStatus` only, because a CLI helper that requested access
  would either never answer or abort — so granting is a manual, per-machine step, not
  something this repository can fix in code · no notification tool ·
  no `ProjectGraph` delete API ·
  hardware integration blocked · external calendar/mail credentials ·
  LLM planner · multi-step **unattended** workflows · a partly-routable sentence can
  still answer one half without flagging the other in some phrasings (14-D, narrowed in
  Phase 19, not fully closed) · failed
  authentication is logged but deliberately **not** written to the audit chain, so an
  unauthenticated caller cannot append to it (16-F, reasoned in
  `audit/PHASE20_DATED_RECALL_REPORT.md`) · **arXiv still refuses this client** (15-F):
  `curl` gets 200 while urllib and httpx both get 406 from the same machine in the same
  minute, so `research` spends ~1.2 s on a retry, answers from the local corpus, and names
  the refusal instead of inventing a paper. Measured, not assumed — see
  `audit/PHASE21_SPEED_RESILIENCE_REPORT.md` §4 for the three diagnoses that were tested
  and discarded.
- **A retry now re-runs the failed steps** (26-A, closed by Phase 27). `task_steps` stores
  each step's own `params` and `device`, and `JarvisCore.retry_job()` rebuilds the failed
  ones into a plan that goes back through `PermissionEngine` — measured live: a workflow
  whose screenshot step failed answered `POST /tasks/{id}/retry` with `executed: 1` and the
  genuine second attempt, while a gated step answered `executed: 0` plus a **fresh**
  `confirmation_id`. Three edges it deliberately does not cross:
  **27-A** `resume()` still only lifts a pause -- a sequence halted by a *denied* step leaves
  later steps `pending`, and nothing dispatches those; the machinery exists
  (`_execute_plan(job_id=…, seqs=…)`), it is a small step and a separate decision about what
  "continue" should mean after a refusal. **27-B** a job written before Phase 27 has no
  stored parameters, so its retry is refused by the gate with `missing required parameter
  'path'` rather than being guessed at -- re-issuing the sentence is the way to run those.
  **27-C** the dashboard has no retry affordance; the endpoint is there and the page does not
  offer it. **27-D** five OCR tests (three of them `@pytest.mark.slow`) fail when the
  machine is already busy
  (measured: 5 failed / 1428 passed in a 46.9 s run, then the same file clean twice in ~4 s)
  -- the macOS Vision helper answers with no lines under load, and those tests need the
  capability/timeout treatment Phase 21 gave the battery and ffmpeg ones. Phase 29's skip
  accounting closed the question of whether CI even runs them: the runner's 6 skips are 5
  battery-gated plus 1 bundle-gated, so none of the five is skipped there and they do run.
  Reasoned in
  `audit/PHASE27_JOB_RETRY_REPORT.md` §5, and `audit/PHASE29_ROUTES_FILL_THEMSELVES_REPORT.md` §8.
- **Reading is no longer allowed to write** (Phase 28). Asking for a document's
  contents used to *author* one, because the design route matched the noun
  (`readme|guide|document`) and never looked at the verb: measured, "read the
  readme" replied *"Created documentation: read the readme (markdown) — …"* and left a
  new file on disk. An artifact route now has to be asked to author — decided by which
  verb leads the sentence, so "document the parser" still writes and "what does the
  document say" does not — and `read_file`, registered since v0.1 and reachable by no
  typed sentence until now, answers with the file's text.
  **28-A** was that one instance of a whole family, and Phase 30 measured the family: the
  count was not 28 but **46** tools that answered their own name with "I understood the
  words but not the goal" (`cancel_event`, `update_event`, `read_email`, `search_events`,
  `ping_device`, `get_device_capabilities`, `inspect_memory`, `get_schedule`,
  `list_workspaces`, `propose_code_change`, `apply_code_change`, the three `extract_paper_*`
  among them) — 28 had been the number of tools missing from `planner.py`, which is a
  statement about a source file, not about behaviour. Naming a tool's own words now reaches
  it or asks for the argument, so **the router no longer claims a capability gap it does
  not have**. What remains is meaning rather than spelling — see **30-B**, below. **28-B** was `make a diagram of the build` answering with
  *"missing required parameter 'components'"* — closed by Phase 29, below.
- **A route may not claim an action it cannot fill** (Phase 29). The flowchart and
  architecture-diagram routes had been dead since they were written: they sent a title
  alone to tools that also require `steps` / `components`, so the permission gate
  answered the user with a schema field name — even for a sentence that named them:

  ```
  >>> "draw a flowchart of onboarding: request, review, approve"
      decision=BLOCK  reply="I cannot do that: missing required parameter 'steps'."
  ```

  What a sentence states is now taken from it (a colon list, "with a, b", "of a, b"),
  the diagram's `id:label` grammar and its `>` arrows are derived rather than dropped,
  and `send_watch_notification` asks "What should the notification say?" instead of
  blocking on an empty title. Where the route cannot read the list cleanly it asks — the
  extractor is all-or-nothing, because trimming "request, review, approve with review as
  a decision" down to two steps would be a partial answer with nothing saying so.
  **29-A** remains: a route can now never emit an action missing a *required* parameter
  (asserted by sweeping the planner's own `Intent(tool=…, params={…})` literals against the
  registry), but *optional* ones are still not extracted — a presentation's slide content
  comes from a fixed outline, not from the sentence. **29-B**: measured this run,
  `research this idea` spent **41,247 ms** waiting on arXiv before its read timed out —
  the answer stayed honest, but nothing bounds that wait, which is the network equivalent
  of everything this phase is about. **29-C**: the same sweep covers only `params={…}`
  literals; a route that builds its dict in a variable is checked by behaviour tests, not
  by the static one. **29-D**: the CI check that guards against the built bundle shipping a
  stale `launcher.py` is skipped by every CI run -- `dist/` is gitignored, and the workflow
  builds the bundle *after* the test step, so the guard can only ever fire on a machine that
  happened to build one earlier. Measured while accounting for the runner's 6 skips
  (`audit/PHASE29_ROUTES_FILL_THEMSELVES_REPORT.md` §8); fixing it means reordering CI, which
  is a separate decision.
- **Naming a capability now reaches it** (Phase 30). A registry-derived route runs last in
  the chain: all of a tool's name words in the sentence reach the tool, `validate_params`
  has to approve the action before it is emitted, and a missing argument is asked about in
  the tool's own `description`. Reasoned in
  `audit/PHASE30_CAPABILITIES_ARE_REACHABLE_REPORT.md`.
  **30-A** was closed inside the phase rather than carried: `sweep_expired_memories` deletes
  permanently and was tiered `LOW` with no confirmation, fifteen lines from
  `clear_memory_scope`, which is `DESTRUCTIVE` and confirms — the only difference was that no
  sentence could reach the first one. It is `DESTRUCTIVE` + confirmation now, in the same
  commit as the route that made it typeable. What is carried:
  **30-B** reachability is by *spelling*, not meaning — `"ping my phone"` still matches
  nothing, because `ping_device` requires the word "device"; an alias vocabulary is the next
  gain and the next source of false matches. **30-C** a case-by-case route still wins where it
  claims the sentence, even when it reads it less precisely: `"list car metrics"` is answered
  with car status (an honest reply, the wrong question). **30-D** 55 capabilities can be named
  but not run, because their argument is an id nobody has memorised and no route offers the
  list first — `list_workspaces` then `close_workspace` is two sentences apart, and the
  multi-step machinery from Phase 14 already exists to close that gap.
- **A fallback still lends its summary to the memory sentence** (26-B, R-14's remaining
  half). The ledger row for the planned step now carries `answered_by: list_directory`, and
  the outcome carries `answered_for: search_files`, so the chain can be traced; what it does
  not yet do is phrase the remembered fact as *"asked to search; a directory listing
  answered it"*. The reply says the same thing in prose (`search_files: recovered using
  list_directory`), so this is a wording gap in one stored sentence, not a false claim.
- **`ToolSpec.parameters` is still a plain dict** — `frozen=True` stops `spec.parameters =
  {}`, and the three list fields the gates read became tuples so they cannot be appended to
  in place, but `spec.parameters["x"] = "y"` still works. Nothing authorises from that map
  (only `validate_params`, which rejects *extra* parameters), so widening it cannot grant
  permission; it is named here rather than pretend the freeze is total.

## License

**No licence file exists in this repository**, so no permission to copy, modify or
distribute is granted — default copyright applies. This is stated rather than assumed
because the neighbouring repositories in this account carry `LICENSE` files and it would
be easy to paste an MIT badge that nothing backs. Add a `LICENSE` and this section will
tell the truth about it.
