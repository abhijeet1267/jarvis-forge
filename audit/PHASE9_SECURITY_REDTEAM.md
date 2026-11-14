# PHASE 9 — PHASE 4: Permission / Safety Red Team

Target: prove or disprove that an agent or tool can bypass the permission engine.
Executed 2026-09-25.

## 4.0 Method and what was attacked

Every attack below was **executed**, not reasoned about, except where a row is
explicitly marked STATIC. Two harnesses were used:

1. **Clean shadow copy** `/tmp/p9shadow` — byte-identical source with the P0 import
   defect repaired locally, because in the repo as-found `ok()` raises `NameError` and a
   crashed tool cannot demonstrate an authorisation bug. **The real repo was not modified.**
   A reproducibility control confirmed the copy is faithful (unfixed copy gives
   126 failed / 875 passed vs 126 failed / 875 passed in place).
2. **Live HTTP server** on `127.0.0.1:8765` with a throwaway `JARVIS_HOME`, exercising
   the real `/command` → `/watch/reply` → `/confirm` chain.

Destructive proofs used disposable fixtures under `/tmp` only. The user's real
`~/.jarvis-forge` was never written. `command_robot_motion` was never allowed to reach a
bridge; no real device was actuated.

**Result: 22 attacks. 19 held. 1 P0 escape and 1 P1 escape confirmed.**

## 4.1 THE P0 — a simulated, unpaired watch can authorise a real destructive action

|              |                                                                                                                                                                                                                                                                                                                                                                                                           |
| ------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **attack**   | Have a _simulated_ watch wearer answer `CONFIRM` to a pending confirmation for a real `delete_file`, and see whether the deletion executes.                                                                                                                                                                                                                                                               |
| **setup**    | `mode = simulate`, `paired = False` (run 1) then `paired = True` (run 2). Real `PermissionEngine.evaluate()` produces the confirmation. Real `ask_watch_confirmation` tool produces the wrist prompt. Real `WatchGateway.reply()` records the wearer's answer. Real `JarvisCore.resolve_confirmation()` executes. This is the exact call sequence of `POST /watch/reply` (`jarvis/api/app.py:2045-2071`). |
| **expected** | Refused. The gateway's own docstring states the rule: _"A simulated wearer **cannot** approve a confirmation, because a runner that approves on the user's behalf is exactly what this system forbids."_ — `jarvis/watch/gateway.py:16-19`                                                                                                                                                                |
| **actual**   | **Executed.** Fixture file `/tmp/p9_watch_victim.txt` and `/tmp/p9_watch_victim2.txt` were both moved to Trash. Final reply: `"Moved p9_watch_victim2.txt to Trash."`                                                                                                                                                                                                                                     |
| **result**   | **FAIL — authorisation escape**                                                                                                                                                                                                                                                                                                                                                                           |
| **severity** | **P0**                                                                                                                                                                                                                                                                                                                                                                                                    |

Measured output, realistic path (prompt created by the real tool):

```json
{
  "watch_mode": "simulate",
  "gateway.paired": true,
  "tool_msg": "Confirmation prompt \"delete_file on laptop_main | path=/tmp/p9_watch_victim2.txt\" is shown on the watch.",
  "item": {
    "state": "displayed",
    "via": "simulated",
    "choices": ["CONFIRM", "CANCEL"]
  },
  "reply_error": "",
  "recorded": "CONFIRM",
  "final_reply": "Moved p9_watch_victim2.txt to Trash.",
  "VICTIM_EXISTS": false
}
```

And unpaired (default state of this machine, `auth_state: "unpaired"`):

```json
{
  "watch_mode": "simulate",
  "watch_paired": false,
  "item_state": "displayed",
  "delivered_via": "simulated",
  "reply_error": "",
  "confirmation_state_after_reply": "pending",
  "executed_reply": "Moved p9_watch_victim.txt to Trash.",
  "VICTIM_FILE_STILL_EXISTS": false
}
```

### Root cause

`reply()` guards three things — unknown item, item with no buttons, and a prompt already
resolved elsewhere — and one of them is the delivery check:

```python
# jarvis/watch/gateway.py:440-446
if item.state in ("queued", "failed"):
    # The wearer cannot have pressed a button the watch never showed.
    return item, "That prompt has not reached your watch, so there is nothing to answer yet."
```

That guard is **only correct for real transports.** The simulated transport reports a
confirmed delivery, which promotes the item straight past `queued`:

