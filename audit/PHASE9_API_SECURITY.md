# PHASE 9 — PHASE 9: API / Network Security Audit

Method: a real server was started from a clean copy of the source (the P0 import defect
repaired locally, so handlers execute instead of crashing) on
`127.0.0.1:8765`, with a throwaway `JARVIS_HOME` and an explicit
`JARVIS_API_TOKEN`. Every result below is a literal HTTP status and response body.
**The token value is never reproduced in this report beyond the fixture string used to
drive the probes; no real credential is recorded.** The server was terminated afterwards
(`lsof -nP -iTCP:8765 -sTCP:LISTEN` → empty).

Configuration in force, read back from `/health`:

```json
"security": { "auth_enabled": true, "token_configured": true,
              "allowed_origins": ["http://127.0.0.1:8765"],
              "host_header_enforced": true,
              "rate_limit_per_minute": 600, "rate_limit_burst": 120,
              "confirm_timeout_seconds": 300 }
```

## 9.1 Authentication and authorization — engine PASSes, but see **API-0**

| probe                                                   | expectation | observed                                         | verdict  |
| ------------------------------------------------------- | ----------- | ------------------------------------------------ | -------- |
| `GET /health` no token                                  | 401         | `401 {"detail":"Missing or invalid API token."}` | **PASS** |
| `GET /health` wrong token                               | 401         | `401 {"detail":"Missing or invalid API token."}` | **PASS** |
| `GET /health` `X-JARVIS-Token`                          | 200         | `200`                                            | **PASS** |
| `GET /health` `Authorization: Bearer …`                 | 200         | `200`                                            | **PASS** |
| `POST /confirm/{id}` unauthenticated                    | 401         | `401`                                            | **PASS** |
| `POST /command` unauthenticated                         | 401         | `401`                                            | **PASS** |
| `POST /privacy/delete` (`confirm:true`) unauthenticated | 401         | `401`                                            | **PASS** |

Every destructive route sits behind the same `Protected` dependency
(`jarvis/api/app.py:357-366` for `/confirm/{confirmation_id}`). Token comparison is
constant-time (`hmac.compare_digest`, `jarvis/security/auth.py:103-106`).

**These PASS results do not mean the API is authenticated.** They mean the routes check a
token — and **API-0 below shows the token is published to anyone who asks for it without
one.** Treat 9.1 as conditional on 9.1bis.

## 9.1bis API-0 — `GET /auth/token` discloses the master credential unauthenticated (**P0**)

`jarvis/api/app.py:326-339` has **no `Protected` dependency**. It returns the live token.
Its only gate is an `Origin` check that is skipped when the header is absent:

```python
@app.get("/auth/token")
async def auth_token(request: Request) -> Dict[str, Any]:
    """Hand the token to a same-origin client (the dashboard).

    A foreign origin cannot read this response because CORS only allows our own
    origin, and browsers do not let a page read a cross-origin reply.
    """
    settings = get_settings()
    origin = request.headers.get("origin")
    if origin and origin not in settings.allowed_origins():   # <-- absent Origin => no check
        raise HTTPException(status_code=403, detail="Origin not allowed.")
    ...
    return {"auth_enabled": True, "token": get_token_store().token()}
```

Measured against a live server configured with a known fixture token, server freshly
started with `JARVIS_HOME` under `/tmp`:

| probe                                        | result                                                                              |
| -------------------------------------------- | ----------------------------------------------------------------------------------- |
| `GET /auth/token`, **no `Origin`, no token** | `HTTP 200` → `{"auth_enabled":true,"token":"<the live token>"}`                     |
| same with `Host: localhost:8765`             | `HTTP 200` → live token                                                             |
| with `Origin: http://evil.test`              | `HTTP 403 {"detail":"Origin not allowed."}`                                         |
| with `Origin: http://127.0.0.1:8765`         | `HTTP 200` → live token                                                             |
| leaked token compared to configured token    | **identical, length 31, `matches: YES`**                                            |
| `POST /command` using only the leaked token  | **`HTTP 200`**, real answer returned: `"It is 21:00 on Friday, 25 September 2026."` |

The token is **not reproduced in this report**; the fixture value was invented by the audit.
The server was terminated afterwards.

