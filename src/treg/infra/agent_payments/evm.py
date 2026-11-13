"""Plain JSON-RPC reads of an EVM chain, for finding a payment whose settlement went unanswered."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

# keccak256 of the event signatures (ERC-20 / TIP-20 and EIP-3009).
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TRANSFER_WITH_MEMO_TOPIC = "0x57bc7354aa85aed339e000bccffabbc529466af35f0772c8f8ee1145927de7f0"
AUTHORIZATION_USED_TOPIC = "0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5"

TIMEOUT_S = 15.0
LOG_SPAN = 200


class ChainUnavailable(Exception):
    """The node did not give a usable answer; nothing can be concluded."""


class Chain:
    def __init__(self, rpc_url: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._url = rpc_url
        self._client = client

    async def call(self, method: str, params: list[Any]) -> Any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            if self._client is not None:
                resp = await self._client.post(self._url, json=payload)
            else:
                async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                    resp = await client.post(self._url, json=payload)
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ChainUnavailable(f"{method}: {exc}") from exc
        if not isinstance(body, dict) or "error" in body:
            raise ChainUnavailable(f"{method}: {body.get('error') if isinstance(body, dict) else body}")
        return body.get("result")

    async def receipt(self, tx_hash: str) -> dict | None:
        result = await self.call("eth_getTransactionReceipt", [tx_hash])
        return result if isinstance(result, dict) else None

    async def head(self) -> tuple[int, datetime]:
        return await self.block(None)

    async def block(self, number: int | None) -> tuple[int, datetime]:
        tag = "latest" if number is None else hex(number)
        result = await self.call("eth_getBlockByNumber", [tag, False])
        if not isinstance(result, dict):
            raise ChainUnavailable(f"block {tag} not found")
        stamp = datetime.fromtimestamp(int(result["timestamp"], 16), UTC).replace(tzinfo=None)
        return int(result["number"], 16), stamp

    async def block_at(self, when: datetime) -> int:
        """The last block at or before `when` (naive UTC). Public nodes prune old blocks, so the
        search starts from an estimate near the head, widens until it brackets `when`, then
        bisects."""
        top, top_time = await self.head()
        if top_time <= when:
            return top
        sample = max(0, top - 1_000)
        _, sample_time = await self.block(sample)
        seconds_per_block = max((top_time - sample_time).total_seconds() / max(top - sample, 1), 0.05)
        guess = max(0, top - int((top_time - when).total_seconds() / seconds_per_block))
        low, high, width = guess, guess, 64
        while low > 0 and (await self.block(low))[1] > when:
            high, low, width = low - 1, max(0, low - width), width * 2
        while high < top and (await self.block(high + 1))[1] <= when:
            low, high, width = high + 1, min(top, high + width), width * 2
        while low < high:
            mid = (low + high + 1) // 2
            if (await self.block(mid))[1] <= when:
                low = mid
            else:
                high = mid - 1
        return low

    async def logs(self, *, address: str, topics: list[str | None], from_block: int,
                   to_block: int) -> list[dict]:
        """Matching logs, asked in spans of `LOG_SPAN` blocks (public nodes refuse wider ranges);
        stops at the first span that has any."""
        start = from_block
        while start <= to_block:
            end = min(to_block, start + LOG_SPAN - 1)
            result = await self.call("eth_getLogs", [{
                "address": address, "topics": topics, "fromBlock": hex(start), "toBlock": hex(end)}])
            if isinstance(result, list) and result:
                return result
            start = end + 1
        return []


def topic_address(address: str) -> str:
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


def moved(receipt: dict, *, asset: str, payer: str, pay_to: str, amount: int) -> bool:
    """True when a successful receipt carries a transfer of exactly `amount` of `asset` from
    `payer` to `pay_to` (a plain Transfer, or TIP-20's TransferWithMemo)."""
    if receipt.get("status") != "0x1":
        return False
    for log in receipt.get("logs") or []:
        topics = [str(t).lower() for t in log.get("topics") or []]
        if str(log.get("address", "")).lower() != asset.lower() or len(topics) < 3:
            continue
        if topics[0] not in (TRANSFER_TOPIC, TRANSFER_WITH_MEMO_TOPIC):
            continue
        if topics[1] != topic_address(payer) or topics[2] != topic_address(pay_to):
            continue
        data = str(log.get("data") or "0x")
        if len(data) >= 66 and int(data[2:66], 16) == amount:
            return True
    return False
