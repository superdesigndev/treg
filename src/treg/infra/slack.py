"""Slack incoming webhooks: post one plain-text message to the channel the URL belongs to."""

import httpx


async def post(webhook_url: str, text: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
    async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
        response = await client.post(webhook_url, json={"text": text})
        response.raise_for_status()
