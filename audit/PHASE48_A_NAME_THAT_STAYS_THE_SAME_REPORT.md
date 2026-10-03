# Phase 48 — A name that stays the same

Closed 2026-10-04. Closes **38-B / 39-E / 40-A** — the item the delivery README had been
telling the user to *live with* for thirteen phases:

> *"The address is your laptop's wifi address, and the router hands that out again whenever the
> lease renews. It changed once today (…169.34 became …169.185) without anything being touched."*

---

## 1. What was measured before anything was written

| Question | Measurement |
|---|---|
| Does the machine have a stable name? | `scutil --get LocalHostName` → `Abhijeets-MacBook-Air-2`; `hostname` → `Abhijeets-MacBook-Air-2.local` |
| Is mDNS alive? | `mDNSResponder` pid 450 |
| **Is the name published on the wifi interface?** | `dns-sd -G v4 <name>.local` → `Add … IF 1 … 127.0.0.1` **and `Add … IF 12 … 172.20.169.185`** — an A record on the wifi interface, TTL 4500 |
| Does the app accept it? | `Host: Abhijeets-MacBook-Air-2.local:8765` → **400**, in LAN mode, while `Host: 172.20.169.185` → 200 |

The name existed, was advertised on the LAN, and was refused by the one thing that decides
whether a request is allowed in. So the gap was never "there is no stable address"; it was this
gate, and the reason in the code was a good one:

```python
# LAN mode: accept exactly the addresses this machine owns. A hostname is never accepted
# here -- that is the DNS-rebinding case the loopback rule was written for.
```

## 2. The narrowing, and why the old rule was over-broad rather than wrong

