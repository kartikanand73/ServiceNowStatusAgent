# ServiceNow Status/Explain MCP Server (POC)

Deterministic data layer for a ServiceNow ticket-status agent. Foundry handles
conversation routing; this server handles every actual ServiceNow call.

## Tools exposed

| Tool | Type | Purpose |
|---|---|---|
| `resolve_caller` | read | Entra email -> ServiceNow `sys_id` |
| `get_my_tickets` | read | List a caller's incidents |
| `get_ticket_details` | read | Full detail + state glossary for one ticket |
| `add_ticket_comment` | **gated write** | Add a comment. Off unless `ALLOW_TICKET_ACTION=true` |

## 1. Local setup

```bash
cd servicenow-mcp
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env: paste your Client ID / Client Secret from the
# Foundry-MCP-Agent Application Registry record
```

Dependencies are pinned exactly (`mcp==1.9.4`, `httpx==0.27.2`) — this matters.
The standalone `fastmcp` package and unpinned `mcp` ranges both produced a
broken dependency chain that crashed on import in testing. Stick to the
pinned versions unless you've verified a newer combination works end to end,
including an actual server start (`python server.py`), not just a clean
`import`.

Load `.env` into your shell before running (or use `python-dotenv` / your own
loader -- kept out of server.py so you can swap in Key Vault later, same as
the Molina MCP servers).

PowerShell quick option:
```powershell
$env:SN_INSTANCE_URL="https://dev375197.service-now.com"
$env:SN_CLIENT_ID="<client id>"
$env:SN_CLIENT_SECRET="<client secret>"
$env:ALLOW_TICKET_ACTION="false"
python server.py
```

## 2. Smoke test BEFORE wiring into Foundry

Don't skip this -- confirm the OAuth token exchange and Table API calls work
standalone first. Quickest way: run this small script against your live
instance and env vars.

```python
# smoke_test.py
import os, server

user = server.resolve_caller(email="<the email on your ZZZ ZZZ test user>")
print(user)

if user["found"]:
    tickets = server.get_my_tickets(user["sys_id"])
    print(tickets)

    if tickets["tickets"]:
        detail = server.get_ticket_details(tickets["tickets"][0]["number"])
        print(detail)
```

```bash
python smoke_test.py
```

Expected: `resolve_caller` returns your test user's `sys_id`, `get_my_tickets`
returns the 4 incidents you seeded, `get_ticket_details` returns the state +
work notes / hold reason / resolution notes for whichever ticket you pull.

If `resolve_caller` returns `found: False`: check that your `ZZZ ZZZ` user
actually has an email set on the `sys_user` record -- that's the join key.

If the OAuth call 401s: double check `glide.oauth.inbound.client.credential.grant_type.enabled`
is still `true`, and that you copied the Client Secret correctly (it's often
masked by default -- click the eye icon to reveal, don't copy the masked dots).

## 3. Wire into Azure AI Foundry

1. In the Foundry portal, open your agent (or create one) and go to the
   **Tools / Connected MCP servers** section (this may be labeled slightly
   differently depending on your Foundry version -- look for "MCP" or
   "Custom tool" under the agent's tool configuration).
2. Since this server uses **stdio transport**, Foundry needs to run it as a
   subprocess (same as your `ado-deploy-mcp` wiring) rather than connect to
   a URL. If your Foundry setup expects an HTTP/SSE MCP endpoint instead,
   you'll need to front this with the same APIM MCP gateway pattern you used
   for Molina (`mgb-mcp-gateway`) -- wrap `server.py` behind an ASGI adapter
   exposing SSE, then register the APIM endpoint URL in Foundry instead of
   a stdio command.
3. Set the environment variables (`SN_INSTANCE_URL`, `SN_CLIENT_ID`,
   `SN_CLIENT_SECRET`, `ALLOW_TICKET_ACTION`) on whatever compute runs this
   process -- Container App env vars if you're using the same APIM gateway
   pattern as Molina, or local env if testing Foundry against your machine
   first.
4. Give the Foundry agent instructions roughly like:
   > "When a user asks about their ServiceNow tickets, call `resolve_caller`
   > with their email first, then `get_my_tickets`. When they ask about a
   > specific ticket, call `get_ticket_details` and explain the status in
   > plain English using the `state_meaning` field plus any work notes,
   > hold reason, or resolution notes returned. Never call
   > `add_ticket_comment` unless the user explicitly asks to leave a comment
   > or request an update, and confirm the ticket number with them first."

## Notes / what's intentionally NOT in this POC

- No retry/backoff on the httpx calls -- fine for a demo, add before any
  real usage.
- Token cache is in-memory and per-process -- fine for a single demo
  session, not for multi-instance production deployment.
- `STATE_GLOSSARY` is a static dict, not real RAG -- matches "Option 1"
  scope (explain what a state means in general). If you want per-ticket
  reasoning over work notes (Option 2), that's a prompt-level change in the
  Foundry agent instructions, not a change to this server -- the raw work
  notes are already returned by `get_ticket_details`.
