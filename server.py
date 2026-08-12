"""
ServiceNow Status/Explain + Action MCP Server
-----------------------------------------------
Deterministic data-layer tools for a ServiceNow ticket status agent.
Follows the same pattern as github-deploy-mcp / ado-deploy-mcp:
  - Deterministic Python calls to the ServiceNow Table API (no LLM in this layer)
  - Read tools always on
  - Action tool gated behind ALLOW_TICKET_ACTION env var, off by default

Transport: stdio (same as ado-deploy-mcp / Databricks Genie MCP server)
"""

import os
import time
import httpx
from typing import Optional
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config (env-driven, same convention as your other MCP servers)
# ---------------------------------------------------------------------------
SN_INSTANCE_URL = os.environ["SN_INSTANCE_URL"].rstrip("/")   # e.g. https://dev375197.service-now.com
SN_CLIENT_ID = os.environ["SN_CLIENT_ID"]
SN_CLIENT_SECRET = os.environ["SN_CLIENT_SECRET"]
ALLOW_TICKET_ACTION = os.environ.get("ALLOW_TICKET_ACTION", "false").lower() == "true"

mcp = FastMCP("servicenow-status-agent", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))

# ---------------------------------------------------------------------------
# OAuth token cache (client_credentials grant) - simple in-memory cache,
# same pattern you'd use for any short-lived bearer token client
# ---------------------------------------------------------------------------
_token_cache = {"access_token": None, "expires_at": 0}


