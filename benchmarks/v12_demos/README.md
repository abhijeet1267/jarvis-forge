# V12 end-to-end demonstrations

The scripts in this directory are the ten demonstrations from the V12 directive
(§18), each run against a **live backend** with disposable resources. They are
kept because the claims in `docs/PROJECT_STATUS.md` are supposed to be
re-checkable, not remembered.

## Running one

Start a backend on a scratch home so the real database and audit chain are never
touched:

```bash
JARVIS_HOME=/tmp/vantage_probe2 JARVIS_BACKEND_PORT=8798 .venv/bin/python -m uvicorn jarvis.api.app:app --host 127.0.0.1 --port 8798
```

Then, from the repository root:

```bash
.venv/bin/python benchmarks/v12_demos/demo_a.py
```

`demo_b.py` spawns a local `http.server` on port 8899 and kills it mid-run; it
cleans up on exit. `demo_c.py` kills the backend on :8798 and relaunches it, so
run it knowing it will restart your probe backend. `demo_f.py` needs
`PYTHONPATH=.` because it imports the redaction function directly.

## What each one proves

| Script | Demo | The measurement that matters |
|---|---|---|
| `demo_a.py` | A — File Mission | The file is byte-identical after the parked run; the approval is what applies it; the proof claim carries an evidence id |
| `demo_b.py` | B — Self-Healing | A killed service observes `unknown`, not `failed` and not a pass; the goal keeps `verified → unknown → verified` |
| `demo_c.py` | C — Interrupted Mission | After SIGKILL and relaunch the mission is listed and resumed, and `first.txt`'s `st_mtime_ns` is unchanged |
| `demo_e.py` | E — Safe Failure | The out-of-scope step is refused at preview *and* at run; the target file's bytes are unchanged; the denial is in the audit chain |
| `demo_f.py` | F — Privacy Egress | Undeclared hosts are refused; a real body carrying a key is redacted to `[redacted]` |
| `demo_g.py` | G — Autonomy Dial | The same read parks at level 0 and completes at level 1; level 99 is refused |
| `demo_h.py` | H — Temporal Undo | The restore is byte-exact; a hand edit makes the entry `conflicted` and undo returns 409 |
| `demo_i.py` | I — Skill Evolution | Only removal/tightening is proposed; the old versions survive; the new version verifies where the old could not |
| `demo_j.py` | J — Human Takeover | The next dispatch is refused by name; `refused_total` moves; release lets the same step run |

**Demo D (Android)** has no script here: it needs a device or an emulator, and
its physical verification is outstanding rather than approximated. The protocol
half is covered by `tests/test_phase97_phone_mac_workflow.py`,
`tests/test_phone_self_approval_guard.py` and
`tests/test_phase71_phone_authority_hardening.py`.

## What these scripts do not prove

They drive the backend over HTTP. **No physical click is made**, so they are
evidence that the server-side behaviour is real, not that the native control
works when a person presses it. That distinction is maintained throughout
`docs/PROJECT_STATUS.md` and should not be collapsed here.