```python
# jarvis/watch/gateway.py:115-118  SimulatedTransport
name = "simulated"
def deliver(self, payload): return DeliveryResult(True, True, "simulated", "delivered to the simulated companion")
# jarvis/watch/gateway.py:382
item.state = "displayed" if result.confirmed else "delivered"
```

So `state == "displayed"` and `delivered_via == "simulated"`, and `reply()` never inspects
either the gateway's `simulate` property or `item.delivered_via`. It returns an empty
error string, which `POST /watch/reply` treats as success, and line 2062
(`approved = item.reply == "CONFIRM"`) turns a fabricated button press into
`resolve_confirmation(cid, approve=True)` on a real risk-3 destructive action.

The failure is precisely the class the brief named: **simulated hardware pretending to be
real**, and **a physical action executing on an approval that no physical user gave.**

### Smallest safe repair — NOT applied (Phase 15)

One guard in `WatchGateway.reply()`, before the choices check, honouring the invariant the
docstring already claims:

```python
if self.simulate or item.delivered_via == "simulated":
    return item, ("A simulated watch cannot approve anything. Connect a real watch, "
                  "or confirm in the dashboard.")
```

This fails closed, touches no other path, and is directly testable. It is **not**
implemented — authorisation is required.

Two supporting changes are recommended alongside it, also not applied:
`ask_watch_confirmation` should report `Verification.UNKNOWN` rather than `SUCCESS` when
`delivered_via == "simulated"` (it currently claims success for a prompt nobody saw), and
`/watch/status` should surface `simulate` in the same field the dashboard trusts.

## 4.2 The P1 — a confirmation is never re-checked before it executes

|              |                                                                                                                                                                                        |
| ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **attack**   | Revoke the permission _after_ the confirmation exists, then approve it.                                                                                                                |
| **setup**    | `evaluate(delete_file /tmp/p9_rt_rev.txt)` → CONFIRM. Then `PermissionEngine.revoke("delete_file")`, which writes an explicit `granted=0` row. Then `resolve_confirmation(cid, True)`. |
| **expected** | Execution refuses: the tool's authority was withdrawn while the approval was in flight.                                                                                                |
| **actual**   | **`after_revoke_executed = True`** — the action ran.                                                                                                                                   |
| **result**   | **FAIL**                                                                                                                                                                               |
| **severity** | **P1**                                                                                                                                                                                 |

Same probe shows a device that went offline between confirmation and approval is also not
re-checked: `evaluate` on an offline device correctly returns `BLOCK`, but an in-flight
confirmation for that device still resolves.

### Root cause

`resolve_confirmation` rebuilds the action from the stored record and executes it
**without re-entering the engine**:

```python
# jarvis/core/orchestrator.py:368-390
action = PlannedAction(step_id=1, tool=record["tool"], params=record["params"],
                       device=record["device"], reason="confirmed by user")
...
result = await agent.execute(action, ctx)
```

`jarvis/core/orchestrator.py:310-407` contains no call to `policy.evaluate()`. Everything
the engine checks at confirmation-creation time — `validate_params`, device existence and
`device.online`, `_path_guard`, `_is_revoked`, the risk classifier — is therefore a
**point-in-time** assertion, while the action executes later.

This is a genuine TOCTOU gap, though a narrow one: the window is bounded by
`confirm_timeout` (300 s default) and the record itself is immutable, so the classic
approve-X/execute-Y substitution is _not_ possible (4.3). The exposure is withdrawal, not
escalation.

Contrast with the scheduler, which does this correctly — it re-evaluates on every run:

```python
# jarvis/scheduler/runner.py:83-84
# Guard 2: re-evaluate through the permission engine, every run.
verdict = core.policy.evaluate(action, session_id=f"sched:{job.schedule_id}")
```

### Smallest safe repair — NOT applied

Re-run the cheap withdrawal checks before executing, reusing the existing engine:

```python
recheck = self.policy.evaluate(action, session_id=session_id, request_id=request_id)
if recheck.decision is not PolicyDecision.EXECUTE:
    response.reply = f"That is no longer allowed: {recheck.reason}"
    response.decision = PolicyDecision.BLOCK
    ... audit ...
    return response
```

`_path_guard` and `_is_revoked` are pure lookups, so the cost is negligible. Note the
interaction to handle: `evaluate()` on a still-`CONFIRM` action would create a _second_
confirmation, so the re-entry must be a check-only variant rather than a naive `evaluate()`
call. Authorisation required before implementing.

## 4.3 Attacks that held — the engine's core guarantees

