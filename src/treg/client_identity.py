"""Framework-neutral normalization for self-reported client identities."""

import re


def _norm_client(raw: str) -> str:
    """A runtime name as a short slug. One spelling for both ends of the roster: what an incoming
    header is stored as, and what `promoted_from` must match to hide a detected pair."""
    return re.sub(r"[^a-z0-9-]", "",
                  raw.strip().lower().split("/", 1)[0])[:32]  # "claude-code/1.2" → "claude-code"


MCP_CLIENTS = frozenset({"mcp", "claude-connector"})


def printable_ascii(value: str, limit: int | None = None) -> str:
    return "".join(c for c in value if " " <= c <= "~").strip()[:limit]


def mcp_host(headers, client: str) -> dict:
    """The MCP host behind an in-process call, read only on the MCP surfaces' own traffic."""
    if client not in MCP_CLIENTS:
        return {"mcp_client": None, "mcp_user_agent": None}
    return {"mcp_client": printable_ascii(headers.get("X-Treg-Mcp-Client", ""), 64) or None,
            "mcp_user_agent": printable_ascii(headers.get("X-Treg-Mcp-User-Agent", ""), 100) or None}