### Second, wider disclosure path: `/dashboard`

The same secret is also embedded in the **unauthenticated** dashboard HTML, so `/auth/token`
is not the only leak. Plain `curl`, no `Origin`, no token, measured:

| route               | status  | bytes  | occurrences of the live token in body |
| ------------------- | ------- | ------ | ------------------------------------- |
| `GET /auth/token`   | **200** | 41     | **1**                                 |
| `GET /dashboard`    | **200** | 22,775 | **1**                                 |
| `GET /openapi.json` | **200** | 71,047 | 0 — but see below                     |
| `GET /docs`         | **200** | 1,016  | 0                                     |
| `GET /redoc`        | **200** | 898    | 0                                     |

`/dashboard` has no `Origin` guard at all, because it is written to be opened by direct
navigation — so the bootstrap token is handed to any local process that asks for the page.
The `dashboard_is_public_but_injects_the_token` test asserts this as intended behaviour;
the audit records that the intended behaviour discloses the master credential.

`GET /openapi.json` returns **200 unauthenticated** and publishes the complete API contract —
96 paths, including `/command`, `/confirm/{confirmation_id}`, `/privacy/delete`, `/memory`,
`/memory/export`, `/memory/overview`, `/voice/command` and `/command/stream`, with their
parameter schemas. An unauthenticated local process therefore learns both the full attack
surface _and_ the credential to use it, in two requests.

This resolves the earlier "is `/docs` mounted?" open question in §9.10: **yes, and
unauthenticated** — API-6 is closed as FALSE-by-measurement rather than UNVERIFIED.

### Why the stated defence does not hold

The docstring's argument is a _browser_ argument, and it is correct for browsers: CORS is
default-deny (9.3) and the `Host` allowlist stops DNS rebinding, so a foreign page cannot
read the response. But the threat model for a bearer token is not "a foreign webpage" — it
is **any other process on the machine**, and:

- `curl`, `wget`, python, node, a native-messaging peer, or any local binary sends **no
  `Origin` header**, so the `if origin and ...` test short-circuits and the token is handed
  over at `HTTP 200`.
- The server binds `127.0.0.1` and every loopback `Host` spelling is allowed
  (`localhost:8765`, `127.0.0.1:8765`, `[::1]:8765`), so the reachability bar is "can open a
  TCP socket to loopback".
- No rate limiting applied to this route in the probe run, so it is not even slow to hit.

This makes the 0600 protection on `$JARVIS_HOME/api_token`
(`jarvis/security/auth.py:89-100`, deliberately `O_CREAT, 0o600` + `os.chmod` so the secret
"is never briefly world-readable") moot: the same secret is served over unauthenticated
HTTP. It also inverts the design claim in the module docstring —
_"Token auth on every endpoint"_ — into: every endpoint is protected by a secret that one
endpoint gives away.

### Blast radius

`POST /command` is the whole pipeline. Holding the token means a local process can drive
every one of the 126 tools — including the 24 confirm-gated ones, because with the token it
can also call `POST /confirm/{id}` and approve its own confirmations. It is the difference
between "another app on this Mac can read JARVIS's memory" and "another app on this Mac can
delete files, send email, command the robot and control the home".

### Smallest safe repair — NOT applied (Phase 15)

The dashboard needs the token to bootstrap, so the fix must keep that path. Two options,
both small:

1. **Bind disclosure to the session that asked for it.** Serve `/dashboard` with a
   single-use, short-lived bootstrap secret in the HTML (as it effectively does today via
   same-origin), and make `/auth/token` itself `Protected`. The dashboard already runs
   same-origin, so it does not need an anonymous token route.
2. If 1 breaks the dashboard bootstrap: make the `Origin` check **mandatory** rather than
   conditional — a request with no `Origin` header is not a browser and must be refused:

```python
origin = request.headers.get("origin")
if origin is None or origin not in settings.allowed_origins():
    raise HTTPException(status_code=403, detail="Same-origin browser request required.")
```

Option 2 is a two-word change and closes the measured hole; it leaves the dashboard working
(browsers always send `Origin` on same-origin `fetch` in current engines — **verify this in
the target browsers before shipping**, since an older WebKit may omit it and break the
dashboard). Recommend 1 as the correct fix, 2 as the stopgap. Not implemented; authorisation
required.

