# MCP (remote connector)

Vantage can call tools on **one** remote [Model Context Protocol][mcp] server
over Streamable HTTP. This is the broad-coverage path: [Zapier MCP][zapier] for
a large app catalogue, an [n8n][n8n] MCP node for self-hosted workflows, or any
other spec-compliant HTTP server.

It is deliberately small. There is no framework here, no per-capability adapter
per app, and no automatic enablement of a server's catalogue. Those are the
things that would make a generic integration layer bigger than the work it
does.

[mcp]: https://modelcontextprotocol.io
[zapier]: https://zapier.com/mcp
[n8n]: https://n8n.io

## What works

| Capability                                 | Status                                                    |
| ------------------------------------------ | --------------------------------------------------------- |
| Connect to one Streamable-HTTP MCP server  | Implemented (`initialize` + `notifications/initialized`)  |
| Server responses as JSON **or SSE**        | Implemented (the response for the request id is selected) |
| Session id (`Mcp-Session-Id`) echo         | Implemented (captured from `initialize`, sent thereafter) |
| `MCP-Protocol-Version` on later requests   | Implemented (the version the server negotiated)           |
| JSON-RPC batch responses                   | Implemented (matched by request id)                       |
| `202` + empty body to a notification       | Accepted as the normal answer                             |
| Discover the server's tools (`tools/list`) | Implemented, follows pagination                           |
| Show state + setup on macOS                | Implemented (the **MCP** screen)                          |
| Call an **allowlisted** tool               | Implemented, EXTERNAL risk + approval                     |
| Call a non-allowlisted tool                | **Refused at the transport**                              |
| OAuth browser flow for an MCP server       | **Not offered** — use a server token                      |
| Multiple MCP servers                       | **Not offered** — one connector per process               |
| Custom auth header (non-`Authorization`)   | **Not exposed as config** — Bearer only                   |

## Two rules that shape it

**Listed is not callable.** `tools/list` is information. A discovered tool
becomes usable only when its name is on `JARVIS_MCP_ALLOWED_TOOLS`. The default
is an empty list, so a freshly configured server exposes zero tools. The macOS
MCP screen shows both counts — "N callable · M advertised" — and marks the
advertised-but-not-allowed rows as _not allowed_, rather than drawing them as if
they worked.

**Every MCP call is an external write.** Vantage cannot know whether a remote
tool sends mail, files a ticket or reads a spreadsheet, so each allowlisted tool
is registered at `RiskLevel.EXTERNAL` and requires confirmation. Invoking one
creates a confirmation; the Approvals screen resolves it. Because the tool runs
through the same policy engine as everything else, the rule that **a phone may
not approve its own request** applies here too.

There is no "call a tool" HTTP route. A route that ran an allowlisted tool
directly would be a second path around the approval gate, so the only way in is
the tool registry.

## Setup

Nothing is fabricated: without the steps below, Vantage reports
`not_configured` on the MCP screen and shows these same steps.

1. Create an MCP endpoint. Either:
   - **Zapier MCP** — create a server in your Zapier account and copy its
     endpoint (it usually carries an access token in the URL or wants a bearer
     token), or
   - **n8n** — add an MCP Server Trigger node to a workflow, activate it, and
     copy the node's production URL.
2. Set the endpoint and credential:

   ```bash
   export JARVIS_MCP_URL="https://your-mcp-endpoint.example/mcp"
   export JARVIS_MCP_TOKEN="..."          # only if the endpoint needs a bearer token
   ```

3. Choose the tools that may be called, by the names the server advertises:

   ```bash
   export JARVIS_MCP_ALLOWED_TOOLS="send_email,create_ticket"
   ```

4. Restart the backend, open the **MCP** screen on macOS, and press **Refresh**.
   "Connected" means a live `initialize` + `tools/list` succeeded. A URL alone
   reads as `disconnected`.

To recover the exact tool names, call `GET /mcp/status` after configuring the
URL: `connection.tools` lists what the server advertises (up to 50), and
`connection.tool_count` is the true count.

### Auth: Zapier and n8n

The connector's only credential is `JARVIS_MCP_TOKEN`, sent as
`Authorization: Bearer <token>`. That is exactly what **Zapier MCP** expects:
Zapier issues an access token for the MCP server, and you either paste it into
`JARVIS_MCP_TOKEN` or use the pre-built endpoint URL that already carries it
(the URL form is supported, and `status()` redacts it).

**n8n** builds its MCP Server Trigger URL from the instance host, so the token
is usually whatever authentication the workflow's webhook is configured for:

