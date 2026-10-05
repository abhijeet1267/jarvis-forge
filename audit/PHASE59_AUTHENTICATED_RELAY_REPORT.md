# Phase 59 — an authenticated relay: a door with a bouncer, not a second laptop

| | |
|---|---|
| Phase | 59 (the roadmap's authenticated relay, built and verified to the limit of what this machine can prove — see §11) |
| Date | 2026-10-05 |
| Starting commit | `d0f24f8` (Phase 58 close-out) |
| Ending commit | `—` (not written yet; filled in §15 after the commit exists) |
| Tests before | 2305 passed · 0 failed · 0 skipped |
| Tests after | **2332 passed · 0 failed · 0 skipped** (76.9 s) |
| Tools / gated | 145 / 30 → **145 / 30** — no capability added, no gate loosened |
| Planner routes | 52 → **52** |
| Host HTTP surface | 122 route objects / 115 paths → **unchanged** (the relay is a separate ASGI app: 4 endpoints) |
| Push status | **nothing pushed; local commits only** |

---

## 1. What was measured before anything was written

The gap register said "no relay, no push" (VANTAGE_CURRENT_STATE.md §642). Before trusting that,
the tree was searched for anything that already does this job:

```
grep -rl "relay|tunnel|TURN|reverse.proxy|webhook" --include=*.py jarvis
  -> jarvis/core/orchestrator.py   (a comment this phase wrote; nothing relay-shaped was there before)
  -> jarvis/watch/gateway.py       (a push bridge, one-way outbound -- see below)
  -> jarvis/relay.py               (written by this phase)
```

Two things already existed, and both are in the opposite direction from what this phase needed:
`jarvis/transport/` is a real TLS device protocol with HMAC-SHA256 device authentication
(challenge → proof, and a server that picks the destination filename) — but loopback-only, file
transfer only, and deliberately not wired into the orchestrator. `jarvis/watch/gateway.py` has a
`BridgeTransport` that POSTs notifications to a URL from `JARVIS_WATCH_BRIDGE`, and it explicitly
refuses to let anything that transport delivered decide a confirmation. Neither is a path *into*
the laptop from elsewhere. So the honest measurement was: **the wire authentication exists; nothing
outside the laptop is allowed to use it.** The relay reuses the pairing secret from that same store
rather than inventing a second credential system, and it deliberately signs **more** than the
transfer protocol does (§3).

No relay, no tunnel, no cloud endpoint was found. The brief's premise held.

## 2. The trust shape, stated before the code

The dangerous version of this feature is a machine in the internet that holds a key to the laptop.
The version built here has one rule: **the relay is a door, and every decision stays inside.**

```
client ──(PSK handshake: challenge ──> proof over challenge+device+timestamp)──> relay
client ──(X-JARVIS-Token: its own host session)────────────────────────────────> relay
relay  ──(that same header, unchanged; device_id from what signed)─────────────> host
                          PermissionEngine / confirmation / tool / verification / audit
                                    ── all still the host's, all still one ──
```

Four properties are load-bearing, and each has a test rather than a comment:

1. the relay never holds the master token and never holds a session of its own — it carries the
   credential the *client* presented;
2. identity comes from what you can sign, not from what you type — `device_id` in a forwarded body
   is overwritten with the device that passed the handshake, because `CommandBody.device_id` is
   otherwise a string the caller makes up;
3. revocation is checked **per request**, not per session — the device's key is looked up on every
   forward, so `revoke` bites immediately instead of at the end of a TTL;
4. down is down, never empty — an unreachable host answers `502 host_unavailable` or
   `504 host_timeout`, never `[]`.

That fourth one is the bug this project has already fixed twice, for mail and for calendar. A failed
relay that answers an empty Vantage state would be the same lie at a larger scale.

## 3. Authentication, and the one place the existing primitive was not enough

`pairing.verify_proof` is HMAC-SHA256 over the **challenge alone**. That is fine for a proof bound
to one live TLS connection; it is not fine for a relay session, which is a new envelope that can be
captured and resent. So the relay's signed payload adds two things:

```
proof = HMAC-SHA256(secret, "<challenge>\n<device_id>\n<unix_time>")
```

with challenges single-use, TTL 60 s, a ±120 s timestamp window, and a cap of 512 tracked
challenges. No new cryptography — same PSK file, same HMAC, same `hmac.compare_digest`, and the
mint time is handed *to* the client so it signs the relay's clock rather than its own.

A wrong proof **burns the challenge**: `redeem` pops the record before it verifies anything, so an
attacker holding a challenge gets one guess and then has to ask for a new one. The proof over a
stale timestamp is refused the same way. Sessions are opaque `token_urlsafe(24)` values with a
900 s TTL, one sign-out ends one session and not the device's others, and nothing about the session
token can be read back as a secret.

## 4. Forwarding, and two allowlists instead of one

The relay forwards a named list — `/health`, `/command`, `/confirmations`, `/confirm/*`,
`/devices`, `/tools`, `/emergency_stop`, `/voice/status` — and refuses everything else with
`path_not_allowed`. The host's own surface rules (§5) apply on top.

The path is **stripped before it is decided**: `/relay//`, `/relay/` and `/relay` carry no host
path and answer 404 `no_path`, rather than the relay guessing at meaning it was not given. The
forward is one attempt, with a bounded deadline (`DEFAULT_TIMEOUT_S = 12`, httpx timeouts mapped to
`RelayTimeout`/`RelayUnavailable` so a slow socket is reported as a timeout and not retried into
duplicate actuation).

The host's answer comes back **byte-identical**, including its status code — a refusal, a pending
confirmation and a verification of UNKNOWN look exactly the way they do on the laptop because they
*are* the laptop's answers. The relay's only invented vocabulary is the two outage codes.

## 5. Authorization stays the laptop's, and the narrow surface survives the hop

The credential that leaves the relay is the one the client presented, in canonical `X-JARVIS-Token`
casing, and the relay **refuses to forward at all** rather than substituting one of its own
(`401 no_host_credential`). Nothing about Phase 58's scoping is laundered by inserting a hop:
a phone-minted session that goes through the relay is still a phone session, and the *host* is the
one that refuses it — seen live in §8, where `/relay/health` came back as the host's own

> `This phone is not allowed to reach /health. The laptop can; this session cannot.`

while the same path with a dashboard-minted credential answered 200. Two independent doors,
different owners, both shut.

## 6. Audit identity: the defect this phase found in its own running code

The relay was run for real, and the ledger was read back. A command that arrived through the relay
from a handset was written **identically to one typed on the laptop**: every executed action records
`device=action.device`, which is the *target* device Phase 31 made deliberate. Nothing had been
faked. Nothing was traceable either.

The fix is additive, because repurposing the device column would corrupt device targeting. The
orchestrator now writes who asked next to what was asked to change
(`jarvis/core/orchestrator.py:406`): `detail["requested_by"] = request.device_id`, plus
`detail["surface"]` when the request carries one. The test asserts all three facts at once —
`requested_by`, `surface`, and a `device` column that has **not** moved.

## 7. The tests

`tests/test_phase59_the_relay_is_a_door_not_a_second_laptop.py`, 27 tests, written red-first against
a stub sender and an injectable clock, each pairing its own uniquely-named device so one test's
revocation cannot break another's session:

- **authentication** — a paired device opens a session; an unknown device gets no challenge; a
  *pending* device is not authorised; a wrong proof is rejected and still burns the challenge; a
  challenge cannot be replayed; a stale timestamp is rejected; a session expires; a missing or
  forged session never reaches the host; sign-out ends one session and not the other; revoking a
  device ends its sessions **and** blocks new ones.
- **forwarding** — URL, method, body and credential forwarded verbatim; the host's answer returned
  unchanged at 200/403/409/500; the relay never supplies a credential of its own; the body's
  `device_id` is the one that signed; a GET carries no body; refused paths; an empty path is not
  guessed at.
- **outages** — unreachable host is `502 host_unavailable` and not an empty result; a timeout is
  `504 host_timeout`; one request is one attempt; every forward carries a bounded deadline.
- **leaks** — the master token appears nowhere in the relay's conversation; a session token carries
  no secret; `describe()` gives an operator counts and no credentials; no `/config`, `/debug`,
  `/env`, `/token` route; the relay answers only for the endpoints it defines.
- **provenance** — the audit test in §6, which runs the real orchestrator.

Two of these were written *after* a live run corrected me. `FastAPI` was imported inside
`build_relay_app`, so `request: Request` was resolved against the function's locals and every
proxied call answered **422** — the fix was a module-level import with a comment saying why. And
listing the running relay's routes found `/openapi.json` being served: the surface test exists now,
and the live process answers 404 (§8).

## 8. Live, on an isolated home and two loopback ports

`JARVIS_HOME=$(mktemp -d)`, host on 127.0.0.1:8916, relay on 127.0.0.1:8917, the real served
`/phone` bootstrap, the real pairing key file, the real SQLite ledger. One consolidated run, then a
follow-up for the two checks the first script got wrong (a dict passed where the helper expected a
token, and a label that asserted the opposite of §3). Quoted output:

```
approval returned a pre-shared key?                        -> True  state=paired
replay the redeemed challenge                              -> 401 {"relay_error":"authentication_failed","detail":"unknown or already spent"}
forward with no relay session                              -> 401 {"relay_error": "session_invalid", ...}
forward with a forged relay session                        -> 401 {"relay_error": "session_invalid", ...}
relay POST /relay/command (body claims laptop_main)        -> 200 {"request_id":"REQ-2a6bbd553f7e", ..."reply":"It is 12:52 on Monday, 05 October 2026."}
host reply carried through unchanged?                      -> True
ledger device column (target)                              -> 'laptop_main'
ledger detail.requested_by (who asked)                     -> 'p59_demo'
ledger detail.surface                                      -> 'relay'
credential text anywhere in the row?                       -> False
relay GET /relay/config (relay refuses)                    -> 403 {"relay_error": "path_not_allowed", ...}
relay GET /relay/audit   (relay refuses)                   -> 403 {"relay_error": "path_not_allowed", ...}
relay GET /relay/health  (host refuses, phone surface)     -> 403 This phone is not allowed to reach /health. The laptop can; this session cannot.
same path with a dashboard-surface credential              -> 200 {"status":"ok","tools":145, ...}
proof over (now - 600)                                     -> 401 timestamp outside the allowed window
the same challenge, with a fresh in-window proof           -> 401 unknown or already spent
bad proof                                                  -> 401 proof did not verify
right proof, same challenge, afterwards                    -> 401 unknown or already spent
a brand-new challenge                                      -> 200 {"relay_session":"…","expires_in":900}
before revocation: relay POST /relay/command               -> 200 …"It is 12:54 on Monday, 05 October 2026."
POST /devices/p59_demo/revoke                              -> 200 {"state":"revoked","reason":"user revoked"}
after revocation: the same live token, same request        -> 401 session_invalid
after revocation: a new challenge for that device          -> 403 device_not_paired
key file removed from the host data dir?                   -> True
relay GET /relay/health, host stopped                      -> 502 {"relay_error": "host_unavailable", "detail": "the laptop at http://127.0.0.1:8916/health is not answering: [Errno 61] Connection refused"}
relay POST /relay/command, host stopped                    -> 502 {"relay_error": "host_unavailable", ...}
relay GET /relay/describe, no session                      -> 401 session_invalid
relay GET /openapi.json · /docs · /redoc                   -> 404 · 404 · 404
```

The outage check used a **live** session with only the host gone, which is the case that matters;
an earlier attempt at this phase reused a session whose device had already been revoked and so
proved nothing — that result was thrown out rather than quoted.

Every hop was loopback. The isolated home was deleted afterwards; the real `~/.jarvis-forge` was
not written to (`ls ~/.jarvis-forge/devices | grep p59 | wc -l` → 0), and both processes are
stopped.

## 9. Timeout discipline

The relay's forward deadline is its own (12 s default, passed to the sender on every call and
asserted by a test). It does not inherit or extend the laptop's `SENTENCE_BUDGET`: a command that
would time out at the host still times out at the host, and the relay gives up on the socket, not
on the truth. `jarvis/net.py` was not modified.