def _get_access_token() -> str:
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 30:
        return _token_cache["access_token"]

    resp = httpx.post(
        f"{SN_INSTANCE_URL}/oauth_token.do",
        data={
            "grant_type": "client_credentials",
            "client_id": SN_CLIENT_ID,
            "client_secret": SN_CLIENT_SECRET,
        },
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()
    _token_cache["access_token"] = payload["access_token"]
    _token_cache["expires_at"] = now + int(payload.get("expires_in", 1800))
    return _token_cache["access_token"]


def _sn_get(path: str, params: dict) -> dict:
    token = _get_access_token()
    resp = httpx.get(
        f"{SN_INSTANCE_URL}/api/now/{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _sn_patch(path: str, body: dict) -> dict:
    token = _get_access_token()
    resp = httpx.patch(
        f"{SN_INSTANCE_URL}/api/now/{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Small status glossary (Option 1 layer). Kept static/deterministic on
# purpose -- this is NOT the LLM's job, it's a lookup table.
# Extend this table over time; it's the cheapest ROI RAG substitute for a POC.
# ---------------------------------------------------------------------------
STATE_LABELS = {
    "1": "New",
    "2": "In Progress",
    "3": "On Hold",
    "6": "Resolved",
    "7": "Closed",
    "8": "Canceled",
}

STATE_GLOSSARY = {
    "1": "The ticket has just been created and hasn't been picked up by a support agent yet.",  # New
    "2": "A support agent is actively working the ticket.",  # In Progress
    "3": "Work is paused, usually waiting on something from you or another team (see the hold reason).",  # On Hold
    "6": "A fix has been applied. The ticket will auto-close after a few days unless you reopen it.",  # Resolved
    "7": "The ticket is fully closed.",  # Closed
    "8": "The ticket was closed without a resolution (cancelled).",  # Canceled
}


# ---------------------------------------------------------------------------
# Tool 1: Resolve a Teams/Entra identity to a ServiceNow caller_id
# ---------------------------------------------------------------------------
@mcp.tool()
def resolve_caller(email: str) -> dict:
    """
    Resolve a user's email (from Entra ID / Teams identity) to their
    ServiceNow sys_user record (sys_id + name). Call this first, before
    fetching tickets, so you only ever query tickets for the authenticated user.
    """
    data = _sn_get(
        "table/sys_user",
        {"sysparm_query": f"email={email}", "sysparm_fields": "sys_id,name,email", "sysparm_limit": 1},
    )
    results = data.get("result", [])
    if not results:
        return {"found": False, "email": email}
    user = results[0]
    return {"found": True, "sys_id": user["sys_id"], "name": user["name"], "email": user["email"]}


# ---------------------------------------------------------------------------
# Tool 2: List a user's tickets
# ---------------------------------------------------------------------------
@mcp.tool()
def get_my_tickets(caller_sys_id: str, limit: int = 10) -> dict:
    """
    Return the caller's incidents, most recently updated first.
    caller_sys_id comes from resolve_caller().
    """
    data = _sn_get(
        "table/incident",
        {
            "sysparm_query": f"caller_id={caller_sys_id}^ORDERBYDESCsys_updated_on",
            "sysparm_fields": "number,short_description,state,sys_updated_on,sys_id",
            "sysparm_limit": limit,
        },
    )
    tickets = data.get("result", [])
    for t in tickets:
        t["state_label"] = STATE_LABELS.get(t.get("state", ""), t.get("state", ""))
    return {"tickets": tickets}


# ---------------------------------------------------------------------------
# Tool 3: Full detail + explanation-ready fields for one ticket
# ---------------------------------------------------------------------------
@mcp.tool()
def get_ticket_details(ticket_number: str) -> dict:
    """
    Return full status detail for a single incident by number (e.g. INC0010001),
    including work notes, hold reason, and resolution notes -- whichever apply
    to its current state. Use this before explaining a ticket's status to a user.
    """
    data = _sn_get(
        "table/incident",
        {
            "sysparm_query": f"number={ticket_number}",
            "sysparm_fields": (
                "number,short_description,state,hold_reason,work_notes,"
                "close_notes,close_code,sys_updated_on,opened_at,assignment_group"
            ),
            "sysparm_limit": 1,
        },
    )
    results = data.get("result", [])
    if not results:
        return {"found": False, "ticket_number": ticket_number}

    ticket = results[0]
    state = ticket.get("state", "")
    return {
        "found": True,
        "ticket": ticket,
        "state_label": STATE_LABELS.get(state, state),
        "state_meaning": STATE_GLOSSARY.get(state, "Status detail not in glossary yet."),
    }


@mcp.tool()
def get_similar_resolved_tickets(keyword: str, limit: int = 5) -> dict:
    """
    Find past RESOLVED incidents whose short description contains the given
    keyword (e.g. 'VPN', 'password', 'slow'). Returns each match's resolution
    notes so the agent can explain what typically fixes this kind of issue.

    This is a keyword-match retrieval layer (Option A). It intentionally does
    NOT do semantic/embedding search -- that's a documented next iteration
    (Option B: vector similarity over ticket descriptions + resolution notes,
    same pattern as BeyondRAG's hybrid RAG). Keeping this deterministic and
    query-based, not LLM-based, matches the rest of this server's design.
    """
    data = _sn_get(
        "table/incident",
        {
            "sysparm_query": (
                f"short_descriptionLIKE{keyword}^state=6^ORDERBYDESCsys_updated_on"
            ),
            "sysparm_fields": "number,short_description,close_code,close_notes,sys_updated_on",
            "sysparm_limit": limit,
        },
    )
    return {"keyword": keyword, "similar_resolved_tickets": data.get("result", [])}


# ---------------------------------------------------------------------------
# Tool 4 (GATED): Add a comment / request update on a ticket
# ---------------------------------------------------------------------------
@mcp.tool()
def add_ticket_comment(ticket_sys_id: str, comment: str) -> dict:
    """
    Add a customer-visible comment to a ticket (e.g. 'please provide an update').
    This is a WRITE action and is disabled unless ALLOW_TICKET_ACTION=true is
    set on the MCP server. Mirrors the ALLOW_DEPLOY_TRIGGER gating pattern.
    """
    if not ALLOW_TICKET_ACTION:
        return {
            "success": False,
            "reason": "Action tools are disabled. Set ALLOW_TICKET_ACTION=true to enable.",
        }

    result = _sn_patch(f"table/incident/{ticket_sys_id}", {"comments": comment})
    return {"success": True, "result": result.get("result", {})}


if __name__ == "__main__":
    # streamable-http so Azure AI Foundry (or any remote MCP client) can
    # connect over a URL instead of spawning this as a local subprocess.
    mcp.run(transport="streamable-http")