Every one of these was attempted and **refused correctly**.

| #   | attack                                          | setup                                                                 | expected                                        | actual                                                                                                                               | result   |
| --- | ----------------------------------------------- | --------------------------------------------------------------------- | ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ | -------- |
| 1   | protected path, direct                          | `read_file(path=/etc/passwd)`                                         | BLOCK                                           | `/private is a protected system location.`                                                                                           | **PASS** |
| 2   | protected path via aliased param                | `search_files(directory=/usr/local/bin)`                              | BLOCK regardless of param name                  | `/usr is a protected system location.`                                                                                               | **PASS** |
| 3   | protected path hidden in a list                 | `args=[/var/root/.ssh/id_rsa]`                                        | BLOCK                                           | `.ssh is a protected private location.`                                                                                              | **PASS** |
| 4   | protected path in nested dict                   | `meta.target=~/.ssh/id_rsa`                                           | BLOCK                                           | `.ssh is a protected private location.`                                                                                              | **PASS** |
| 5   | path traversal                                  | `path=~/Documents/../../../etc/passwd`                                | BLOCK                                           | `/private is a protected system location.`                                                                                           | **PASS** |
| 6   | credential files                                | `~/.aws/credentials`, `~/.zshrc`, `~/Library/LaunchAgents/evil.plist` | BLOCK                                           | all three refused                                                                                                                    | **PASS** |
| 7   | **symlink escape**                              | `/tmp/x/escape -> ~/.ssh`, request `/tmp/x/escape/id_rsa`             | BLOCK                                           | `.ssh is a protected private location.`                                                                                              | **PASS** |
| 8   | unrestricted shell                              | inspect `allowed_commands` / `powerful_commands`                      | no `rm/sudo/bash/sh/python/curl/dd`             | `allowed_n=17`, `powerful_n=0`, intersection `[]`                                                                                    | **PASS** |
| 9   | interpreter execution                           | same allowlist, argv-only exec                                        | no interpreter                                  | `create_subprocess_exec(*args)` — never `shell=True` (`jarvis/tools/base.py:47-71`)                                                  | **PASS** |
| 10  | **confirmation replay**                         | approve the same `CONF` id twice                                      | 2nd refused                                     | `"That request was already approved."`, `decision=BLOCK`                                                                             | **PASS** |
| 11  | **confirmation substitution**                   | approve `delete_file`'s CONF, expect another tool to ride it          | action rebuilt from stored record               | executed tool/params/device all taken from the DB row, never from the caller (`orchestrator.py:368-374`)                             | **PASS** |
| 12  | **confirmation ID guessing**                    | `resolve_confirmation("CONF-"+random 128-bit)`                        | no record → nothing runs                        | `"I have no record of that confirmation request."`, `executed=False`                                                                 | **PASS** |
| 13  | **expired confirmation**                        | `confirm_timeout=1`, wait 2.2 s, approve                              | refused, nothing runs                           | `decision=BLOCK`, `state=expired`, `executed=False`, reply `"…expired before it was answered, so I did nothing."`                    | **PASS** |
| 14  | watch answering a prompt with no buttons        | `queue(notification, choices=())`, reply `CONFIRM`                    | refused                                         | `"That watch item has nothing to answer."`                                                                                           | **PASS** |
| 15  | approving a prompt that never reached the wrist | force `state="queued"`, reply `CONFIRM`                               | refused                                         | `"That prompt has not reached your watch, so there is nothing to answer yet."`                                                       | **PASS** |
| 16  | **scheduler exceeds its ceiling**               | job with `max_risk=LOW` wrapping risk-3 `delete_file`                 | blocked, file untouched                         | `outcome=blocked`, `file_survived=True`, `"A schedule cannot raise its own authority."`                                              | **PASS** |
| 17  | **scheduler self-approving**                    | job whose ceiling _does_ permit a destructive action                  | park as awaiting approval, never execute        | `outcome=awaiting_approval`, `file_deleted_by_scheduler=False`                                                                       | **PASS** |
| 18  | agent self-authorisation                        | inspect `Agent` base class                                            | agents only execute already-adjudicated actions | methods `['execute','owns','tool_names']`; no engine reference; `PermissionEngine.evaluate` has exactly one caller, the orchestrator | **PASS** |
| 19  | emergency stop must not be gated                | `risk>=3` without `requires_confirmation`                             | exactly one such tool                           | `['emergency_stop']`, sole holder of the `safety` tag                                                                                | **PASS** |

