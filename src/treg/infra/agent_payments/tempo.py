"""MPP charge on Tempo, through `pympp` (import name `mpp`)."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping

from . import Offer, PaymentRejected, Settlement, Verified, micro_to_decimal

CONSUMED_HEADERS = ("authorization", "payment-authorization")


def _payment_scheme(value: str) -> bool:
    # The HTTP auth scheme name is case-insensitive (RFC 9110 §11.1).
    return value[:8].lower() == "payment "


class MppTempoRail:
    protocol = "mpp"
    consumed_headers = CONSUMED_HEADERS

    def __init__(self, *, secret: str, realm: str, recipient: str, chain_id: int,
                 rpc_url: str = "", currency: str = "") -> None:
        from mpp.methods.tempo import server as tempo
        from mpp.server import Mpp

        self.network = f"tempo:{chain_id}"
        methods = tempo.charge(
            recipient=recipient, chain_id=chain_id, rpc_url=rpc_url or None,
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
            amount=micro_to_decimal(offer.amount_micro), description=offer.description)
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
        self._check_binding(parsed, offer)
        try:
            validation = await self._mpp.validate_credential(
                parsed, intent="charge",
                request={"amount": micro_to_decimal(offer.amount_micro)})
        except PaymentError as exc:
            raise PaymentRejected(_code(exc), str(exc) or exc.title) from exc
        if not validation.payer:
            raise PaymentRejected("verification_failed", "the payment names no payer")
        payload = parsed.payload if isinstance(parsed.payload, dict) else {}
        if payload.get("type") == "hash":
            # Push mode: the payer already broadcast the transfer, so the hash is the proof.
            proof, deferrable = f"mpp:{self.network}:tx:{str(payload.get('hash')).lower()}", False
        else:
            # A signed transaction can be wrapped in a fresh challenge; one use per transaction.
            signed = str(payload.get("signature") or "").lower().encode()
            proof, deferrable = f"mpp:{self.network}:{hashlib.sha256(signed).hexdigest()}", True
        return Verified(protocol=self.protocol, network=self.network, proof_id=proof,
                        payer=validation.payer.lower(), amount_micro=offer.amount_micro,
                        deferrable=deferrable, credential=parsed)

    def _check_binding(self, parsed, offer: Offer) -> None:
        """The echoed challenge is HMAC-authenticated by `validate_credential`; its signed route
        scope must name this route, or a payment for one query could be spent on another."""
        raw = parsed.challenge.request or ""
        try:
            echoed = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        except (ValueError, TypeError) as exc:
            raise PaymentRejected("malformed_credential", "the challenge request cannot be read") from exc
        if not isinstance(echoed, dict) or echoed.get("_mppx_scope") != self._request(offer).scope():
            raise PaymentRejected("invalid_challenge", "this payment was issued for another request")

    async def settle(self, verified: Verified, offer: Offer) -> Settlement:
        from mpp.errors import PaymentError

        try:
            receipt = await self._mpp.broadcast_credential(
                verified.credential, intent="charge",
                request={"amount": micro_to_decimal(offer.amount_micro)})
        except PaymentError as exc:
            raise PaymentRejected("settlement_failed", str(exc) or exc.title) from exc
        return Settlement(reference=receipt.reference,
                          receipt_headers=(("Payment-Receipt", receipt.to_payment_receipt()),))


def _code(exc) -> str:
    from mpp.errors import InvalidChallengeError, MalformedCredentialError, PaymentExpiredError

    if isinstance(exc, PaymentExpiredError):
        return "payment_expired"
    if isinstance(exc, InvalidChallengeError):
        return "invalid_challenge"
    if isinstance(exc, MalformedCredentialError):
        return "malformed_credential"
    return "verification_failed"
