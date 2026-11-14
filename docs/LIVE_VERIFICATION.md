# Live verification runbook

Exact steps to prove the Google and MCP integrations against real accounts and a
real server. Until these are run, the integrations are **locally tested only**
(fakes and `httpx` shapes) — that is stated in `docs/PROJECT_STATUS.md` and this
file does not change it.

Prerequisites this machine already has: `.venv`, JDK 21
(`/opt/homebrew/opt/openjdk@21`), Android SDK (`~/Library/Android/sdk`).
Not had: a Google OAuth client, an MCP endpoint, an Android device/emulator.

Every block below is a command you can paste. `$TOKEN` always means the master
API token at `~/.jarvis-forge/api_token`; it is never printed.

---

## 0. Start from a clean, isolated data directory (recommended)

A live run writes real credentials and real tasks. Use a throwaway home so your
normal one is untouched, and so a bad experiment is one `rm -rf` away.

```bash
export VH=~/vantage-live-check
rm -rf "$VH"
JARVIS_HOME="$VH" .venv/bin/python run.py serve
```

The token file is written by the _server_ as it starts, so the second terminal's
export only works once that line has printed `Vantage API token -> …/api_token`.
Leave the server running. In a second terminal, for every step below:

```bash
export VH=~/vantage-live-check
export TOKEN="$(cat "$VH/api_token")"
export BASE=http://127.0.0.1:8765
```

`$BASE` in every `-H "Origin: …"` must be exactly the scheme+host+port you are
calling, because `/google/connect/begin`, `/google/disconnect` and the Google
writes are same-origin protected. Calling `http://127.0.0.1:8765` with
`Origin: http://localhost:8765` is refused — correctly.

---

## 1. Google: connect for real

### 1a. Create the OAuth client (one time, in the browser)

1. <https://console.cloud.google.com> → new project.
2. Enable **Google Calendar API** and **Google Tasks API**.
3. OAuth consent screen → add your own account as a **test user**.
4. Credentials → **Create credentials → OAuth client ID → Desktop app**.
5. Copy the client id. Desktop clients need **no** secret.

### 1b. Export it and restart the backend

The backend reads settings at start, so restart it with the new environment:

```bash
export VH=~/vantage-live-check
export JARVIS_HOME="$VH"
export JARVIS_GOOGLE_CLIENT_ID="YOUR_ID.apps.googleusercontent.com"
.venv/bin/python run.py serve
```

(`JARVIS_GOOGLE_CLIENT_SECRET` is only for a _Web application_ client.)

### 1c. Confirm it is configured but not yet connected

```bash
curl -s -H "X-JARVIS-Token: $TOKEN" "$BASE/google/status" | python3 -m json.tool
```

Expect `connection.state == "disconnected"`, `connection.configured == true`,
`setup.setup == []`. If it says `not_configured`, the restart did not pick up
`JARVIS_GOOGLE_CLIENT_ID`.

### 1d. Begin the authorization and open the URL

```bash
curl -s -X POST -H "X-JARVIS-Token: $TOKEN" -H "Origin: $BASE" \
     "$BASE/google/connect/begin" | tee /tmp/google_begin.json | python3 -m json.tool
```

Copy `url` from that output and open it in a browser on the same machine. (The
redirect lands on the backend, not on a page you need to see.) The response's
`redirect_uri` must be the one registered: `http://127.0.0.1:8765/google/callback`
— note **8765**, so start the backend on the default port for this step.

### 1e. The callback in the browser

Grant consent. You should see **"Connected to Google"**. Two things this proves
that the unit tests cannot:

- the callback is a _separate HTTP request_ from `connect/begin`, so the
  process-wide pending state survived it;
- PKCE and the `state` match, so the exchange actually completed at Google.

The pending state (and the access-token cache) live in the backend process's
memory, so this must be **one backend process, with one uvicorn worker**. Every
supported launch is: `run.py serve` (no `--workers`), the macOS app's
`uvicorn jarvis.api.app:app` (no `--workers`), and the packaged app via
`app/launcher.py`. Starting uvicorn with `--workers 2` or more would let this
callback land on a worker that never saw the `begin` and refuse it with "There
is no authorization in progress". Do not add workers without moving the state
to a persisted file (0600, TTL, single-use) first.