Attacks 1-7 confirm the guard is param-name-agnostic and symlink-aware —
`resolve_path` uses `Path.resolve()` before comparison (`jarvis/security/paths.py:131-137`),
and `_path_guard` walks _values_ at any depth (`paths.iter_string_values`), which is what
closes the "guard only looks at a param literally called `path`" hole the docstring says
was previously open.

Attacks 16-17 are the inverse of 4.1/4.2: the **scheduler** re-checks authority on every
run, the **confirmation path** does not, and the **watch** checks delivery but not
authenticity.

## 4.4 Additional finding — risk tier is mutable at runtime (P1)

|              |                                                                                                                                                              |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **attack**   | A compromised or buggy tool elevates its own risk tier in the live registry, then re-requests the same action.                                               |
| **setup**    | Fetch the real `ToolSpec` for `delete_file`, set `risk = RiskLevel.READ` and `requires_confirmation = False` in the process-wide registry, then re-evaluate. |
| **expected** | A destructive tool cannot be re-adjudicated as a read.                                                                                                       |
| **actual**   | `before=(3,True)` → `after=(0,False)` → `decision_after_mutation = "EXECUTE"`                                                                                |
| **result**   | **FAIL**                                                                                                                                                     |
| **severity** | **P1**                                                                                                                                                       |

The classifier trusts the registry object graph directly
(`jarvis/security/risk.py:78-105` reads `spec.risk` / `spec.requires_confirmation`), and
`ToolSpec` is a mutable pydantic model in a singleton registry. Any code that can import
`jarvis.tools` can downgrade every tool's declared risk with two attribute assignments, and
the engine will honour it. This is not reachable from model output or from the HTTP API —
it needs code execution inside the process, so it is a _containment_ weakness rather than a
remote escape. It is exactly the property the brief asked to verify: **"Tools cannot
silently elevate their own risk tier"** is currently **false**.

Suggested minimal hardening (not applied): validate the decision against the persisted
`tools` table (which already mirrors every spec — `jarvis/tools/__init__.py` docstring) or
freeze specs with `model_config = ConfigDict(frozen=True)` after registry construction.

## 4.5 Standing grants widen the blast radius of any future mislabel (P2, informational)

`evaluate()` will downgrade `CONFIRM` → `EXECUTE` when a standing grant exists, the tool is
`reversible`, and the tool is not trust-sensitive:

```python
# jarvis/security/policy.py:152-158
if (decision is PolicyDecision.CONFIRM and spec.reversible
        and self.is_granted(action.tool, action.device)
        and not self._trust_sensitive(action)):
    decision, reason = PolicyDecision.EXECUTE, "covered by a standing grant"
```

Measured exposure: **15 tools** are `risk>=2` **and** `requires_confirmation=True` **and**
`reversible=True`, including `delete_file` (risk 3), `move_file`, `copy_file` and
`run_allowlisted_command`. One grant makes every future repeat of those silent.

Two things limit this, and both were verified as working:

- `_trust_sensitive` exempts `approve_pairing`, `remove_phone`, `register_phone`, and any
  `send_to_device` whose target is unpaired or unknown (`policy.py:173-206`) — so a grant
  earned on one device cannot silently authorise trust changes or transfers to a new device.
- The design is documented and intentional; the risk is that `reversible=True` on
  `delete_file` rests entirely on `trash_on_delete` being enabled. If that setting is ever
  turned off, `delete_file` becomes irreversible while remaining eligible for standing-grant
  auto-execution. **The reversibility claim and the trash setting are not linked in code.**
  Recommended: make `delete_file`'s effective `reversible` derive from
  `settings.trash_on_delete` at decision time. Severity P2, no change made.

## 4.6 "Agents can request actions but cannot authorise actions" — verdict

**Holds for the agent layer; broken at the periphery.**

- No `Agent` subclass reaches the engine: `JarvisCore` calls `policy.evaluate()`; agents
  receive an already-adjudicated `PlannedAction`. Attack 18.
- No tool can approve its own confirmation: approvals require a `CONF-<128-bit>` id that is
  only ever minted by `_create_confirmation` and returned to the human-facing surface.
  Attack 12.
- **But** the two escapes in 4.1 and 4.2 are both cases where _something other than the
  permission engine_ effectively decides: a simulated device's fake button press, and an
  approval whose authority was withdrawn after the fact.

## 4.7 RT-7 — `revoke()` silently fails to revoke (P1)

