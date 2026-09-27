<img src="assets/hero.svg" alt="JARVIS Forge — a permissioned, local-first agent OS" width="100%"/>

# JARVIS Forge

A permissioned, **local-first** personal agent OS. You type or say a sentence; it is
planned by deterministic rules, gated by a permission engine, held for confirmation
when the risk says so, executed against your real machine, **verified by observing the
world afterwards**, and written to a tamper-evident audit chain. It runs with no API
key, no network and no vendor — an LLM is optional and off by default.

What this repository is mostly about is not features. It is about the difference
between *"the handler returned"* and *"the thing happened"* — and the twenty phases of
measurement spent closing that gap, in the ledger and on the screen.

[![ci](https://img.shields.io/github/actions/workflow/status/abhijeet1267/jarvis-forge/ci.yml?label=tests%20%C2%B7%20macos)](https://github.com/abhijeet1267/jarvis-forge/actions/workflows/ci.yml)
[![suite](https://img.shields.io/badge/tests-1286%20passed%20%C2%B7%200%20failed-16a34a)](tests)
[![tools](https://img.shields.io/badge/tools-127%20across%2015%20agents-2f6df6)](jarvis/tools)
[![offline](https://img.shields.io/badge/LLM-off%20by%20default%20%C2%B7%20runs%20fully%20offline-0f172a)](jarvis/config.py)
[![python](https://img.shields.io/badge/python-3.11%20%C2%B7%203.12%20(CI)-6b7688)](.github/workflows/ci.yml)

| | |
|---|---|
| **Runtime** | Python 3.11+ · FastAPI · SQLite (32 tables) · stdlib-first |
| **Planning** | `RulePlanner` — 38 deterministic routes, no model required |
| **Authority** | `PermissionEngine` · 5 risk tiers · 25 confirmation-gated tools |
| **Proof** | tri-state `Verification` (success / failed / **unknown**) on every action |
| **Ledger** | `action_logs` hash-chained + `audit.jsonl` append-only, `verify_chain()` |
| **Edge** | token file `0600` · constant-time compare · same-origin · Host allowlist · 600/min |
| **Surface** | web dashboard · voice (macOS `say`/STT) · watch queue · phone pairing |
| **Size** | 47,038 lines · 32,103 source · 14,935 tests · 44 test modules |

---

## Verified status

Every figure below is produced by a command in the right-hand column. Nothing here is
typed in by hand: the charts in `assets/` are **generated** from `benchmarks/*.json`,
and the diagrams read their counts from the live registry.

| Claim | Value | Reproduce |
|---|---|---|
| Test suite | **1286 passed · 0 failed · 1 skipped · 3 xfailed** (1290 collected) | `python -m pytest -q` |
| Suite growth, Phase 9 → 20 | 875 passing / **126 failing** → 1286 passing / 0 failing | `cat benchmarks/suite_history.json` |
| Tools registered | **127** | `python -c "from jarvis.core.orchestrator import JarvisCore as J; print(len(J().tools.names()))"` |
| Confirmation-gated | **25** of 127 | `python assets/build_diagrams.py` (prints the live counts) |
| Risk tiers | read 68 · low 33 · external 12 · high 11 · physical 3 | same |
| API routes | **104** | `python -c "s=open('jarvis/api/app.py').read(); print(sum(s.count(f'@app.{v}(\"') for v in ('get','post','put','patch','delete')))"` |
| Acceptance commands measured | 17 of 18, live, over HTTP | `python benchmarks/acceptance_run.py` |
| Audit chain integrity | `verify_chain() → ok` on a fresh home | `JARVIS_HOME=$(mktemp -d) python -c "from jarvis.core.orchestrator import JarvisCore as J; print(J().audit.verify_chain())"` |
| Regenerated figures parse as SVG | 7/7 | `python assets/build_charts.py && python assets/build_diagrams.py` |

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
| Packaged desktop app | **BROKEN — exits 1** | Phase 9/10 finding, still open; see [Downloads](#downloads) |
| Live browser-driven UI test | **NOT PERFORMED** | browser interaction is blocked by the agent runtime's policy; the page's JS is instead executed from the shipped file by Node |

---

## Contents

- [How a sentence becomes a verified act](#how-a-sentence-becomes-a-verified-act)
- [The state vocabulary](#the-state-vocabulary)
- [Benchmarks](#benchmarks)
- [Hardware truth](#hardware-truth)
- [The twenty phases](#the-twenty-phases)
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

### Suite growth, Phase 9 → Phase 20

<img src="assets/chart-suite-growth.svg" alt="Test suite growth from 875 passing with 126 failing to 1286 passing with none failing" width="100%"/>

Phase 9 inherited **126 failing tests**. The cause was a single import line in
`jarvis/tools/base.py` that left `Verification` undefined, so every tool result raised
`NameError` on construction — one undecorated import, 126 red tests, and every offline
read auditable as a success.

### The plan's own acceptance list, measured

<img src="assets/chart-acceptance-states.svg" alt="Eighteen acceptance commands with their terminal state, audit token and latency" width="100%"/>

| Command | State the user sees | Audit token | ms |
|---|---|---|---|
| show me my laptop battery | VERIFIED | success | 23 |
| plan my day | VERIFIED | success | 5 |
| remember that this is my AMR project | VERIFIED | success | 5 |
| what did I work on yesterday | VERIFIED | success | 6 |
| check my car fuel | **UNAVAILABLE** | failure | 5 |
| turn on study mode | **UNAVAILABLE** | failure | 6 |
| transfer this file to my phone | **NOTHING EXECUTED** | no_action | 6 |
| design this system | **NOT RECOGNISED** | no_action | 5 |
| schedule two hours for project work tomorrow | **NOTHING EXECUTED** | no_action | 4 |
| draft an email to my teammate | **FAILED** | failure | 6 |
| research this idea | **FAILED** | failure | 419 |
| find my latest project report | VERIFIED | success | **11,299** |

Median command: **5 ms**. The two outliers are real and unresolved: a full-filesystem
`search_files` (11.3 s) and arXiv rejecting the research request with HTTP 406.

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

## The twenty phases

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
  could not, instead of a bare "Forgotten".
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
python -m pytest -q                       # 1286 passed, 0 failed
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
tests/         44 modules · 1290 collected · includes the dashboard's own JS, run by Node
dashboard/     single-file control panel; state rendered from server fields
audit/         22 phase reports + 4 CSV matrices — the evidence behind this README
benchmarks/    acceptance_run.py and the JSON the charts are drawn from
assets/        generated figures + the two generators that draw them
app/           PyInstaller spec and bundle sources (see Downloads — currently broken)
```

## Downloads

**Source only.** There is no packaged application to download, and publishing one would
be a lie: the desktop bundle exits 1 on launch, which is an open Phase 9/10 finding
rather than a packaging step I can wave away.

- [Download the source (`.zip`)](https://github.com/abhijeet1267/jarvis-forge/archive/refs/heads/main.zip)
- [Download the source (`.tar.gz`)](https://github.com/abhijeet1267/jarvis-forge/archive/refs/heads/main.tar.gz)
- Build it yourself with `python build_app.py` — and expect it to fail until that
  blocker is closed. `install_app.sh` installs nothing until then.

## Known open items

Stated plainly, because the point of this repository is that a gap you can name is
better than a gap you have to discover:

- Desktop bundle exits 1 · no `read_file` route · no notification tool ·
  `POST /memory/{id}/forget` still over-claims · no `ProjectGraph` delete API ·
  risk tier is mutable at runtime (RT-3) · recovery retries too broadly (R-2) ·
  R-11–R-14 · hardware integration blocked · external calendar/mail credentials ·
  LLM planner · multi-step **unattended** workflows · a partly-routable sentence can
  still answer one half without flagging the other in some phrasings (14-D, narrowed in
  Phase 19, not fully closed) · a refusal and a clarifying question share one label
  because separating them would mean guessing from punctuation (19-B) · failed
  authentication is logged but deliberately **not** written to the audit chain, so an
  unauthenticated caller cannot append to it (16-F, reasoned in
  `audit/PHASE20_DATED_RECALL_REPORT.md`).

## License

**No licence file exists in this repository**, so no permission to copy, modify or
distribute is granted — default copyright applies. This is stated rather than assumed
because the neighbouring repositories in this account carry `LICENSE` files and it would
be easy to paste an MIT badge that nothing backs. Add a `LICENSE` and this section will
tell the truth about it.
