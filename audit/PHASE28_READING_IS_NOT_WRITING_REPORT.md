# Phase 28 — Asking to read something must not make JARVIS write something

Report date: 2026-09-30. Continues Phases 9-27.

No brief. The scope came from the reachability sweep this phase ran to answer a different
question -- "which of the tools README lists as unrouted are actually unreachable?" -- and
the first probe returned this:

```
>>> "read the readme"
    intent=create_documentation  decision=EXECUTE  tools=['documentation_created']
    reply='Created documentation: read the readme (markdown) -- read-the-readme-2340c886.md'
```

A sentence asking to *read* authored a new file, audited it as a success, and told the user
it was done. That is not a mislabelled state, which the previous five phases were all
about. It is an unrequested side effect, chosen by a substring.

---

## 1. The mechanism, and why "read" is the trigger

One regex in the design route:

```python
docs = re.search(r"\b(document\w*|readme|guide|write up the docs)\b", text)   # planner.py:1165
...
if docs:                                                    # planner.py:1209
    title = _title()                                        # the whole sentence
    return Intent("create_documentation", tool="create_documentation", params={"title": title})
```

The noun decides, and the verb is never consulted. "readme" contains the word "read", so
the more literally a user asked to read, the more certainly it matched. Measured, each of
these wrote a file:

```
"read the readme"            -> Created documentation: read the readme …
"show me the readme"         -> Created documentation: show me the readme …
"open README.md"             -> Created documentation: open README.md …
```

Two neighbouring truths the same probe exposed:

* **`read_file` was never reachable.** Registered since v0.1, risk tier READ, in
  `IDEMPOTENT_TOOLS`, and listed in README under Known open items as "no `read_file` route"
  since Phase 12 -- so the system had the capability and could not be asked to use it, while
  it could be *tricked* by the same sentence into using one it wasn't asked for.
* **It is not a one-off.** Measured over the registry: **29 of 127** tools appear nowhere in
  `planner.py` as a route -- `cancel_event`, `update_event`, `search_events`, `read_email`,
  `inspect_memory`, `get_schedule`, `list_workspaces`, `ping_device`,
  `get_device_capabilities`, `propose_code_change`, `apply_code_change`,
  `extract_paper_methods/limitations/datasets`, `sweep_expired_memories`, `forget_paper`,
  the `link_*`/`add_*` graph tools, and others. For those, "I understood the words but not
  the goal yet" is true of the *router* and reads to a person as "I can't".

---

## 2. The fix, and the rule it is built on

**An authoring route has to be asked to author.** Not a blocklist of naughty words --
`_authoring_request()` compares which verb class speaks *first* in the sentence:

```python
_AUTHOR_VERBS  = create|make|design|draw|write|build|generate|produce|prepare|add|draft|
                 author|update|extend|revise|document
_CONSUME_VERBS = read|show|open|get|list|display|print|cat|what|which|tell|find|search|
                 say|says|summarise|summarize|explain|describe|check|review|look
```

`document the parser` → authoring first → still authors. `what does the document say` →
consumption first → does not. The guard sits after the two *non-authoring* design branches
(analyzing and listing assets), which are unaffected, and before every branch that creates
an artifact.

**`read_file` becomes reachable.** A new rule, registered between the vision and
file-search rules, resolving names by one stated rule rather than two opinions:

| Input | Resolves to | Why |
|---|---|---|
| `read notes.txt` | `<file_workspace>/notes.txt` | a bare name is anchored, so a read cannot mean "whatever sits next to the process" |
| `read ./sub/notes.txt` | `<file_workspace>/sub/notes.txt` | a relative path is anchored the same way |
| `read /etc/hosts`, `~/…` | as typed | an explicit path is the engine guard's business, and refusing it here with write-shaped wording would be its own untruth |
| `read the readme` | `<file_workspace>/README.md` | the alias is spelled out, not guessed from the sentence |

**The reply shows what was read, capped.** `read_file` answered "Read README.md." before,
which meant a successful read showed the user nothing; the message now carries the text,
truncated at 1,200 characters with `… (rest withheld…)` -- because that message is also what
`_remember_outcome()` writes into memory, so an uncapped read would copy a whole file into
the store. The full content stays in `output.content`.

---

## 3. Live, over HTTP, on a launched server

`JARVIS_FILE_WORKSPACE` pointed at a scratch tree containing a three-line `README.md`:

```
=== reading must not write ===
--- "read the readme"
    intent: read_file | decision: EXECUTE | tools: ['read_file']
    reply: Read README.md: / # AMR main board / battery is at 44 percent / heater coil arrived Tuesday
--- "show me the README.md"      intent: read_file | decision: EXECUTE
  workspace now: README.md                     # nothing added

=== authoring still authors ===
--- "write documentation for the parser"
    intent: create_documentation | decision: EXECUTE | tools: ['documentation_created']

=== the ledger row for the read ===
    read_file | success | {"path": "/private/var/folders/…/README.md"}

=== a missing file, and a protected path ===
--- "read the file absent.txt"
    reply: That did not work: File not found: /private/var/folders/…/absent.txt
--- "read the file /etc/hosts"
    intent: read_file | decision: BLOCK | tools: []
    reply: I cannot do that: /private is a protected system location.
```