Found by probing the standing-grant path described in 4.5. This is a **kill-switch failure**.

|              |                                                                                                                                                                                  |
| ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **attack**   | Grant a standing permission on the risk-3 destructive `delete_file`, then revoke it, and check whether confirmation gating is restored.                                          |
| **setup**    | `grant("delete_file", device="laptop_main")` → `evaluate()` becomes `EXECUTE`. Then `revoke("delete_file", device="laptop_main")` → re-evaluate.                                 |
| **expected** | `BLOCK`/`CONFIRM` restored — an explicit revocation must always win.                                                                                                             |
| **actual**   | `after revoke(no dev): EXECUTE \| _is_revoked False` and `after revoke(dev): EXECUTE \| _is_revoked False`. **The tool keeps auto-executing with no confirmation, permanently.** |
| **result**   | **FAIL**                                                                                                                                                                         |
| **severity** | **P1**                                                                                                                                                                           |

### Root cause — a timestamp tie that never resolves

`revoke()` writes a row but does **not** set `granted_at`, so the column default fills it:

```sql
-- jarvis/db/database.py, table permissions
granted_at  TEXT NOT NULL DEFAULT (datetime('now')),
```

```python
# jarvis/security/policy.py:84-93 — revoke() inserts granted=0 and revoked_at,
# but never supplies granted_at.
```

`_is_revoked()` then reads "the latest row" with a **non-deterministic sort key**:

```sql
-- jarvis/security/policy.py:226-233
ORDER BY granted_at DESC LIMIT 1
```

`datetime('now')` has **one-second granularity**, so a grant and its revocation issued in
the same second collide, and `LIMIT 1` over tied keys returns the grant row. Measured rows:

```
{'tool': 'delete_file', 'granted': 1, 'granted_at': '2026-09-25 15:22:27', 'revoked_at': None}
{'tool': 'delete_file', 'granted': 0, 'granted_at': '2026-09-25 15:22:27', 'revoked_at': '2026-09-25 15:22:27'}
ORDER BY granted_at DESC LIMIT 1 picks:  granted = 1     <-- revocation invisible
```

The tie is **permanent**: no later clock tick repairs it, because no new row is written.
Checking 1.3 s afterwards still reported `_is_revoked False`.

Isolation confirms the tie is the entire mechanism — forcing the revoke into a later second
makes it work:

```
grant granted_at:  2026-09-25 15:24:11
revoke granted_at: 2026-09-25 15:24:12     (slept 1.2 s between the two calls)
decision -> BLOCK | _is_revoked True        <-- correct
```

So the same call pair produces either a working revocation or a permanently dead one
depending on which side of a second boundary the two statements land on. `is_granted()`
(`policy.py:96-104`) has the identical defect in the opposite direction, so a grant can
also be silently lost.

A second, independent bug sits alongside it: `grant(tool)` with the default
`device=None` writes `device = NULL`, but `is_granted(tool, "laptop_main")` tests
`(device IS 'laptop_main' OR device = 'laptop_main')`, which never matches `NULL`. Measured:
`after grant(no dev): CONFIRM | is_granted -> False`. A device-agnostic grant is inert.

**Smallest safe repair — NOT applied.** Make insertion order authoritative, which is what
the table actually encodes:

```sql
ORDER BY granted_at DESC, rowid DESC   -- in both is_granted() and _is_revoked()
```

and have `revoke()` set `granted_at` explicitly, and make `is_granted`/`_is_revoked`
treat a `NULL` device row as a wildcard (`device IS NULL OR device = ?`).

## 4.8 RT-8 — device scoping is vacuous across the whole catalogue (P2)

`evaluate()`'s second check is `if spec.allowed_devices and action.device not in
spec.allowed_devices: BLOCK` (`policy.py:122-126`). Measured over all 126 specs:

```
allowed_devices distribution: {('laptop_main',): 126}
  control_home_device      allowed_devices=['laptop_main'] risk=1
  command_robot_motion     allowed_devices=['laptop_main'] risk=4
  get_car_status           allowed_devices=['laptop_main'] risk=0