## 9.1ter Routes without an authentication dependency

Enumerated from the decorator/signature pairs in `jarvis/api/app.py`: `/` (name + version),
`/dashboard` (HTML, embeds the token), `/auth/token` (**the P0 above**), plus FastAPI's
static assets and any mounted docs. Every other route examined carries `Protected`.

## 9.2 Host-header validation and DNS rebinding — PASS, one soft edge

| `Host:` header     | expected    | observed                                                   |
| ------------------ | ----------- | ---------------------------------------------------------- |
| `evil.example.com` | refuse      | `400 {"detail":"Host header must be a loopback address."}` |
| `10.0.0.5:8765`    | refuse      | `400` same                                                 |
| `localhost:8765`   | accept      | `401` (host accepted, auth then applied)                   |
| `[::1]:8765`       | accept      | `401`                                                      |
| `0.0.0.0:8765`     | **refuse?** | **`401` — accepted**                                       |

Rebinding defence is real and enforced before authentication, so a page that resolves a
hostile name to `127.0.0.1` is stopped by the `Host` check. The allowlist is
`jarvis/security/auth.py:33-41`.

**Finding (P3):** the allowlist literally contains `"0.0.0.0"` with the comment
`# only ever a bind address, but harmless to accept`. A `Host: 0.0.0.0:8765` request is
therefore not rejected. It is still unauthenticated, so this is not an escape — but the
entry exists purely as a convenience for the server's own bind string, and the correct
place for that is the `Host` of a loopback interface, not a wildcard address literal.
Recommend removing it; no change made.

## 9.3 CORS — PASS

| probe                                         | observed                                                                    |
| --------------------------------------------- | --------------------------------------------------------------------------- |
| `GET /health` with `Origin: http://evil.test` | `401`, **no `access-control-allow-origin` header on any response examined** |
| header inventory across 12 responses          | `access-control-allow-origin` present on **0 of 12**                        |

`cors_origins` defaults to an empty list (`jarvis/config.py:53`), so no cross-origin grant
is emitted. `settings.cors_origins` was reported by `/health` as exactly
`["http://127.0.0.1:8765"]` — the app's own origin. A browser cannot read responses
cross-origin. **PASS.**

## 9.4 Response headers — PASS with one gap

Observed on authenticated JSON responses:

```
x-content-type-options: nosniff
x-frame-options: DENY
server: uvicorn
```

Missing: `content-security-policy`, `referrer-policy`, `strict-transport-security`.
`/dashboard` serves HTML plus inline `<script>`; with `x-frame-options: DENY` and
loopback-only `Host` enforcement the clickjacking and MITM surface is small, but a
`script-src 'self'` CSP would be cheap defence-in-depth. **P4.**

Note the `deny` headers are absent on the `400` Host-rejection path (the middleware
short-circuits before the header layer), so a rejection response is `server: uvicorn` only.
Cosmetic. **P4.**

## 9.5 Input robustness — mostly PASS, one real gap

| probe                                 | expected                  | observed                                                                                                 | verdict         |
| ------------------------------------- | ------------------------- | -------------------------------------------------------------------------------------------------------- | --------------- |
| truncated JSON `{"text": "hi"`        | 4xx structured            | `422 [{"type":"json_invalid","loc":["body",13],"msg":"JSON decode error"}]`                              | **PASS**        |
| empty body                            | 422                       | `422 [{"type":"missing","loc":["body"],"msg":"Field required"}]`                                         | **PASS**        |
| `{"text":""}`                         | 422 with reason           | `422 [{"type":"value_error","loc":["body","text"],"msg":"Value error, command text must not be empty"}]` | **PASS**        |
| 10 KB / 200 KB body                   | handled                   | handled (rate-limited in that run)                                                                       | —               |
| **2 MB body**                         | reject cleanly with `413` | **`200`, processed in 4071 ms**                                                                          | **FAIL — P2**   |
| 2 MB body (repeat, saturated limiter) | —                         | connection closed: `[Errno 32] Broken pipe`                                                              | inconsistent    |
| 20 MB body                            | `413`                     | `[Errno 32] Broken pipe`                                                                                 | no clean status |