## 10. What this component is not allowed to become

`describe()` returns `{host_base, live_sessions, allowed_paths, timeout, session_ttl, surface}` —
counts, never tokens. There is no route that lists sessions, no debug surface, no config dump, and
no OpenAPI document. The relay holds no master token, so there is nothing to lose from its process
or its memory.

## 11. OFF_LAN_REACHABILITY: BLOCKED_EXTERNAL_DEPENDENCY

Everything in §7 and §8 is `LOCAL RELAY PROTOCOL PROOF`. It shows that a component which
authenticates a device, carries the client's own credential, overwrites typed identity, refuses
paths off its list, and reports an outage as an outage **works**. It does not show that Vantage is
reachable from outside the wifi, and no amount of loopback evidence does.

```
OFF_LAN_REACHABILITY: BLOCKED_EXTERNAL_DEPENDENCY
missing: a relay host that is itself reachable from outside this network — a public address
         (VPS/cloud instance with a routable IP), its own TLS certificate trusted by the
         handset, and an outbound path from this laptop to it.
measured here: no relay host for this direction is configured anywhere; `JARVIS_RELAY_*` settings do
         not exist. The only outbound relay URL in the project is `JARVIS_WATCH_BRIDGE`, which posts
         notifications and is refused as evidence for a confirmation (jarvis/watch/gateway.py:134);
         this laptop has no control over the network it is standing on.
not performed: router configuration, port forwarding, tunneling to a third-party provider, and
         publishing this laptop or its data directory to any address. Those are the user's
         network and the user's machine, and each one widens blast radius.
```