The last two lines are the shape this phase argued for: the route claims the sentence, the
tool or the guard answers for it, and neither invents a document.

---

## 4. What building this found in the router's own reporting

A rule that raises is swallowed and reported as "no rule matched":

```python
except Exception as exc:                                    # planner.py:228
    log.info("planner rule error", extra_fields={"error": str(exc)})
```

`log.info`, under a logger defaulted quiet, with no rule name. So **a route broken on every
single call is indistinguishable from a route that declines to match**, and the user-facing
symptom is "I understood the words but not the goal" -- a sentence problem, not a crash.

This is not hypothetical: my own first patch deleted a line from `_append_file`, and the
only reason it surfaced at all is that `test_phase12_command_path.py` happens to cover the
append route. Every other phrasing that rule used to answer would simply have become
unrecognised, silently. It now logs at `warning` with the rule's name and the exception
type, pinned by `test_a_raising_rule_is_named_and_not_mistaken_for_no_match` -- which
attaches its own handler, because the project logger does not propagate and `caplog` sees
nothing from it. That is the second time this program has found a diagnostic path that could
not be observed by the normal means (Phase 26's swallowed failure record is the first).

---

## 5. Regressions

`tests/test_phase28_reading_is_not_writing.py` -- **33 tests**, red first: 22 failed and 1
passed on the unfixed tree. (Only the passing case's identity was not recorded as it ran, so
none is claimed here.) Grouped as: 28-A reading never
authors (8 read phrasings × no-write, 4 authoring phrasings × still authors, 1 parked
diagram note), 28-B the read route (6 phrasings reach `read_file`, missing file, protected
path, workspace anchoring, excerpt cap, no confirmation for a read), 28-C reachability and
the README claim (3), plus the rule-error test and the structural guard test.

No existing test needed changing. The acceptance sweep moved **no command's intent, state or
audit token** -- checked row by row against the committed artifact -- which is the result
this change should have: it touches sentences that were previously misrouted, not the
routings 18 commands already exercise.

---

## 6. Everything re-measured after the change

| | |
|---|---|
| Suite | **1466 passed · 0 failed · 0 skipped · 0 xfailed** (1466 collected), 52 modules |
| Reachability | unrouted registered tools: **29 → 28** (the sweep is asserted by count, on purpose -- it moves only when someone routes another capability) |
| Acceptance | 17 of 18 measured, median **5 ms**, sum **1,395 ms**; states unchanged from the Phase 27 artifact for all 18 rows |
| Safety matrix | **5 of 6 ok**, `turn on heater` still FAIL (22-G, owner's call) |
| Registry | 127 tools, 25 gated, tiers read 68 · low 33 · external 12 · destructive 11 · physical 3 -- unchanged; this phase added no capability, it made an existing one reachable |
| Figures | charts and diagrams regenerated; `chart-suite-growth.svg` ends at the Phase 28 point |

---

## 7. Carried forward

* **28-A** 28 tools remain unreachable from a sentence. The list is in §1. Routing them is a
  product decision per capability (and some, like `approve_pairing`, deliberately have no
  spoken form); the honest statement now is that the *count* is measured rather than
  remembered.
* **28-B** `make a diagram of the build` blocks with *"missing required parameter
  'components'"* -- an internal schema field shown to a human instead of a question they
  could answer. Asserted as-is so the suite records it; the fix is a decision about how the
  gate asks for missing parameters, and it applies to every gated tool, not just diagrams.
* **28-C** A consumption verb that leads a sentence declines the artifact route outright, so
  "summarise my last design asset" or "review the diagram you made" now falls through to
  "not understood" where it used to produce something. The old behaviour was worse (it
  authored on demand), but the gap is real and belongs with 28-A.
* Everything already open: 27-A…D, 26-A closed by Phase 27, 26-B, the `ToolSpec.parameters`
  residual, 22-G, 23-E, mic/speech TCC, 15-F, 14-D, 16-F, hardware **BLOCKED**, LLM planner,
  unattended workflows.

---

## 8. Git

Committed and pushed to `main` on `abhijeet1267/jarvis-forge`:

```
0888cf1  Reading a document must not author one
         11 files changed
```

CI run `36693836791`, verdict copied from the runner after it finished:

```
conclusion=success
  suite (3.11): success
  suite (3.12): success
```

The 33 new tests ran on both macOS images, including the rule-error test that has to attach
its own logging handler because the project logger does not propagate -- the one place this
phase could not assert through the obvious fixture, recorded so the next reader does not
" simplify" it back to `caplog` and watch it pass for the wrong reason.
