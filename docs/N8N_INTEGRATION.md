# n8n automation in Vantage

Vantage runs **approved n8n workflows** and reports exactly what happened. It
does not create, edit, activate or delete workflows, and it cannot reach n8n's
admin API.

- Deployment (Docker, backups, security): `~/n8n/docs/OPERATIONS.md`
- Adapter: `jarvis/integrations/n8n.py`, `n8n_client.py`, `n8n_audit.py`
- Tools: `jarvis/tools/n8n_tools.py`
- Routes: `GET /n8n/status`, `GET /n8n/workflows`,
  `POST /n8n/workflows/{key}/run`, `GET /n8n/executions`
- Screens: macOS **Automation** (`N8NView.swift`)

---

## The registry is the allowlist

`REGISTRY` in `jarvis/integrations/n8n.py` is a hand-written tuple. A workflow
exists in Vantage only because a person added it there, with a description, its
input schema, and a risk class. Nothing discovers n8n's workflows and registers
them.

```python
WorkflowSpec(
    key="echo",                         # Vantage's name; the only id a caller may use
    title="Diagnostic echo",
    description="Returns what it was given...",
    path="vantage/echo",                # the webhook, never sent to clients
    risk=RiskClass.READ,                # READ | MUTATING | EXTERNAL
    fields=(FieldSpec("message", "text", required=True, max_length=2000),),
    idempotent=True,                    # whether a retry is safe
    context=(),                         # data Vantage gathers and supplies
)
```

Adding a workflow is a code change with a review, which is the point: a chat
sentence cannot widen what n8n may be asked to do.

`JARVIS_N8N_ALLOWED_WORKFLOWS` (or the Connections file) may narrow the list
further. It cannot widen it — execution looks the key up in `REGISTRY`.

## Execution path, once

Both the HTTP route and the chat tool call `N8NClient.execute`. There is no
second path. The order inside it is the security design:

1. **Key lookup** in the registry (a caller cannot supply a URL or an n8n id);
2. **Allowlist** check;
3. **Confirmation** — a `MUTATING`/`EXTERNAL` workflow is refused without
   `confirmed=True`; on the route that is a `409`;
4. **Input validation** against the declared schema — unknown fields are
   refused, sizes and choices are bounded;
5. **Correlation id** generated here (never accepted from the caller), and an
   in-flight guard so the same run cannot start twice;
6. **POST to the webhook**, bounded by a total deadline;
7. **Result read** with a byte ceiling and a content check.

## What each outcome means

| Status        | Meaning                                                               | Retry offered          |
| ------------- | --------------------------------------------------------------------- | ---------------------- |
| `succeeded`   | n8n answered 2xx with readable JSON and did not report a failure      | yes if idempotent      |
| `failed`      | n8n answered, and said the workflow failed (or 4xx/5xx)               | yes if idempotent      |
| `timeout`     | the request outlived its budget                                       | yes only if idempotent |
| `unreachable` | nothing answered                                                      | yes if idempotent      |
| `refused`     | nothing left Vantage (unknown key, unconfirmed, bad input, duplicate) | —                      |

A 200 is **not** enough. A workflow that returns `{"ok": false}` with a 200 is a
failure; a response that is not JSON is a failure; a response over 512 KiB is a
failure. The adapter says which, because "it did not work" is not a diagnosis.

## Secrets

- The n8n API key lives in the **Keychain** (`KeychainStorage` under `n8n`) or
  `JARVIS_N8N_API_KEY`. It is never returned by a route, never in a log line,
  and scrubbed from every error sentence.
- The URL is validated: `http`/`https` only, no query or fragment, and a
  non-loopback host is refused unless `JARVIS_N8N_ALLOW_REMOTE=true` is set
  deliberately.
- **Loopback is not the authentication.** Vantage's routes carry the existing
  token and same-origin gate; n8n's UI has its own owner account.

## Routes

```
GET  /n8n/status?probe=true      -> {state, detail, problems, config(redacted), latency_ms}
GET  /n8n/workflows              -> {workflows:[{key,title,risk,fields,...}], enabled, count}
POST /n8n/workflows/{key}/run    -> {ok,status,correlation_id,message,output,...}
GET  /n8n/executions?limit=20    -> {executions:[...], count}
```

Example — run the diagnostic workflow:

```bash
curl -X POST http://127.0.0.1:8765/n8n/workflows/echo/run \
  -H "X-JARVIS-Token: $(cat ~/.jarvis-forge/api_token)" \
  -H "Origin: http://127.0.0.1:8765" -H "Content-Type: application/json" \
  -d '{"inputs":{"message":"hello from Vantage","tag":"demo"}}'
```

Observed response (2026-10-29, real n8n):

```json
{
  "ok": true,
  "status": "succeeded",
  "correlation_id": "v8n-98bf359979f01beb",
  "workflow": "echo",
  "message": "Echoed hello from Vantage",
  "output": {
    "ok": true,
    "summary": "Echoed hello from Vantage",
    "received_at": "2026-10-29T09:05:03.742Z",
    "_vantage": { "correlation_id": "v8n-98bf359979f01beb" }
  },
  "http_status": 200,
  "duration_ms": 206,
  "retry_safe": true
}
```

Errors: `404` unknown workflow (with the approved list), `403` refused
(allowlist or invalid input), `409` confirmation required, `401` no token,
`403 Same-origin request required` without an `Origin`.

## Chat

Sentences that name **n8n**, **workflow** or **automation** route to the
registry; everything else keeps its existing behaviour.

| Sentence                                 | Result                                    |
| ---------------------------------------- | ----------------------------------------- |
| `list my n8n workflows`                  | the approved list with what each needs    |
| `run my echo workflow saying hello`      | runs `echo`, message = "hello"            |
| `run the daily agenda workflow`          | runs `daily_agenda`                       |
| `run my repository maintenance workflow` | _not registered_ → answered with the list |

The model never supplies a workflow id: the sentence is matched against the
registry's keys and titles, and the **key** is what the tool receives.

## Calendar context

`daily_agenda` declares `context=("events",)`. Vantage gathers today's events
from the calendar it is already connected to and sends them with the request,
plus `_events_available` and `events_error`. The workflow gets **no Google
credential** — it has nothing to revoke and cannot read the calendar on its own.
A failed read is passed as a failure, so the workflow reports a failed read
rather than summarising a free day that does not exist.

## Audit

`GET /n8n/executions` returns the recent runs: time, workflow key, correlation
id, status, HTTP status, duration, and the initiating session. It stores **no
inputs and no outputs** — an agenda's contents are the person's schedule, and
the audit trail's job is to say what ran, not what it said.