A local test presented as off-LAN proof is the exact failure mode this phase was briefed to avoid,
so the claim stops here. What is *ready* for that proof: the protocol, the service, the running
process (`python -m jarvis.relay --host <base> --port <n>`), and the test suite. When a real relay
host exists, the same checks run against it and §11 changes from BLOCKED to a measurement.

**One design decision that off-LAN use forces, recorded rather than decided here:** as built, the
relay authenticates devices against the *host's* `<home>/devices` key store, so the process must
share that directory with the laptop — true in every test and in the live run, and false for a
relay sitting in another country. Remote deployment needs a different device-authentication shape
(the relay holding a verifier that cannot be replayed against the host, or the host answering an
authentication query per session). That is a user/infrastructure decision with a security cost, not
a detail to tidy away — see 59-A.

## 12. Browser verification: NOT APPLICABLE

No page changed. `git status` for this phase is `jarvis/relay.py` (new),
`tests/test_phase59_...py` (new) and 8 added lines in `jarvis/core/orchestrator.py`. No dashboard or
phone file was touched, and the relay has no client UI wired to it (§13, 59-B). Browser verification
would have been a screenshot of an unchanged page, so it was not performed and is not claimed.

## 13. Numbers, re-measured after the work

| | before (Phase 58 close) | after (measured now) |
|---|---|---|
| Tests | 2305 · 0 failed · 0 skipped | **2332 · 0 failed · 0 skipped** (76.9 s) |
| Test modules | 85 | **86** |
| Tools / confirmation-gated | 145 / 30 | **145 / 30** |
| Risk split (read/low/external/destructive/physical) | 77 / 38 / 15 / 12 / 3 | **77 / 38 / 15 / 12 / 3** |
| Planner rules | 52 | **52** |
| Host HTTP routes / paths | 122 / 115 | **122 / 115** |
| Relay HTTP endpoints | none | **4** (`/relay/challenge`, `/relay/session` POST, `/relay/session` DELETE, `/relay/{path}` forward) |
| Versioned Python/HTML/JS/Swift | 70.9k | **72,227** lines |
| Shipped code (`jarvis/` + `dashboard/` + `app/` + `assets/` + `run.py`) | 42.2k | **42,937** lines |
| Tests | 28.7k | **28,689** lines |
| `audit/` reports | 60 | **61** (this file) |
| New file | — | `jarvis/relay.py`, 462 lines |