### 1f. Verify, then prove the token is not exposed

```bash
curl -s -H "X-JARVIS-Token: $TOKEN" "$BASE/google/status" | python3 -m json.tool
```

Expect `state == "connected"`, your `account` email, and the granted `scopes`.
Then prove the refresh token is nowhere in anything the API returns:

```bash
for p in /google/status /mcp/status /health; do
  curl -s -H "X-JARVIS-Token: $TOKEN" "$BASE$p"
done | grep -Ei 'refresh|1//|ya29\.' && echo "LEAK — investigate" || echo "no token material in any payload"
```

And that it is in the OS keychain, not in a file. `KeychainStorage` uses the
`keyring` library, whose service name is prefixed: the entry is
`vantage.ai.google.oauth` (account `credential`). Read it with `keyring` rather
than guessing at `security`'s field layout:

```bash
.venv/bin/python -c "
import keyring
v = keyring.get_password('vantage.ai.google.oauth', 'credential')
print('stored in keychain:', bool(v), '| length', len(v or ''))
print('looks like a refresh token:', bool(v) and v.startswith('1//'))
"
# and nothing google-shaped was written into the data dir
ls -l "$VH" | grep -iE 'google' && echo "UNEXPECTED file — investigate" || echo "no google credential file on disk (correct)"
```

To confirm the item is really in the login keychain (not a fallback store):

```bash
.venv/bin/python -c "import keyring; print(keyring.get_keyring())"
```

Expect a macOS `Keyring` backend. If it says a `PlaintextKeyring` or
`fail.Keyring`, the token went somewhere weaker — stop and fix that before
connecting a real account.

### 1g. Live reads

```bash
curl -s -H "X-JARVIS-Token: $TOKEN" "$BASE/google/calendar/events?date=today&days=1" | python3 -m json.tool
curl -s -H "X-JARVIS-Token: $TOKEN" "$BASE/google/tasks?show_completed=false" | python3 -m json.tool
```

Each must report a `status` and `read_succeeded`. **A `read_succeeded: false`
here is the success of the honest-failure design**, not a failure of the run:
it proves a broken read is not drawn as an empty day.

### 1h. Live writes — prove Google is the source of truth

Create through the route, then confirm in the Google Tasks web UI:

```bash
curl -s -X POST -H "X-JARVIS-Token: $TOKEN" -H "Origin: $BASE" -H 'content-type: application/json' \
     -d '{"title":"Vantage live check","notes":"created by docs/LIVE_VERIFICATION.md"}' \
     "$BASE/google/tasks" | tee /tmp/google_task.json | python3 -m json.tool
```

Expect `ok: true` and a `task.id`. Open <https://tasks.google.com> and confirm
the task is there. Then complete it — the adapter only claims success when
Google reports `status: completed`:

```bash
TASK_ID=$(python3 -c 'import json;print(json.load(open("/tmp/google_task.json"))["task"]["id"])')
curl -s -X POST -H "X-JARVIS-Token: $TOKEN" -H "Origin: $BASE" -H 'content-type: application/json' \
     -d '{}' "$BASE/google/tasks/$TASK_ID/complete" | python3 -m json.tool
```

This also proves there is **no local copy**: check the local planner has no such task.

```bash
curl -s -H "X-JARVIS-Token: $TOKEN" "$BASE/tasks" | grep -ci 'Vantage live check' || echo "0 local copies (correct)"
```

### 1i. The same writes through the assistant (tools + approval)

The tool path is what the phone uses. First prove routing, then the gate, then
the round-trip. (These read the same isolated home, so pass `JARVIS_HOME`.)

Routing — an explicit "Google Tasks" sentence must pick the Google tool:

```bash
JARVIS_HOME="$VH" .venv/bin/python -c "
from jarvis.core.orchestrator import JarvisCore
core = JarvisCore()
plan = core.planner.parse('add a task to Google Tasks buy milk')
print('intent:', plan.intent, [a.tool for a in plan.actions])
"
```

Expect `intent: add_google_task ['add_google_task']`. (Confirmed on this tree.)

A simpler, exact proof of the gate (no model involved):