**Finding:** there is **no request body size limit**. A 2 MB `/command` payload was
accepted and pushed through the full planner pipeline, costing **4.07 s** of server time for
a string the planner could not use ("I understood the words but not the goal yet"). Larger
bodies are torn down by the ASGI/uvicorn layer rather than by the app, producing a broken
pipe instead of a `413`, so the behaviour is size-dependent and not a contract.

Impact is bounded — the server binds loopback and requires a token — but a compromised or
misbehaving local process can spend unbounded CPU per request, and the limiter's cost model
counts requests, not bytes. Recommend a middleware cap (e.g. `Content-Length` and streamed
body ceiling → `413`). **P2.** No change made.

## 9.6 Rate limiting — PASS

150 sequential `GET /health` requests issued as fast as the client could make them:

```json
{ "200": 118, "429": 32 } // elapsed 0.2 s
```

The bucket admits a burst of 120 (configured `rate_limit_burst = 120`) then returns
`429 {"detail":"Too many requests. Slow down."}`. Bounded, enforced per client address, and
the limiter is reset between tests by `tests/conftest.py:36-46` so the suite does not mask
it. **PASS.**

One consequence to record: because the limiter is keyed per client address and the burst is
120, a local process can lock the dashboard out of `/health`-adjacent endpoints for up to a
minute. Availability nuisance, not a security hole. **P4.**

## 9.7 Token lifecycle and storage — PASS

`TokenStore.token()` precedence (`jarvis/security/auth.py:59-78`): explicitly configured
`api_token` → token file in `JARVIS_HOME` → generate `secrets.token_urlsafe(32)` and persist.
Persistence uses `os.open(..., 0o600)` then `os.chmod(0o600)` so the secret is never
briefly world-readable (`auth.py:89-100`). `/health` reported the token file as
`$JARVIS_HOME/api_token`, `token_configured: true`.

Redaction of the token from logs and audit records is covered in Phase 7 (privacy/export);
`tests/test_privacy.py::test_audit_never_persists_the_configured_token` exists for it and
passes in the fixed copy.

**No credential or token value from the user's environment appears in this report.** The
fixture token `audit-token-12345` is a string this audit invented, in a throwaway home now
deletable.

## 9.8 Endpoint inventory and exposure

| endpoint                                                                | status      | observed                                                                                                               |
| ----------------------------------------------------------------------- | ----------- | ---------------------------------------------------------------------------------------------------------------------- |
| `GET /`                                                                 | 200         | `{"name":"JARVIS Forge Core","version":"0.1.0","tools":126}` — **public metadata, no auth needed to read the version** |
| `GET /health`                                                           | 200         | full security/privacy/device posture — authenticated                                                                   |
| `GET /tools`                                                            | 200         | 126 tools with `risk`, `risk_label`, `requires_confirmation`, `reversible`, `parameters` — authenticated               |
| `GET /devices`                                                          | 200         | 5 devices with capabilities and `auth_state`                                                                           |
| `GET /audit`                                                            | 200         | hashed chain records                                                                                                   |
| `GET /metrics`                                                          | 200         | `{"failures":{"total_failures":0,...},"audit":{"sampled":2,...}}`                                                      |
| `GET /failures`                                                         | 200         | `[]`                                                                                                                   |
| `GET /confirmations`                                                    | 200         | `[]`                                                                                                                   |
| `GET /voice/status`                                                     | 200         | mic permission state, devices, TTS/STT backends                                                                        |
| `GET /watch/status`                                                     | 200         | `mode: simulate`, `paired: false`, `online: false`, outbox counts                                                      |
| `GET /docs`, `/redoc`, `/openapi.json`                                  | **404/429** | not reachable in these probes once the limiter engaged; **UNVERIFIED whether they are mounted** — see 9.10             |
| `GET /home/status`, `/robotics/status`, `/car/status`, `/vision/status` | **404**     | these routes do not exist; hardware state is only reachable via tools and `/health` (see Phase 11)                     |

`GET /tools` disclosing the full risk/confirmation matrix to any token holder is fine for a
local control plane, and is what the dashboard renders.

## 9.9 Streaming and confirmation round-trip — PASS on the API contract

`POST /command {"text":"delete the file /tmp/p9_api_victim.txt"}` returned:

