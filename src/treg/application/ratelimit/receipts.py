"""The blocklist write receipt: a Slack message the server posts after every successful write.

Best-effort by construction: `send` only schedules a task after the write has committed and
published, so a slow or failing Slack can neither delay the response nor undo the write. A failed
post is counted (`blocklist_receipt_failures`) and logged. The token comes from
`TREG_BLOCKLIST_RECEIPT_SLACK_TOKEN`; both it and the channel empty = no receipt.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from ...config import get_settings
from . import metrics

log = logging.getLogger("treg.ratelimit")
_SLACK_POST = "https://slack.com/api/chat.postMessage"
_TIMEOUT_S = 5.0
_tasks: set[asyncio.Task] = set()


def _text(action: str, result: dict) -> str:
    lines = [f"treg blocklist {action}: version {result['version']}, audit {result['audit_id']}"]
    if action == "block":
        lines.append(f"TTL {result['ttl_s']} s (auto-unblock)")
    for e in result["entries"][:50]:
        lines.append(f"- {e['type']} {e['value'][:16]} (entry {e['id']}) until {e['expires_at']}: {e['reason']}")
    if len(result["entries"]) > 50:
        lines.append(f"... and {len(result['entries']) - 50} more")
    return "\n".join(lines)


async def post(token: str, channel: str, text: str) -> None:
    """One Slack Web API call; raises on any failure."""
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        r = await client.post(_SLACK_POST, headers={"Authorization": f"Bearer {token}"},
                              json={"channel": channel, "text": text})
    body = r.json()
    if r.status_code != 200 or not body.get("ok"):
        raise RuntimeError(f"slack {r.status_code}: {body.get('error')}")


async def _deliver(action: str, result: dict) -> None:
    s = get_settings()
    try:
        await post(s.blocklist_receipt_slack_token, s.blocklist_receipt_slack_channel, _text(action, result))
        metrics.count("blocklist_receipts_sent")
    except Exception as exc:  # noqa: BLE001 - a receipt never changes the write
        metrics.count("blocklist_receipt_failures")
        log.error("rate_limit_blocklist_receipt_failed audit=%s error=%s: %s",
                  result.get("audit_id"), type(exc).__name__, exc)


def send(action: str, result: dict) -> None:
    s = get_settings()
    if not (s.blocklist_receipt_slack_token and s.blocklist_receipt_slack_channel):
        metrics.count("blocklist_receipts_unconfigured")
        return
    task = asyncio.get_running_loop().create_task(_deliver(action, result))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def drain() -> None:
    if _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