```

**Every one of the 126 tools declares exactly `['laptop_main']`.** The check therefore either
always passes (action on `laptop_main`) or always blocks (any other device) — it encodes no
per-tool boundary at all. Hardware-domain tools are _required_ to be dispatched with
`device="laptop_main"` and reach their real target through a `target_device`/`robot_id`
_parameter_, which the engine never inspects. So the layer the docs describe as device
scoping is a formality; the only real boundary is the parameter-value path.

Not directly exploitable (it fails closed and is arguably intended as "the laptop is the
control plane"), but it means a claimed security control provides zero discrimination.
Severity P2.

## 4.9 RT-9 — `LaptopAgent` re-hardcodes the verified flag the tool layer fixed (P1)

`jarvis/agents/laptop.py:36-40`:

```python
result = await super().execute(action, ctx)
# Light verification: confirm the platform matches what we expect.
if result.ok and not result.verification_note:
    result.verification_note = f"executed on {ctx.platform} ({action.device})"
    result.verified = True          # <-- unconditional
```

`base.py:90-99` documents that `verified` being hardcoded `True` is the original defect —
"which is how an offline robot command with no bridge ended up audited as a success" — and
moves truth to the tri-state `verification`. This agent then sets `verified = True` again
for every tool it owns (17 laptop tools) without touching `verification`, so `verified` and
`verification` can disagree, and the field used by the audit/response layer says
"verified" for an action that was never observed. "Checking the platform matches" is not
verification of an effect. Severity **P1**; this is the same bug class as the historical
incident, reintroduced one layer up.

## 4.10 Severity roll-up for this phase

| ID   | finding                                                                                                                                            | severity                            |
| ---- | -------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------- |
| RT-1 | Simulated/unpaired watch can authorise a real destructive action end-to-end                                                                        | **P0**                              |
| RT-2 | `resolve_confirmation()` never re-runs the engine: revoked permission / offline device still executes                                              | **P1**                              |
| RT-3 | Tool risk tier and `requires_confirmation` are mutable at runtime and trusted by the classifier                                                    | **P1**                              |
| RT-4 | 15 reversible confirm-gated tools become silent under one standing grant; `delete_file`'s reversibility is not tied to `trash_on_delete`           | **P2**                              |
| RT-5 | `ask_watch_confirmation` returns `Verification.SUCCESS` for a `delivered_via="simulated"` prompt nobody saw                                        | **P2** (honesty; cross-ref Phase 5) |
| RT-6 | Path guard, symlink escape, traversal, allowlist, replay, substitution, expiry, ID entropy, scheduler ceiling and scheduler self-approval all held | **PASS**                            |
| RT-7 | `revoke()` fails silently on a `granted_at` second-granularity tie; a device-agnostic grant/revoke never matches                                   | **P1**                              |
| RT-8 | `allowed_devices` is `['laptop_main']` on all 126 tools, so engine-level device scoping discriminates nothing                                      | **P2**                              |
| RT-9 | `LaptopAgent` overwrites `verified = True` unconditionally, defeating the tri-state fix for 17 tools                                               | **P1**                              |

Cross-referenced, independently confirmed in `PHASE9_AGENT_BOUNDARIES.csv`: RT-8, RT-9 and
the `emergency_stop` no-op recorded as **HON-1** in `PHASE9_HONESTY_MATRIX.csv`.

## 4.8 Reproduction

```bash
# harness
rsync -a --exclude .venv --exclude __pycache__ --exclude .pytest_cache \
      --exclude dist --exclude app/build \
      /Users/abhijeetmiskin/AppData/jarv/jarvis-forge/ /tmp/p9shadow/
sed -i '' '16s/import ActionResult/import ActionResult, Verification/' \
    /tmp/p9shadow/jarvis/tools/base.py          # shadow only

cd /tmp/p9shadow
JARVIS_HOME=$(mktemp -d) PYTHONPATH=/tmp/p9shadow \
  /Users/abhijeetmiskin/AppData/jarv/jarvis-forge/.venv/bin/python /tmp/p9_redteam.py
JARVIS_HOME=$(mktemp -d) PYTHONPATH=/tmp/p9shadow \
  /Users/abhijeetmiskin/AppData/jarv/jarvis-forge/.venv/bin/python /tmp/p9_rt2.py
JARVIS_HOME=$(mktemp -d) PYTHONPATH=/tmp/p9shadow \
  /Users/abhijeetmiskin/AppData/jarv/jarvis-forge/.venv/bin/python /tmp/p9_watch2.py
```

Scratch scripts live in `/tmp` (`p9_redteam.py`, `p9_rt2.py`, `p9_watch.py`,
`p9_watch2.py`); probe fixtures were `/tmp/p9_rt_*.txt`, `/tmp/p9_watch_victim*.txt`.
Servers started for these tests were terminated; `lsof -nP -iTCP:8765 -sTCP:LISTEN`
returns nothing.