## 14. Open items

- **59-A — a remote relay needs a different device-authentication shape.** §11. The local proof is
  complete; the deployment assumption inside it is not.
- **59-B — no client is wired to the relay.** `dashboard/phone.html` still talks to the host
  directly and only works on the same wifi. Pointing a client at a relay is a UI change plus a
  credential-storage question (a phone that remembers a relay address remembers a token), and it is
  worth doing only once a real relay host exists.
- **59-C — relay sessions are in-memory.** Restarting the relay signs everyone out. That is the
  safer default and it is honest, but it is also why the tests inject a clock: no persistence layer
  was added, and none was claimed.
- **59-D — the transfer protocol still signs the challenge alone** (`pairing.verify_proof`,
  `jarvis/transport/protocol.py`). It is loopback, bound to one live TLS connection, and out of this
  phase's scope. Same key, two different bindings, now documented — if the transfer protocol ever
  crosses a network, it needs what the relay already has.
- **59-E — `GET /devices` publishes `key_ref`,** a filesystem path to the PSK file, to a
  master-token caller. Not the secret; still more about the machine than a device list needs to say.
  Not changed here because the panel may be reading it.

## 15. Delivery

Commit: `—` — recorded below once it exists, with the reason it was not written in advance: an
earlier phase put a hash in a report before the commit did, and that is a fabrication with a good
intention behind it.

Delivered files: the app bundle in `~/Downloads/Vantage/` and both archives are rebuilt from this
tree at close-out, so `jarvis/relay.py` ships inside the bundle's source copy. The relay is not
started by the app, the login item, or any client — it is a separate process an operator runs on
purpose, which is the point of §2.

Nothing was pushed.
