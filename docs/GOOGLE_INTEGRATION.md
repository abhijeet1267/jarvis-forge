# Google Calendar + Tasks

Vantage reads Google Calendar and Google Tasks directly, through a dedicated
adapter, rather than duplicating them. Google is the source of truth for what it
holds: events and tasks are never copied into Vantage's own database, so the
same item never becomes two reminders.

## What works

| Capability                                 | Status                                         |
| ------------------------------------------ | ---------------------------------------------- |
| Read today's Google Calendar events        | Implemented, read-only                         |
| Read Google Tasks, with completed filter   | Implemented                                    |
| Create a task                              | Implemented (route, macOS, Android, assistant) |
| Complete a task                            | Implemented, verified against Google's answer  |
| Create/complete a task by voice or chat    | Implemented as tools, with approval            |
| Show connection state on macOS and Android | Implemented                                    |
| Edit / delete calendar events              | **Not offered** — see below                    |
| Connect / disconnect from the phone        | **Not offered** — by design                    |

### Tasks are reachable from the assistant, not just the screen

`list_google_tasks`, `add_google_task` and `complete_google_task` are tools in
the registry (`jarvis/tools/google_tasks.py`). A sentence that names Google --
"add a task to Google Tasks buy milk", "mark buy milk done in Google Tasks" --
routes to them. A sentence that does not still goes to the local planner, so the
two task lists never swap on a phrasing change.

The write tools are `EXTERNAL` and require confirmation. On the **phone** this
composes with the self-approval rule: a task created from Android produces a
confirmation that Android cannot approve, so the Mac answers it. That is the
connection lifecycle the phone gets -- it can ask, it cannot authorise.

### The phone reads and asks, the Mac connects and approves

The Android companion talks to the companion gateway, whose allowlist is narrow
by design. `/google/status`, `/google/calendar/events` and `/google/tasks` are
on it because the phone could already obtain the same data through `/command`
(which can run arbitrary tool actions) — a structured read route widens nothing,
it only makes the data usable as a screen.

Writes do **not** get their own phone route. Android asks for one through
`/command` (already allowed), which is the path that carries the confirmation
workflow; a direct `/google/tasks` POST on the gateway allowlist would bypass it.
`/google/connect*`, `/google/callback` and `/google/disconnect` are
`SameOriginProtected` and are **not** on the gateway allowlist. A phone cannot
start or tear down the Mac's Google authorisation.

**The stated limitation:** Google must be connected on the Mac. Android uses the
Mac's connection through the gateway; it cannot authorise an account on its own.

### Calendar is read-only on purpose

The connection requests `calendar.readonly`. Vantage has no event-editing UI on
either platform, so it does not ask for a write scope it cannot use. An attempt
to write returns a plain "this connection is read-only" result rather than
inventing a success or requesting a permission nothing exercises.

## Setup

Nothing is fabricated: without the steps below, Vantage reports
`not_configured` and shows these same steps in the app.

1. Create a project at <https://console.cloud.google.com>.
2. Enable the **Google Calendar API** and the **Google Tasks API** for it.
3. Create an **OAuth client** of type **Desktop app**.
   (A "Web application" client also works if you set `google_client_secret`.)
4. Set the client id in your environment:

   ```bash
   export JARVIS_GOOGLE_CLIENT_ID="....apps.googleusercontent.com"
   # only for a "Web application" client:
   export JARVIS_GOOGLE_CLIENT_SECRET="..."
   ```

   A shell `export` only reaches a backend started from that shell. Every
   supported launch sets the process directory to the app/repo root, so
   `Settings` finds a `.env` there: the repo root for `run.py` and the macOS
   app, and `Vantage.app/Contents/Resources/app` for the installed bundle.
   Put `JARVIS_GOOGLE_CLIENT_ID=...` in that directory's `.env`. Never put it
   inside a signed bundle after signing — it invalidates the seal; export it
   in the environment instead. `.env` is gitignored; do not commit it.

5. Register this redirect URI on the client:

   ```
   http://127.0.0.1:8765/google/callback
   ```

   The port is fixed to the backend's actual port: the route builds the
   redirect from the request, so if you run on a non-default port, register
   that exact `http://127.0.0.1:<port>/google/callback` too (Google refuses an
   unregistered redirect with `redirect_uri_mismatch`).

6. Start the backend, open **Settings → Connections** (macOS: the **Google**
   screen), and press **Connect**. The browser handles consent; the callback
   lands on the local backend, which stores the refresh token.
7. Press **Refresh** to see the verified state.

### Settings

| Variable                         | Meaning                                                        |
| -------------------------------- | -------------------------------------------------------------- |
| `JARVIS_GOOGLE_CLIENT_ID`        | OAuth client id. Empty = not configured.                       |
| `JARVIS_GOOGLE_CLIENT_SECRET`    | Only for a Web-application client. Never required for Desktop. |
| `JARVIS_GOOGLE_CALENDAR_ENABLED` | Default `true`. `false` drops the calendar scope entirely.     |
| `JARVIS_GOOGLE_TASKS_ENABLED`    | Default `true`. `false` drops the tasks scope.                 |
| `JARVIS_GOOGLE_CALENDAR_ID`      | Which calendar to read. Default `primary`.                     |
| `JARVIS_GOOGLE_TASK_LIST_ID`     | Which task list. Default `@default`.                           |

## Security