```json
{
  "status": 200,
  "needs_confirmation": true,
  "decision": "CONFIRM",
  "confirmation_id": "CONF-4dfbe7d6b63192f844f172306fc3b12f",
  "reply": "This needs your confirmation before I run it.\n  Action: delete_file\n  Device: laptop_main\n  Details: path=…"
}
```

Destructive intent over HTTP is refused without a confirmation, and the confirmation id is
the 128-bit secret form (`CONF-` + 32 hex chars, `jarvis/core/models.py:27-34`). The SSE
command stream emits staged events and is covered by `test_command_stream_*`, which passes
in the fixed copy. **PASS.**

The one escape found from the API surface — a simulated watch answering `CONFIRM` and
causing the deletion to execute — is documented as **RT-1** in
`PHASE9_SECURITY_REDTEAM.md`; it is an authorisation-logic defect, not an HTTP defect, so it
is not double-counted here.

## 9.10 Findings and gaps

| ID        | finding                                                                                                                                                                                                                                                                                                            | severity                       |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------ |
| **API-0** | **`GET /auth/token` returns the live master token to any unauthenticated, header-less local request; the leaked token then authorises `POST /command`. Defeats the entire token-auth model and the 0600 token file.**                                                                                              | **P0**                         |
| API-1     | No request body size limit; 2 MB `/command` accepted and burned 4.07 s, larger bodies break the pipe instead of returning 413                                                                                                                                                                                      | **P2**                         |
| API-2     | `Host: 0.0.0.0:8765` is allowlisted, weakening the rebinding list's "loopback only" claim — and it is also a `Host` that `/auth/token` will answer                                                                                                                                                                 | **P3**                         |
| API-3     | No CSP / referrer-policy; inline script on `/dashboard`; rejection responses skip the security-header layer                                                                                                                                                                                                        | **P4**                         |
| API-4     | `/` exposes name+version unauthenticated                                                                                                                                                                                                                                                                           | **P4**                         |
| API-5     | No status endpoints for home / robotics / car / vision; peripheral truth is only reachable through tool calls or by parsing `/health`, so the dashboard and any monitor must re-implement probing                                                                                                                  | **P3** (product, not security) |
| API-6     | Whether `/docs`, `/redoc`, `/openapi.json` are mounted is **UNVERIFIED** — the limiter returned 429 before they could be read. Command to verify: restart with a fresh `JARVIS_HOME`, then `curl -H "X-JARVIS-Token: …" -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/openapi.json` one request at a time | **P4**                         |
| —         | Token _verification_ itself is sound: constant-time compare, correct precedence, 0600 file, no token in logs. CORS default-deny, host enforcement, rate limiting, malformed/empty input handling, and `Protected` on every destructive route all behaved correctly                                                 | **PASS**                       |

**Priority note.** API-0 dominates this phase. The mitigations that make it survivable today
are that the socket is loopback-bound and no remote attacker can reach it without first
running code on the machine — so it is a **local privilege escalation / defence-in-depth
collapse**, not a remote takeover. It is still P0 by the brief's definition, because the
system can be made to perform an unsafe action by a party the design believes it has
excluded, and because every other control in this layer assumes the token is secret.

## 9.11 Reproduction

```bash
rsync -a --exclude .venv --exclude __pycache__ --exclude .pytest_cache \
      --exclude dist --exclude app/build \
      /Users/abhijeetmiskin/AppData/jarv/jarvis-forge/ /tmp/p9shadow/
sed -i '' '16s/import ActionResult/import ActionResult, Verification/' /tmp/p9shadow/jarvis/tools/base.py

cd /tmp/p9shadow
JARVIS_HOME=$(mktemp -d) JARVIS_API_TOKEN=audit-token-12345 JARVIS_WATCH_SIMULATE=1 \
  PYTHONPATH=/tmp/p9shadow <repo>/.venv/bin/python -m uvicorn jarvis.api.app:app \
  --host 127.0.0.1 --port 8765

<repo>/.venv/bin/python /tmp/p9_api.py      # auth, host, CORS, malformed, oversized, headers
<repo>/.venv/bin/python /tmp/p9_api2.py     # rate-limit burst, watch reply chain, body sizes, public routes
```