| n8n authentication   | What to do                                                                                                                                                                              |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| None                 | Leave `JARVIS_MCP_TOKEN` empty. Works; any caller who knows the URL can call it, so prefer one of the rows below.                                                                       |
| Header Auth (custom) | n8n can be told to accept `Authorization: Bearer <secret>` — configure the node that way, then `JARVIS_MCP_TOKEN=<secret>`.                                                             |
| Basic Auth           | **Not wired.** The connector sends a Bearer header only; there is no config key for a different scheme (adding one would mean editing `config.py`). Use Header Auth or a token instead. |

A custom (non-`Authorization`) header is not exposed: `MCPConfig` has
`auth_header`/`auth_prefix` fields, but no `Settings` variable sets them, so in
Vantage they are always `Authorization: Bearer `. That is deliberate — a
second credential path is a second place a token can leak.

### Settings

| Variable                   | Meaning                                                          |
| -------------------------- | ---------------------------------------------------------------- |
| `JARVIS_MCP_URL`           | The MCP endpoint. Empty = not configured.                        |
| `JARVIS_MCP_TOKEN`         | Bearer token, if the endpoint needs one. Never logged.           |
| `JARVIS_MCP_ALLOWED_TOOLS` | Comma-separated allowlist. **Empty = nothing callable.**         |
| `JARVIS_MCP_NAME`          | Label used in tool names (`mcp__<name>__<tool>`). Default `mcp`. |

## Security

- **The token is not a setting that leaks.** `Settings` holds it, but every
  status payload, error message and log line is passed through
  `security.privacy.redact`, and `MCPConfig.public()` never serialises it. The
  endpoint URL is redacted too, since a hosted MCP URL often carries the token
  in its query string.
- **The allowlist is enforced at the transport.** `MCPConnector.call_tool`
  refuses a name not on the list _before_ any request is made — so a future
  caller that reaches the connector without going through the tool adapter still
  hits the boundary. A test asserts the network is never touched.
- **Bounded.** Every exchange has a total wall-clock ceiling and a per-socket
  gap (`jarvis/net.py`'s `GAP_TIMEOUT`), reused by the header-aware reader, a
  512 KiB response cap, and a cap on how many tools a status payload echoes back
  (the advertised count is always the real one).
- **Honest states.** `not_configured`, `disconnected`, `connected`, `error` —
  the same vocabulary as the Google connection. A JSON-RPC error, an expired
  credential (`401`/`403`, which names `JARVIS_MCP_TOKEN`), and an unreachable
  host are different states with different fixes. A redirect says which final
  URL to configure rather than silently following it.
- **Writes are not special-cased.** They go through `RiskLevel.EXTERNAL` and the
  normal confirmation lifecycle; nothing here can approve itself.

## Routes

| Route                         | Notes                                                      |
| ----------------------------- | ---------------------------------------------------------- |
| `GET /mcp/status`             | Verified connection + advertised/callable tools. No token. |
| `GET /mcp/status?probe=false` | Configuration only, no network round trip.                 |

`/mcp/status` is `Protected` (master token or a paired session) and is **not** on
the companion gateway's allowlist. A phone cannot read the connector's endpoint
or catalogue.

## Architecture

```
jarvis/integrations/mcp_client.py   transport, allowlist, status
jarvis/tools/mcp_tools.py           registers one Vantage tool per allowed name
jarvis/api/app.py                   GET /mcp/status
macos/Vantage/Sources/Views/MCPView.swift   the screen
```

## Adding a capability

1. Add the tool's name to `JARVIS_MCP_ALLOWED_TOOLS` and restart.
2. It appears in the registry as `mcp__<name>__<tool>` at `EXTERNAL` risk.
3. Asking the assistant for it produces a confirmation; approve on the machine.

Nothing in the task or calendar code changes. That is the point: Google is
modelled as a typed adapter because Vantage needs typed, deep access, while MCP
covers the long tail of apps without a bespoke adapter each.

## Tests

```bash
.venv/bin/python -m pytest tests/test_mcp_connector.py tests/test_google_tasks_tools_and_mcp.py -q
```

No test contacts a real server. `tests/test_mcp_connector.py` runs a fake
Streamable-HTTP server on an ephemeral `127.0.0.1` port that emits SSE, issues
an `Mcp-Session-Id`, answers a notification with `202`/empty, returns a batch,
and can refuse with `401`/`403` — proving the session id is echoed, the SSE
response for the request id is the one used, the notification is handled, a
`401` is named honestly, and a non-allowlisted tool never reaches the server.
`tests/test_google_tasks_tools_and_mcp.py` drives the `_exchange` seam for the
envelope/status/credential-leak properties.

**No real MCP service is connected on this machine** (`JARVIS_MCP_URL` is
unset). Everything above is verified against a local fake; a live Zapier or n8n
connection is still an unrun step.
