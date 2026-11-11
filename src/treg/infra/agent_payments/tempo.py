"""MPP charge on Tempo, through `pympp` (import name `mpp`)."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import httpx

from . import Lookup, Offer, PaymentRejected, Settlement, Unconfirmed, Verified, micro_to_decimal
from .evm import Chain, ChainUnavailable, moved

CONSUMED_HEADERS = ("authorization",)
# A transaction with no expiring nonce can be included at any later time; a day is how long the
# reconciler waits before it calls one that has not landed failed.
UNBOUNDED_VALIDITY = timedelta(days=1)
# Block timestamps and our clock disagree a little; this is the margin past `valid_before`.
EXPIRY_GRACE = timedelta(seconds=60)
RPC_TIMEOUT_S = 30.0


def _payment_scheme(value: str) -> bool:
    # The HTTP auth scheme name is case-insensitive (RFC 9110 §11.1).
    return value[:8].lower() == "payment "


class MppTempoRail:
    protocol = "mpp"
    consumed_headers = CONSUMED_HEADERS
    stripe_network = "tempo"

    def __init__(self, *, secret: str, realm: str, recipient: str, chain_id: int,
                 rpc_url: str = "", rpc_api_key: str = "", currency: str = "",
                 challenge_ttl_s: int = 120) -> None:
        from mpp.methods.tempo import rpc_url_for_chain
        from mpp.methods.tempo import server as tempo
        from mpp.server import Mpp

        self.network = f"tempo:{chain_id}"
        self.pay_to = recipient.lower()
        self._challenge_ttl = timedelta(seconds=challenge_ttl_s)
        # Tempo's hosted RPC takes its console API key in this header, not in the URL.
        client = (httpx.AsyncClient(headers={"tempo-api-key": rpc_api_key}, timeout=RPC_TIMEOUT_S)
                  if rpc_api_key else None)
        self._chain = Chain(rpc_url or rpc_url_for_chain(chain_id), client=client)
        methods = tempo.charge(
            recipient=recipient, chain_id=chain_id, rpc_url=rpc_url or None, http_client=client,
            currencies=[currency] if currency else None, decimals=6)
        self._mpp = Mpp(methods=methods, realm=realm, secret_key=secret)

    def presented(self, headers: Mapping[str, str]) -> str | None:
        value = headers.get("authorization") or ""
        return value if _payment_scheme(value) else None

    def _request(self, offer: Offer):
        from mpp.server.http import Request

        url = offer.path + (f"?{offer.query}" if offer.query else "")
        return Request(method=offer.method, url=f"https://payment.invalid{url}")

    async def challenge(self, offer: Offer) -> list[tuple[str, str]]:
        handler = self._mpp["tempo/charge"](
            amount=micro_to_decimal(offer.amount_micro), description=offer.description,
            expires_in=self._challenge_ttl)
        result = await handler(self._request(offer))
        return [("WWW-Authenticate", value)
                for value in result.challenge.headers.get_list("www-authenticate")]

    async def verify(self, credential: str, offer: Offer) -> Verified:
        from mpp import Credential
        from mpp.errors import PaymentError

        try:
            parsed = Credential.from_authorization(credential)
        except Exception as exc:
            raise PaymentRejected("malformed_credential", "the Payment credential cannot be parsed") from exc
        echoed = _echoed_request(parsed)
        try:
            validation = await self._mpp.validate_credential(
                parsed, intent="charge", http_request=self._request(offer),
                request={"amount": micro_to_decimal(offer.amount_micro)})
        except PaymentError as exc:
            raise PaymentRejected(_code(exc), str(exc) or exc.title) from exc
        if not validation.payer:
            raise PaymentRejected("verification_failed", "the payment names no payer")
        details = validation.details if isinstance(validation.details, dict) else {}
        tx_hash = str(details.get("transaction_hash") or "")
        if details.get("mode") not in ("push", "pull") or not tx_hash:
            raise PaymentRejected("malformed_credential", "send a signed or broadcast Tempo transfer")
        now = datetime.now(UTC).replace(tzinfo=None)
        if details["mode"] == "push":
            # The payer already broadcast the transfer, so it is settled, never drawn as credit.
            deferrable, expires_at = False, now
        else:
            # Only a recovered secp256k1 sender is authenticated before broadcast; keychain,
            # multisig, P256 and WebAuthn senders are authenticated by the chain at settlement.
            deferrable = details.get("payer_verified") is True
            expires_at = _timestamp(details.get("valid_before")) or now + UNBOUNDED_VALIDITY
        # Both modes key on the transaction hash the chain records, so one transfer is one proof
        # whichever mode presents it and under whichever challenge it is wrapped.
        return Verified(protocol=self.protocol, network=self.network,
                        proof_id=f"mpp:{self.network}:tx:{tx_hash.lower()}",
                        payer=validation.payer.lower(), amount_micro=offer.amount_micro,
                        asset=str(echoed.get("currency") or "").lower(),
                        pay_to=str(echoed.get("recipient") or "").lower(),
                        expires_at=expires_at, deferrable=deferrable, credential=parsed)

    async def settle(self, verified: Verified, offer: Offer) -> Settlement:
        from mpp.errors import PaymentError

        from mpp.errors import InternalPaymentError, PaymentOutcomeUnknownError

        try:
            receipt = await self._mpp.broadcast_credential(
                verified.credential, intent="charge", http_request=self._request(offer),
                request={"amount": micro_to_decimal(offer.amount_micro)})
        except (InternalPaymentError, PaymentOutcomeUnknownError) as exc:
            # pympp raises these once the transaction may have reached the chain.
            raise PaymentRejected("settlement_unknown", str(exc) or exc.title) from exc
        except PaymentError as exc:
            raise PaymentRejected("settlement_failed", str(exc) or exc.title) from exc
        except Exception as exc:
            raise PaymentRejected("settlement_unknown", "the Tempo node did not answer") from exc
        return Settlement(reference=receipt.reference,
                          receipt_headers=(("Payment-Receipt", receipt.to_payment_receipt()),))

    async def find(self, payment: Unconfirmed) -> Lookup:
        tx_hash = payment.proof_id.rsplit(":", 1)[-1]
        try:
            receipt = await self._chain.receipt(tx_hash)
            if receipt is None:
                _, head_time = await self._chain.head()
        except ChainUnavailable as exc:
            return Lookup("pending", reason=str(exc))
        if receipt is not None:
            if moved(receipt, asset=payment.asset, payer=payment.payer, pay_to=payment.pay_to,
                     amount=payment.amount_micro):
                return Lookup("landed", reference=tx_hash)
            return Lookup("absent", reference=tx_hash, reason="transfer_mismatch")
        if head_time > payment.expires_at + EXPIRY_GRACE:
            return Lookup("absent", reason="expired_unsettled")
        return Lookup("pending", reason="not_yet_mined")


def _echoed_request(parsed) -> dict:
    """The challenge request the credential echoes (HMAC-checked by `validate_credential`): the
    token and recipient the transfer must move."""
    raw = parsed.challenge.request or ""
    try:
        echoed = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (ValueError, TypeError) as exc:
        raise PaymentRejected("malformed_credential", "the challenge request cannot be read") from exc
    if not isinstance(echoed, dict):
        raise PaymentRejected("malformed_credential", "the challenge request cannot be read")
    return echoed


def _timestamp(stamp) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(stamp), UTC).replace(tzinfo=None) if stamp else None
    except (OverflowError, OSError, ValueError, TypeError):
        return None


def _code(exc) -> str:
    from mpp.errors import InvalidChallengeError, MalformedCredentialError, PaymentExpiredError

    if isinstance(exc, PaymentExpiredError):
        return "payment_expired"
    if isinstance(exc, InvalidChallengeError):
        return "invalid_challenge"
    if isinstance(exc, MalformedCredentialError):
        return "malformed_credential"
    return "verification_failed"