- **The refresh token is never a setting.** Settings are read into logs,
  diagnostics and `/health` payloads; the token lives in the OS keychain via the
  project's existing `KeychainStorage`. It is never returned by any route — the
  status payload is asserted to contain no token material.
- **A saved token is not a connection.** "Connected" is only reported after a
  live call to Google succeeded. A stored-but-revoked token reports `expired`
  with the reason.
- **PKCE + single-use state.** The authorization uses an S256 code challenge,
  and the callback is bound to a state value held in memory. A forged or
  replayed callback cannot complete an authorization it did not start.
- **Least privilege.** Read-only calendar; read+write tasks. Write scopes are
  only requested for functionality that exists.
- **Errors are not successes.** A failed calendar read is a failure with a
  status, never an empty day — telling someone they are free when the calendar
  could not be read is the mistake this design exists to prevent. This holds
  through the availability tool: `find_free_time` refuses with `slot=None` for
  every failure status, which is tested rather than assumed.
- **The authorization code does not reach the log.** uvicorn's access log
  records the full request line, and Google's redirect carries `?code=…` —
  measured on this machine, the code appeared verbatim. `jarvis/log_safety.py`
  scrubs credential-bearing query parameters from the access log; the parameter
  names stay so the log still says what happened. The test suite includes a
  control that runs a real server _without_ the filter to prove the leak is
  real and that the test would notice if the filter were dropped.
  The scrubber is installed when `jarvis.api.app` is imported, not only in
  `run.py`, because the macOS app launches `uvicorn jarvis.api.app:app`
  directly — measured on that path, a `/google/callback` code reached the log
  before the import-time install was added.
- **One backend process, one uvicorn worker.** The pending authorization and
  the access-token cache live in the backend process. A multi-worker start
  (`uvicorn --workers N > 1`) would split `connect/begin` from `callback` across
  processes and fail every connect. No supported launch does this; see
  `docs/LIVE_VERIFICATION.md` §1e before changing that.
- **Disconnect is honest about revocation.** A token Google has already
  revoked (`invalid_token`) counts as revoked, not as "Google could not be
  reached"; only a real transport failure says that, and the local token is
  removed either way.

## Routes

All are `Protected` (master token or a paired session) unless noted.

| Route                                           | Notes                                                       |
| ----------------------------------------------- | ----------------------------------------------------------- |
| `GET /google/status`                            | Verified connection state + setup requirements. No secrets. |
| `POST /google/connect/begin`                    | Same-origin. Returns the authorization URL.                 |
| `GET /google/callback`                          | Google's redirect. Bound by state + PKCE.                   |
| `POST /google/disconnect`                       | Same-origin. Revokes remotely where possible.               |
| `GET /google/calendar/events?date=today&days=1` | `status` says whether the read succeeded.                   |
| `GET /google/tasks?show_completed=false`        | Task list.                                                  |
| `GET /google/tasks/lists`                       | Task lists, for choosing one.                               |
| `POST /google/tasks`                            | Same-origin. Create.                                        |
| `POST /google/tasks/{id}/complete`              | Same-origin. Verified against Google's answer.              |

## Architecture

```
jarvis/integrations/google_auth.py      OAuth lifecycle + keychain storage
jarvis/integrations/google_calendar.py  GoogleCalendarProvider (CalendarProvider)
jarvis/integrations/google_tasks.py     GoogleTasksClient
jarvis/tools/google_tasks.py            Tools: list/add/complete (add+complete EXTERNAL)
```

### Tools

| Tool                   | Risk       | Confirmation | Purpose                             |
| ---------------------- | ---------- | ------------ | ----------------------------------- |
| `list_google_tasks`    | `READ`     | no           | Read the connected account's tasks. |
| `add_google_task`      | `EXTERNAL` | yes          | Create a task in Google Tasks.      |
| `complete_google_task` | `EXTERNAL` | yes          | Mark a task completed, verified.    |

They are reachable by voice, chat and the phone through `/command`. The trigger
is an explicit "Google Tasks" in the sentence, so ordinary "add a task" still
uses the local planner (pinned by `test_without_the_word_google_the_local_planner_is_unchanged`).

`GoogleCalendarProvider` implements the existing `CalendarProvider` contract, so
the planner, the availability tools and `/calendar/*` use Google without knowing
it is Google. `get_calendar_provider()` prefers a connected Google account, then
CalDAV, then the local calendar — and only selects a remote provider when an
account is actually usable, so a half-finished setup falls back rather than
routing every read to a provider that can only fail.

This is verified rather than asserted: `test_the_planner_uses_the_connected_google_calendar`
drives the real `start_day` tool with the selector pinned to a Google provider
and requires the events to appear; `test_the_planner_does_not_claim_an_empty_day_when_google_fails`
requires a failed Google read to be reported as a failure, never as "nothing
scheduled". Both fail if the planner's `read_succeeded` branch is weakened.

## Tests

```bash
.venv/bin/python -m pytest tests/test_google_integration.py -q
.venv/bin/python -m pytest tests/test_google_tasks_tools_and_mcp.py -q
cd android && JAVA_HOME=/opt/homebrew/opt/openjdk@21 ./gradlew :app:testDebugUnitTest
```

The Android toolchain needs a JDK 17–21 (the repo's `gradle.properties` says
so; JDK 22+ breaks AGP's instrumentation transform). On this machine
`/opt/homebrew/opt/openjdk@21` is that JDK.

No test contacts Google. The adapters are driven with an injected auth object,
and HTTP shapes go through `httpx.MockTransport`. Nothing probes a real account
or mutates live calendar data.
