"""Nano (`nano:mainnet`, scheme `exact`): the payer sends XNO to treg's account and presents the
block hash; treg reads that block from the operator's node and credits it once.

A Nano block has no memo, so the payment is bound to its request through the block's
`representative` field (xno-binding v1, option C): the challenge names an order id, HMAC-bound to
the route, the amount and the recipient, and the account `SHA256("xno-binding/v1/rep|" + order +
"|" + pay_to + "|" + raw)` that the payer's send block must set as its representative. A block
paying the right amount to treg's account for another request carries another representative.
The block hash is the `proof_id`, so `AgentPayment.proof_id`'s UNIQUE key refuses a second use of
the same block, across restarts and instances.

The money moved before the request was sent (as MPP's push mode), so a Nano payment is never
deferrable and has no zero-amount proof. Amounts: an offer's micro-USD is converted to raw (1 XNO
= 10**30 raw) at the operator's configured XNO-per-USD rate, rounded up. No key or seed is held
here: treg only reads blocks. Standard library only; the node is a JSON-RPC URL (`block_info`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import Any, Protocol

from . import Lookup, Offer, PaymentRejected, Settlement, Unconfirmed, Verified

NETWORK = "nano:mainnet"
ASSET = "xno"
RAW_PER_XNO = 10 ** 30
MICRO_PER_USD = 10 ** 6

CHALLENGE_HEADER = "X-NANO-PAYMENT-REQUIRED"
PAYMENT_HEADER = "x-nano-payment"
RESPONSE_HEADER = "X-NANO-PAYMENT-RESPONSE"
CONSUMED_HEADERS = (PAYMENT_HEADER,)
BINDING = "xno-binding/v1/rep"

# A send block's node time may run a little past the challenge's expiry.
EXPIRY_GRACE = timedelta(seconds=60)
# How long after its expiry a paid challenge may still be presented (a slow confirmation).
PRESENT_GRACE = timedelta(seconds=600)
TIMEOUT_S = 15.0

_ALPHABET = "13456789abcdefghijkmnopqrstuwxyz"
_ADDRESS = re.compile(r"(?:nano|xrb)_([13][13456789abcdefghijkmnopqrstuwxyz]{59})")
_HASH = re.compile(r"[0-9A-Fa-f]{64}")
_RAW = re.compile(r"[0-9]{1,40}")


# --- accounts -------------------------------------------------------------------------------

def _b32(value: int, chars: int) -> str:
    out = []
    for _ in range(chars):
        out.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(out))


def _unb32(text: str) -> int:
    value = 0
    for char in text:
        value = value * 32 + _ALPHABET.index(char)
    return value


def _checksum(public_key: bytes) -> bytes:
    return hashlib.blake2b(public_key, digest_size=5).digest()[::-1]


def public_key_of(address: str) -> bytes:
    """The 32-byte public key of a `nano_` (or legacy `xrb_`) address; ValueError when the address
    is malformed or its checksum is wrong."""
    match = _ADDRESS.fullmatch(address.strip().lower()) if isinstance(address, str) else None
    if match is None:
        raise ValueError("not a nano_ address")
    body = match.group(1)
    key = _unb32(body[:52])
    if key >> 256:
        raise ValueError("not a nano_ address")
    public_key = key.to_bytes(32, "big")
    if _unb32(body[52:]).to_bytes(5, "big") != _checksum(public_key):
        raise ValueError("the address checksum is wrong")
    return public_key


def address_of(public_key: bytes) -> str:
    if len(public_key) != 32:
        raise ValueError("a public key is 32 bytes")
    return ("nano_" + _b32(int.from_bytes(public_key, "big"), 52)
            + _b32(int.from_bytes(_checksum(public_key), "big"), 8))


def normalize(address: str) -> str:
    """The checksum-validated `nano_` form of an address."""
    return address_of(public_key_of(address))


def same_account(a: Any, b: str) -> bool:
    try:
        return public_key_of(str(a)) == public_key_of(b)
    except ValueError:
        return False


# --- amounts and binding --------------------------------------------------------------------

def parse_rate(xno_per_usd: str) -> Fraction:
    """`TREG_AGENT_PAYMENTS_NANO_XNO_PER_USD` as an exact positive fraction ("1.25" -> 5/4)."""
    try:
        rate = Fraction(str(xno_per_usd).strip())
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError("the XNO per USD rate is not a decimal number") from exc
    if rate <= 0:
        raise ValueError("the XNO per USD rate must be positive")
    return rate


def raw_for(amount_micro: int, rate: Fraction) -> int:
    """The raw that pays `amount_micro` micro-USD at `rate` XNO per USD, rounded up to whole raw."""
    if amount_micro <= 0:
        raise ValueError("amount must be positive")
    exact = Fraction(amount_micro, MICRO_PER_USD) * rate * RAW_PER_XNO
    return -(-exact.numerator // exact.denominator)


def representative_for(order: str, pay_to: str, raw: int) -> str:
    """xno-binding v1 option C: the representative account a send block for `order` must name."""
    message = f"{BINDING}|{order}|{pay_to}|{raw}".encode()
    return address_of(hashlib.sha256(message).digest())


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")


def _unb64(text: str) -> dict:
    padded = text.strip() + "=" * (-len(text.strip()) % 4)
    value = json.loads(base64.urlsafe_b64decode(padded.encode()))
    if not isinstance(value, dict):
        raise ValueError("not an object")
    return value


# --- the node -------------------------------------------------------------------------------

class NodeUnavailable(Exception):
    """The node did not give a usable answer; nothing can be concluded."""


class BlockReader(Protocol):
    async def block_info(self, block_hash: str) -> dict | None: ...


class Node:
    """`block_info` from a Nano node's JSON-RPC (the operator's own, or a public one it trusts)."""

    def __init__(self, url: str, *, client: Any = None) -> None:
        self._url = url
        self._client = client

    async def block_info(self, block_hash: str) -> dict | None:
        import httpx

        payload = {"action": "block_info", "json_block": "true", "hash": block_hash}
        try:
            if self._client is not None:
                resp = await self._client.post(self._url, json=payload)
            else:
                async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                    resp = await client.post(self._url, json=payload)
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise NodeUnavailable(f"block_info: {exc}") from exc
        if not isinstance(body, dict):
            raise NodeUnavailable("block_info: not an object")
        if "error" in body:
            if "not found" in str(body["error"]).lower():
                return None
            raise NodeUnavailable(f"block_info: {body['error']}")
        return body


# --- the rail -------------------------------------------------------------------------------

class NanoRail:
    protocol = "nano"
    network = NETWORK
    stripe_network = None
    consumed_headers = CONSUMED_HEADERS
    deferrable_payments = False

    def __init__(self, *, pay_to: str, secret: str, xno_per_usd: str, node_url: str = "",
                 challenge_ttl_s: int = 600, node: BlockReader | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.pay_to = normalize(pay_to)
        if len(secret) < 32:
            raise ValueError("the challenge secret must be 32+ characters")
        if node is None and not node_url:
            raise ValueError("a Nano node URL is required")
        self._secret = secret.encode()
        self._rate = parse_rate(xno_per_usd)
        self._ttl = timedelta(seconds=challenge_ttl_s)
        self._node = node or Node(node_url)
        self._clock = clock or (lambda: datetime.now(UTC))

    def presented(self, headers: Mapping[str, str]) -> str | None:
        return headers.get(PAYMENT_HEADER) or None

    def proves(self, offer: Offer) -> bool:
        return False

    def raw(self, offer: Offer) -> int:
        return raw_for(offer.amount_micro, self._rate)

    def _mac(self, nonce: str, expires: int, offer: Offer, raw: int) -> str:
        message = "|".join(("treg-nano/v1", nonce, str(expires), offer.method, offer.resource_url,
                            str(raw), self.pay_to)).encode()
        digest = hmac.new(self._secret, message, hashlib.sha256).digest()[:16]
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def _order(self, offer: Offer, raw: int) -> str:
        nonce = secrets.token_hex(16)
        expires = int((self._clock() + self._ttl).timestamp())
        return f"{nonce}.{expires}.{self._mac(nonce, expires, offer, raw)}"

    async def challenge(self, offer: Offer, *, pay: bool = True,
                        prove: bool = False) -> list[tuple[str, str]]:
        if not pay:
            return []
        raw = self.raw(offer)
        order = self._order(offer, raw)
        required = {
            "x402Version": 2, "error": "payment required",
            "resource": {"url": offer.resource_url, "description": offer.description},
            "accepts": [{
                "scheme": "exact", "network": self.network, "asset": "XNO", "amount": str(raw),
                "payTo": self.pay_to, "maxTimeoutSeconds": int(self._ttl.total_seconds()),
                "extra": {"order": order, "representative": representative_for(order, self.pay_to, raw),
                          "binding": BINDING, "decimals": 30,
                          "credential": f"{PAYMENT_HEADER}: base64url JSON {{order, block}}"}}],
        }
        return [(CHALLENGE_HEADER, _b64(required))]

    def _check_order(self, order: str, offer: Offer, raw: int) -> datetime:
        try:
            nonce, expires_text, mac = order.split(".")
            expires = int(expires_text)
        except (AttributeError, ValueError) as exc:
            raise PaymentRejected("malformed_credential", "the Nano order cannot be read") from exc
        if not hmac.compare_digest(mac, self._mac(nonce, expires, offer, raw)):
            raise PaymentRejected("invalid_challenge", "this payment was not issued for this request")
        expires_at = datetime.fromtimestamp(expires, UTC)
        if self._clock() > expires_at + PRESENT_GRACE:
            raise PaymentRejected("invalid_challenge", "this payment's challenge has expired")
        return expires_at

    async def verify(self, credential: str, offer: Offer, *,
                     before_remote: Callable[[], Awaitable[None]] | None = None) -> Verified:
        try:
            presented = _unb64(credential)
            order, block_hash = str(presented["order"]), str(presented["block"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PaymentRejected("malformed_credential", "X-NANO-PAYMENT cannot be decoded") from exc
        if not _HASH.fullmatch(block_hash):
            raise PaymentRejected("malformed_credential", "the block hash is not 64 hex digits")
        raw = self.raw(offer)
        expires_at = self._check_order(order, offer, raw)
        if before_remote is not None:
            await before_remote()
        try:
            info = await self._node.block_info(block_hash.upper())
        except NodeUnavailable as exc:
            raise PaymentRejected("verification_unavailable", "the Nano node could not be read") from exc
        payer = self._check_block(info, raw, representative_for(order, self.pay_to, raw), expires_at)
        return Verified(protocol=self.protocol, network=self.network,
                        proof_id=f"nano:{self.network}:{block_hash.lower()}", payer=payer,
                        amount_micro=offer.amount_micro, asset=ASSET, pay_to=self.pay_to,
                        expires_at=(expires_at + EXPIRY_GRACE).replace(tzinfo=None),
                        deferrable=False, credential=block_hash.upper())

    def _check_block(self, info: dict | None, raw: int, representative: str,
                     expires_at: datetime) -> str:
        """The payer's address when `info` is a confirmed send of exactly `raw` to `pay_to` that
        names `representative`, sent before the challenge expired."""
        if not info:
            raise PaymentRejected("verification_failed", "the node does not know this block")
        contents = info.get("contents") if isinstance(info.get("contents"), dict) else {}
        if str(info.get("confirmed", "")).lower() != "true":
            raise PaymentRejected("payment_unconfirmed",
                                  "the block is not confirmed yet; send the request again shortly")
        if info.get("subtype") != "send" or contents.get("type") != "state":
            raise PaymentRejected("verification_failed", "the block is not a send")
        destination = contents.get("link_as_account") or info.get("link_as_account")
        if not same_account(destination, self.pay_to):
            raise PaymentRejected("verification_failed", "the block does not pay this recipient")
        amount = str(info.get("amount", ""))
        if not _RAW.fullmatch(amount) or int(amount) != raw:
            raise PaymentRejected("verification_failed", "the block does not pay the offered amount")
        if not same_account(contents.get("representative"), representative):
            raise PaymentRejected("invalid_challenge", "the block was not sent for this request")
        try:
            sent_at = datetime.fromtimestamp(int(info.get("local_timestamp") or 0), UTC)
        except (TypeError, ValueError, OverflowError) as exc:
            raise PaymentRejected("verification_failed", "the block has no time") from exc
        if sent_at.timestamp() <= 0 or sent_at > expires_at + EXPIRY_GRACE:
            raise PaymentRejected("verification_failed", "the block was sent after the challenge expired")
        try:
            return normalize(str(info.get("block_account") or contents.get("account") or ""))
        except ValueError as exc:
            raise PaymentRejected("verification_failed", "the block names no valid sender") from exc

    async def settle(self, verified: Verified, offer: Offer) -> Settlement:
        """The payer already moved the money and `verify` read it confirmed: nothing to submit."""
        response = {"success": True, "network": self.network, "transaction": verified.credential,
                    "payer": verified.payer}
        return Settlement(reference=verified.credential,
                          receipt_headers=((RESPONSE_HEADER, _b64(response)),))

    async def find(self, payment: Unconfirmed) -> Lookup:
        """A claimed block whose credit went unrecorded: the chain says whether it still pays us."""
        block_hash = payment.proof_id.rsplit(":", 1)[-1].upper()
        try:
            info = await self._node.block_info(block_hash)
        except NodeUnavailable as exc:
            return Lookup("pending", reason=str(exc))
        if not info:
            return Lookup("pending", reason="block_unknown")
        if str(info.get("confirmed", "")).lower() != "true":
            return Lookup("pending", reason="not_confirmed")
        contents = info.get("contents") if isinstance(info.get("contents"), dict) else {}
        destination = contents.get("link_as_account") or info.get("link_as_account")
        if info.get("subtype") == "send" and same_account(destination, payment.pay_to) \
                and same_account(info.get("block_account") or contents.get("account"), payment.payer):
            return Lookup("landed", reference=block_hash)
        return Lookup("absent", reference=block_hash, reason="transfer_mismatch")