Accepting a hostname is dangerous: an attacker's page can resolve `evil.example.com` to
`127.0.0.1` (or to a victim laptop's LAN address) and the browser will send
`Host: evil.example.com` from inside the network. Public DNS will answer anything, so a public
name proves nothing about who owns it.

`.local` is different: **RFC 6762 reserves it for multicast DNS and public DNS never answers for
it.** No one can register `evil.local` and point it at your laptop. So the rule became two
conditions, not one:

```python
return stripped in interface_addresses() or mdns_name_is_ours(stripped)
```

`mdns_name_is_ours()` requires the `.local` suffix **and** that the name resolves to an address
this machine already owns — the second condition matters because every other laptop on the same
wifi has a `.local` name too. Answers are cached per name for 60 s: this runs on every request,
and a resolver call per request is a slow path any device on the network can drive. A "no"
expires, because a lease renewal and a repaired network both change the answer.

Refused, and tested as refused: `evil.example.com`, `attacker.local.test`, a `.local` name that
resolves to `10.99.1.7`, a name with no answer at all, and **every name while LAN mode is off**
— the private posture is unchanged.

## 3. The bug the browser found after the gate was fixed

The unit tests went green, the isolated instance answered `200` to a name-addressed page, and
then the real browser said the feature did not work:

```
POST /auth/session  -> 403 Origin not allowed.
GET  /devices, /tools, /confirmations, /voice/status  -> 401 (40 more times, every poll)
```

The panel printed `Abhijeets-MacBook-Air-2.local`. Chrome lowered it to
`abhijeets-macbook-air-2.local` before sending `Origin`, and the gate compared strings:

```python
elif origin not in settings.allowed_origins():
```

So the page loaded, the bootstrap code was spent on a 403, and every call after it was
unauthorised — a laptop-shaped brick. Two fixes, because both halves were wrong:

* the name is **advertised in the form a browser will send** (lowercased), and
* the origin comparison **ignores host case**, because the host part of a URL is
  case-insensitive by definition and a person typing the mixed-case name should not be refused.

This is the pattern the whole program keeps hitting: a claim verified by a request is not a
claim verified in the client the feature is for.

## 4. What the panel shows now

`lan_phone_url()` preferred the first non-loopback entry, which was always the **address** — so
the QR encoded the thing that dies with the lease. It now prefers the name, and a second line
prints the address as well:

```python
phone: http://abhijeets-macbook-air-2.local:8765/phone
   or, if that name does not resolve: http://172.20.169.185:8765/phone
```

The fallback is not decoration: Android's Chrome does not reliably resolve mDNS, so a panel that
showed only the name would fix the iPhone and break the Android. When there is no name, the
preference order is exactly what it was.

## 5. Live, on the delivered app

Rebuilt, re-signed (`valid on disk`, `satisfies its Designated Requirement`), relaunched:

```
GET  http://abhijeets-macbook-air-2.local:8765/phone            -> 200
     advertised phone_urls: 127.0.0.1 · 172.20.169.185 · abhijeets-macbook-air-2.local

Host: Abhijeets-MacBook-Air-2.local:8802  -> 200     (isolated LAN instance; was 400)
Host: evil.example.com:8802               -> 400     (the rebinding door stays shut)
Host: 127.0.0.1:8802                      -> 200

Phone page in a real browser at 375 px over the name:
  /auth/session 200, six quick actions rendered, tap Storage ->
      "Storage: 18.1 GiB free of 228.3 GiB on / (40% used, /dev/disk3s3s1)"  [check_storage]
  0 console errors
  mic disabled, reading "the laptop can hear, but this browser will not hand the page a
  microphone on an insecure address" -- correct: `isSecureContext` is false on this origin,
  and that is now a different sentence from "the laptop has no ears", which is no longer true.
```

## 6.5 The lease changed while this phase was running

Not a hypothetical. Between the first live proof and the last, the wifi address moved:

```
before   interface_addresses(): 127.0.0.1 · 172.20.169.185 · ::1 · fe80::…
         advertised: http://172.20.169.185:8765/phone
after    interface_addresses(): 127.0.0.1 · 2409:40f4:…:443e · ::1 · fe80::…   (no IPv4 lease)
         advertised: http://[2409:40f4:40db:f28c:1088:d8ff:3467:443e]:8765/phone
                    http://abhijeets-macbook-air-2.local:8765/phone      ← unchanged
```

The number in the panel is now a different address, and the name is the same string it has
always been. That is the whole argument of this phase, demonstrated by the network rather than
by a test.

It also broke seven of this phase's own tests, which had hard-coded `172.20.169.185` as "an
address this machine owns". They passed alone and failed in a full run, then failed alone too --
not a leak, a lease. The file now injects both halves of that premise (`OURS` / `THEIRS` and a
patched `interface_addresses()`), so it passes on a laptop with no network at all. A test that
depends on which address a router handed out this minute is the same category of mistake as a
bookmark that does.

And it bounds what the name can promise: with no IPv4 on the interface, `dns-sd` now answers
`No Such Record` on the wifi interface and only `127.0.0.1` overall. The name is still
advertised and still accepted -- it resolves to an address the machine owns -- but a phone could
not reach this laptop by name *or* number in that state. The name survives a lease renewal; it
does not manufacture a lease. See 48-F.

## 7. Numbers, re-measured

| | before | after | how |
|---|---|---|---|
| Test suite | 1896 passed · 0 failed | **1915 passed · 0 failed · 0 skipped** (88.3 s) | `JARVIS_HOME=$(mktemp -d) .venv/bin/python -m pytest -q` |
| Phase 48 tests | 0 | **19** | `tests/test_phase48_a_name_that_stays_the_same.py` |
| Test modules | 74 | **75** | `ls tests/test_*.py | wc -l` |
| Phase reports | 49 | **50** (this file) | `ls audit/*.md | wc -l` |
| Lines | 63.1k | **63,472** — 39,003 source · 24,469 tests | `wc -l` |
| Routes | 112 (106 paths) | 112 (106 paths) — unchanged | `len(app.routes)` |
| Name-addressed request | 400 | **200**, page fully usable | live, above |

## 8. Open items

* **48-A — cross-device resolution is inferred, not observed.** The A record is published on
  the wifi interface (`dns-sd` above), which is the mechanism a phone uses, but there is no
  second device in this test environment to type the name. The claim "your phone can now find
  the laptop by name after a lease renewal" rests on the record being advertised, not on a
  phone having done it. One tap on the user's phone closes it.
* **48-B — Android may not resolve `.local` at all**, which is why the address is still printed.
  On such a handset the original problem remains: the number changes. The real fixes are mDNS on
  the router, a DHCP reservation, or the self-signed-https path — none of which this phase did.
* **48-C — the name is only advertised while it resolves to this machine.** On networks where the
  responder is blocked (some guest/isolated-wifi APs), the panel silently falls back to the
  address. Correct behaviour, but the user is not told *why* the name disappeared.
* **48-D — the 60 s name cache means a lease change is visible up to a minute late**, and the
  cache is per process: a restart re-asks. Both are deliberate, neither is measured under a real
  renewal.
* **48-E — `/auth/session`'s 403 was found by hand in a browser, and the suite only learned about
  it afterwards.** The general shape — "the page loads and every API call fails" — has no guard
  that would catch the next variant (a wrong origin list, a stripped header) without a real
  browser in the loop.

## 9. What changed for the reader

The laptop now has one address that does not move: `http://abhijeets-macbook-air-2.local:8765/phone`.
The panel prints it first and the wifi number underneath, and the gate that spent thirteen
phases refusing hostnames refuses them still — for everything except the name this machine can
prove is its own.