```bash
.venv/bin/python -c "
from jarvis.core.models import PlannedAction
from jarvis.core.orchestrator import JarvisCore
from jarvis.security.policy import PolicyDecision
core = JarvisCore()
a = PlannedAction(tool='add_google_task', params={'title': 'live gate check'})
v = core.policy.evaluate(a, session_id='LIVE-1')
print('decision:', v.decision.value, '| confirmation:', v.confirmation_id)
assert v.decision is PolicyDecision.CONFIRM, v.decision
print('OK: a Google write is gated')
"
```

Then approve it the way an operator does and confirm Google changed:

```bash
.venv/bin/python -c "
import asyncio
from jarvis.core.orchestrator import JarvisCore
core = JarvisCore()
cid = [c for c in core.policy.pending() if c['tool']=='add_google_task'][0]['confirmation_id']
print(asyncio.run(core.resolve_confirmation(cid, True, 'LIVE-1')).reply)
"
```

### 1j. The callback's adversarial cases (do these once, on the real server)

These are the properties the brief calls out. Each expects a refusal, and each
must not disturb a newer valid attempt. The strings below are the actual refusal
sentences in `jarvis/integrations/google_auth.py`; match them with `grep -F`
(fixed strings), because a regex would quietly pass on a near-match.

```bash
# (a) a forged callback with nothing pending -> "There is no authorization in progress"
curl -s "$BASE/google/callback?code=forged&state=forged" | grep -Fo 'There is no authorization in progress' | head -1

# (b) stale tab: begin twice, complete with the FIRST state -> refused,
#     and the newer attempt must still be completable in its own tab.
A=$(curl -s -X POST -H "X-JARVIS-Token: $TOKEN" -H "Origin: $BASE" "$BASE/google/connect/begin")
B=$(curl -s -X POST -H "X-JARVIS-Token: $TOKEN" -H "Origin: $BASE" "$BASE/google/connect/begin")
SA=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["state"])' "$A")
SB=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["state"])' "$B")
curl -s "$BASE/google/callback?code=x&state=$SA" | grep -Fo 'replaced by a newer one' | head -1
# The second attempt must survive that. Its callback should reach Google (with
# a fake code it fails at Google, not at the state check) -- that difference is
# the proof the first callback did not cancel it:
curl -s "$BASE/google/callback?code=x&state=$SB" | grep -Fo 'Google refused the token exchange' | head -1

# (c) single use: replay the state that (b) just consumed -> nothing pending.
curl -s "$BASE/google/callback?code=x&state=$SB" | grep -Fo 'There is no authorization in progress' | head -1
```

Verified on this tree with a throwaway home and a deliberately invalid client
id (so the exchange fails at Google, not at the state gate): (a) says "There is
no authorization in progress", (b) says "replaced by a newer one" for the first
state while the second reaches Google, and (c) says "There is no authorization
in progress" on replay.

`STATE_TTL_SECONDS = 600`; waiting ten minutes then completing must answer
"The authorization took too long and is no longer valid. Start again." That one
is slow, so run it last or skip it and say so.

### 1k. Disconnect

```bash
curl -s -X POST -H "X-JARVIS-Token: $TOKEN" -H "Origin: $BASE" "$BASE/google/disconnect" | python3 -m json.tool
curl -s -H "X-JARVIS-Token: $TOKEN" "$BASE/google/status" | python3 -c "import json,sys;print(json.load(sys.stdin)['connection']['state'])"
```

Expect `revoked: true` and then `disconnected`. Confirm at
<https://myaccount.google.com/permissions> that Vantage's access is gone.

---

## 2. MCP: one real server

### 2a. Zapier MCP (broad catalogue) or n8n (self-hosted)

- **Zapier**: create an MCP server at <https://zapier.com/mcp>, copy its endpoint
  (usually token-bearing), then add one action (e.g. "Send Email") to it.
- **n8n**: add an **MCP Server Trigger** node, activate the workflow, copy the
  production URL.

### 2b. Find the real tool names, then allowlist one

Export only the URL first, restart, and read what the server advertises:

```bash
export VH=~/vantage-live-check JARVIS_HOME=~/vantage-live-check
export JARVIS_MCP_URL="https://your-endpoint/mcp"
export JARVIS_MCP_TOKEN="..."           # only if the endpoint needs it
.venv/bin/python run.py serve
```

