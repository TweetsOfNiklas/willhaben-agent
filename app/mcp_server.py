"""MCP server (stdio) that exposes the running willhaben Agent to Claude Desktop / Claude Code.

It is a thin client of the agent's JSON API, so it can run on any machine:

  WH_AGENT_URL=http://homeserver:8010 WH_AGENT_PASSWORD=... python -m app.mcp_server

Claude Code:   claude mcp add willhaben -e WH_AGENT_URL=http://homeserver:8010 -- uv --directory <repo> run python -m app.mcp_server
Claude Desktop: see README (claude_desktop_config.json).
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

# WH_HELPER_* are the pre-rename names; still accepted so existing MCP configs keep working.
BASE = (os.environ.get("WH_AGENT_URL") or os.environ.get("WH_HELPER_URL") or "http://127.0.0.1:8000").rstrip("/")
_PW = os.environ.get("WH_AGENT_PASSWORD") or os.environ.get("WH_HELPER_PASSWORD")
AUTH = (os.environ.get("WH_AGENT_USER") or os.environ.get("WH_HELPER_USER") or "admin", _PW) if _PW else None

mcp = MCPServer(
    "willhaben-agent",
    instructions=(
        "Access to your willhaben Agent: search on willhaben.at and the other second-hand sites it knows "
        "(list_platforms), your search agents and their hits, willhaben chats with reply drafts, your own ads. "
        "Messages are only sent with send_reply; always ask for confirmation of the exact text first."
    ),
)

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)


async def _call(method: str, path: str, **kw: Any) -> Any:
    async with httpx.AsyncClient(base_url=BASE, auth=AUTH, timeout=300,
                                 headers={"x-wh-client-kind": "mcp"}) as c:   # timeline: actor "mcp"
        r = await c.request(method, path, **kw)
    try:
        body = r.json()
    except ValueError:
        body = r.text
    if r.status_code >= 400:
        return {"error": r.status_code, "detail": body}
    return body


@mcp.tool(annotations=READ)
async def status() -> dict:
    """Login status of the willhaben session, unread messages and new hits."""
    return await _call("GET", "/api/status")


@mcp.tool(annotations=READ)
async def search_willhaben(keyword: str, price_from: float | None = None, price_to: float | None = None,
                           rows: int = 20, sort: int = 1) -> dict:
    """Live search on the willhaben marketplace. sort: 1 newest, 3 price ascending, 4 price descending, 7 relevance."""
    params = {"keyword": keyword, "rows": rows, "sort": sort}
    if price_from is not None:
        params["price_from"] = price_from
    if price_to is not None:
        params["price_to"] = price_to
    return await _call("GET", "/api/search", params=params)


@mcp.tool(annotations=READ)
async def list_platforms() -> list:
    """The second-hand sites the agent knows (willhaben, kleinanzeigen, …), whether each is switched on and what it can do."""
    return await _call("GET", "/api/platforms")


@mcp.tool(annotations=READ)
async def search_listings(platform: str, keyword: str, price_from: float | None = None, price_to: float | None = None,
                          rows: int = 20, sort: int = 1) -> dict:
    """Live keyword search on one site (platform id from list_platforms). sort: 1 newest, 3 price ascending,
    4 price descending, 7 relevance."""
    params = {"platform": platform, "keyword": keyword, "rows": rows, "sort": sort}
    if price_from is not None:
        params["price_from"] = price_from
    if price_to is not None:
        params["price_to"] = price_to
    return await _call("GET", "/api/search", params=params)


@mcp.tool(annotations=READ)
async def get_ad(ad_id: str, platform: str = "willhaben") -> dict:
    """Details of an ad (title, price, description, location, seller); `platform` for sites other than willhaben."""
    return await _call("GET", f"/api/ads/{platform}/{ad_id}")


@mcp.tool(annotations=READ)
async def list_search_agents() -> list:
    """All of your search agents with interval and last run."""
    return await _call("GET", "/api/agents")


@mcp.tool()
async def create_search_agent(url: str | None = None, name: str | None = None, interval_minutes: int = 5,
                              platform: str | None = None, keyword: str | None = None, price_from: float | None = None,
                              price_to: float | None = None, willhaben_url: str | None = None,
                              sites: list[str] | None = None) -> dict:
    """Create a search agent from a result-list URL of any supported site (set the filters in the browser first),
    or from a keyword with an optional price range on one platform (list_platforms). `sites`: one agent watching
    several platforms (the URL, if given, is the main search; the other sites get its keyword)."""
    return await _call("POST", "/api/agents",
                       json={"url": url or willhaben_url, "platform": platform or "willhaben", "keyword": keyword,
                             "price_from": price_from, "price_to": price_to, "name": name,
                             "interval_s": interval_minutes * 60, "sites": sites})


@mcp.tool(annotations=READ)
async def list_hits(agent_id: int | None = None, state: str = "new", limit: int = 20) -> list:
    """Hits of the search agents. state: new, saved, seen, ignored, filtered (held back by the screening) or all."""
    params: dict[str, Any] = {"state": state, "limit": limit}
    if agent_id:
        params["agent_id"] = agent_id
    return await _call("GET", "/api/hits", params=params)


@mcp.tool(annotations=READ)
async def list_conversations() -> list:
    """willhaben chats (as last synced) with partner, ad, last message and unread count."""
    return await _call("GET", "/api/chats")


@mcp.tool(annotations=READ)
async def get_conversation(conversation_id: str) -> dict:
    """One conversation with ad context, message history and existing drafts."""
    return await _call("GET", f"/api/chats/{conversation_id}")


@mcp.tool()
async def draft_reply(conversation_id: str, hint: str | None = None) -> dict:
    """Have the agent write a reply draft (uses your rules from the settings). Sends nothing."""
    return await _call("POST", f"/api/chats/{conversation_id}/draft", json={"hint": hint})


@mcp.tool()
async def save_reply_draft(conversation_id: str, text: str) -> dict:
    """Store a reply text you wrote yourself as a draft in the dashboard. Sends nothing."""
    return await _call("POST", f"/api/chats/{conversation_id}/drafts", json={"text": text})


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True, open_world_hint=True))
async def send_reply(draft_id: int, text: str) -> dict:
    """Sends a draft as a willhaben message. Only call this after the text has been explicitly confirmed."""
    return await _call("POST", f"/api/drafts/{draft_id}/send", json={"text": text})


@mcp.tool(annotations=READ)
async def list_my_ads() -> list:
    """Your own monitored ads with status, expiry date and auto-reactivation."""
    return await _call("GET", "/api/myads")


@mcp.tool()
async def track_my_ad(ad_id_or_url: str, auto_renew: bool = False) -> dict:
    """Add one of your own ads to expiry monitoring."""
    return await _call("POST", "/api/myads", json={"ref": ad_id_or_url, "auto_renew": auto_renew})


if __name__ == "__main__":
    mcp.run("stdio")