```bash
curl -s -H "X-JARVIS-Token: $TOKEN" "$BASE/mcp/status" | python3 -m json.tool
```

Expect `connection.state == "connected"`, `tool_count > 0`, and a `tools` list.
`callable` will be `[]` — that is correct until 2c.

**Prove listed ≠ callable right now**, before widening anything:

```bash
curl -s -H "X-JARVIS-Token: $TOKEN" "$BASE/mcp/status" \
  | python3 -c "import json,sys;c=json.load(sys.stdin)['connection'];print('advertised',len(c['tools']),'callable',len(c['callable']))"
```

### 2c. Allowlist exactly one tool and restart

```bash
export JARVIS_MCP_ALLOWED_TOOLS="<one exact name from 2b>"
.venv/bin/python run.py serve
```

```bash
curl -s -H "X-JARVIS-Token: $TOKEN" "$BASE/mcp/status" \
  | python3 -c "import json,sys;c=json.load(sys.stdin)['connection'];print('callable:',c['callable'])"
ls -1 "$VH" >/dev/null; .venv/bin/python -c "
from jarvis.tools import build_default_registry
print([n for n in build_default_registry().names() if n.startswith('mcp__')])"
```

Expect exactly one `mcp__<name>__<tool>` in both.

### 2d. Prove the approval gate, then call it for real

```bash
.venv/bin/python -c "
from jarvis.core.models import PlannedAction
from jarvis.core.orchestrator import JarvisCore
from jarvis.security.policy import PolicyDecision
core = JarvisCore()
name = [n for n in core.tools.names() if n.startswith('mcp__')][0]
v = core.policy.evaluate(PlannedAction(tool=name, params={'arguments': {}}), session_id='LIVE-2')
print(name, '->', v.decision.value, v.reason)
assert v.decision is PolicyDecision.CONFIRM
print('OK: MCP tools are EXTERNAL and confirmed')
"
```

Then approve that confirmation (same pattern as 1i) and confirm the effect in the
third-party app (the email arrived, the ticket exists).

### 2e. Prove a non-allowlisted tool cannot be called

```bash
.venv/bin/python -c "
from jarvis.integrations import mcp_client
from jarvis.config import get_settings
c = mcp_client.connector_from_settings(get_settings())
try:
    c.call_tool('a-tool-not-on-the-list', {})
except mcp_client.MCPError as e:
    print('refused as expected:', e)
"
```

The refusal must happen **before** any network call (`call_tool` checks the
allowlist first).

### 2f. Token never leaks

```bash
# with the token still exported in the backend's env
curl -s -H "X-JARVIS-Token: $TOKEN" "$BASE/mcp/status" | grep -c "$JARVIS_MCP_TOKEN" || echo "0 (good)"
```

---

## 3. Android, end to end

### 3a. Start the gateway

The gateway starts with the backend only when LAN mode is on. Turn it on in the
macOS app (**Settings → Allow devices on my wifi**) or by setting
`JARVIS_HOST=0.0.0.0` and writing `~/.jarvis-forge/lan_mode` to `true`; then
restart the backend. Confirm it is listening:

```bash
curl -s -H "X-JARVIS-Token: $TOKEN" "$BASE/companion/status" | python3 -m json.tool
# running: true, lan_mode: true, and last_error empty
curl -sk https://127.0.0.1:8766/companion/health | python3 -m json.tool
```

`/companion/health` reports the **certificate fingerprint** the phone pins. Note
the port: `JARVIS_COMPANION_PORT`, default **8766**.

### 3b. Pairing code (Mac only, by design)

```bash
curl -s -X POST -H "X-JARVIS-Token: $TOKEN" "$BASE/companion/pairing-code" | python3 -m json.tool
```

This returns `code`, `certificate_fingerprint`, `expires_in_seconds` (120) and
`companion_port`. A phone cannot issue one — the route checks the peer is local.

### 3c. Run the app

```bash
cd android
JAVA_HOME=/opt/homebrew/opt/openjdk@21 \
ANDROID_HOME=$HOME/Library/Android/sdk ANDROID_SDK_ROOT=$HOME/Library/Android/sdk \
./gradlew :app:installDebug
adb shell am start -n com.vantage.companion/.MainActivity
```

`adb` must see a device or running emulator first (`adb devices`). If none is
listed, save this whole section as **BLOCKED (no device)** — do not claim an
Android runtime result without one.

In the app: **Setup** → enter the gateway's LAN address and port (`8766`) and
the pairing code → connect. The app computes the leaf's SHA-256 and matches it
against the code's fingerprint; a mismatch throws rather than connecting.

### 3d. Prove the phone surface, live

On the phone's **Google** screen:

1. Status shows **connected** (it reads through the gateway as the master
   token, so it sees the Mac's connection).
2. Tasks list matches the Mac.
3. Add a task from the phone → you get an **approval-required** notice, not a
   silently created task.
4. Tap a task's circle to complete → same.

Then, on the Mac:

```bash
curl -s -H "X-JARVIS-Token: $TOKEN" "$BASE/confirmations" | python3 -m json.tool
```

Approve it as the operator. Confirm the task now exists in Google Tasks, and
that **the phone could not approve it itself** — attempt an approval from the
phone's Approvals screen and expect "You cannot approve your own requests."

### 3e. Prove the phone cannot reach the connector or the connection lifecycle

```bash
# $PHONE_SESSION is the session_token the phone got from
# POST /companion/pairing-redeem. These must all be 403 at the gateway:
curl -sk -H "X-JARVIS-Token: $PHONE_SESSION" https://<mac-ip>:8766/mcp/status
curl -sk -X POST -H "X-JARVIS-Token: $PHONE_SESSION" https://<mac-ip>:8766/google/connect/begin
# and an allowed read must still work, which proves the refusals above are the
# allowlist and not a broken session:
curl -sk -H "X-JARVIS-Token: $PHONE_SESSION" https://<mac-ip>:8766/google/status
```

---

## 4. Outcome table — fill this in as you run

The **pre-verified** column is what was actually run on this tree while writing
this runbook, against a throwaway home and a local fake MCP server. Those are
proven with the _software_; they still need your account / device to be
live-verified. Everything unmarked is genuinely yours to run.

| Check                                             | Section | Pre-verified here  | Your result |
| ------------------------------------------------- | ------- | ------------------ | ----------- |
| Google configured → disconnected                  | 1c      | yes                |             |
| Real callback completes in a separate request     | 1e      | no (needs consent) |             |
| Status connected + account + scopes               | 1f      | no (needs consent) |             |
| No token in any payload; keychain only            | 1f      | no (needs consent) |             |
| Calendar + tasks reads                            | 1g      | shape only         |             |
| Route create/complete appears in Google           | 1h      | no (needs consent) |             |
| No local copy of a Google task                    | 1h      | yes (unconfigured) |             |
| Tool write is CONFIRM, then completes             | 1i      | CONFIRM gate yes   |             |
| Forged state refused                              | 1j(a)   | yes                |             |
| Stale tab does not kill the newer attempt         | 1j(b)   | yes                |             |
| State single-use (replay)                         | 1j(c)   | yes                |             |
| MCP connected, advertised vs callable             | 2b      | yes (fake server)  |             |
| Allowlist gates the registry                      | 2c      | yes (fake server)  |             |
| MCP tool is CONFIRM, then effect is real          | 2d      | CONFIRM gate yes   |             |
| Non-allowlisted tool refused before network       | 2e      | yes                |             |
| MCP token not in status                           | 2f      | yes                |             |
| Android pairs with pinned fingerprint             | 3c      | no (needs device)  |             |
| Android add/complete awaits Mac approval          | 3d      | no (needs device)  |             |
| Phone cannot self-approve                         | 3d      | guard test only    |             |
| Phone cannot reach /mcp/status or /google/connect | 3e      | no (needs device)  |             |

Anything you cannot run (no account, no device) stays **BLOCKED** in
`docs/PROJECT_STATUS.md` — not "done".

---

## 5. Cleanup

```bash
# stop the backend, then:
rm -rf ~/vantage-live-check
```

If you connected a **real** Google account you also want to remove Vantage's
access at <https://myaccount.google.com/permissions> — deleting the local home
does not revoke it (use `/google/disconnect` first, which does ask Google).
