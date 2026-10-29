"""x402 v2 `exact` (EIP-3009) through the official `x402` SDK and an HTTP facilitator."""

from __future__ import annotations

from collections.abc import Mapping

from . import Offer, PaymentRejected, Settlement, Verified

SIGNATURE_HEADER = "payment-signature"
LEGACY_HEADER = "x-payment"  # x402 v1; refused, but still consumed so it never reaches upstream
CONSUMED_HEADERS = (SIGNATURE_HEADER, LEGACY_HEADER)


class X402Rail:
    protocol = "x402"
    consumed_headers = CONSUMED_HEADERS

    def __init__(self, *, pay_to: str, network: str, asset: str, asset_name: str,
                 asset_version: str, facilitator_url: str, max_timeout_s: int,
                 facilitator=None) -> None:
        self.network = network
        self._pay_to = pay_to
        self._asset = asset
        self._extra = {"name": asset_name, "version": asset_version}
        self._max_timeout_s = max_timeout_s
        if facilitator is None:
            from x402.http import FacilitatorConfig, HTTPFacilitatorClient

            facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=facilitator_url))
        self._facilitator = facilitator

    def presented(self, headers: Mapping[str, str]) -> str | None:
        return headers.get(SIGNATURE_HEADER) or headers.get(LEGACY_HEADER) or None

    def _requirements(self, offer: Offer):
        from x402.schemas import PaymentRequirements

        return PaymentRequirements(
            scheme="exact", network=self.network, asset=self._asset,
            amount=str(offer.amount_micro), pay_to=self._pay_to,
            max_timeout_seconds=self._max_timeout_s, extra=dict(self._extra))

    async def challenge(self, offer: Offer) -> list[tuple[str, str]]:
        from x402.http import encode_payment_required_header
        from x402.schemas import PaymentRequired, ResourceInfo

        required = PaymentRequired(
            x402_version=2, error="payment required",
            resource=ResourceInfo(url=offer.resource_url, description=offer.description),
            accepts=[self._requirements(offer)])
        return [("PAYMENT-REQUIRED", encode_payment_required_header(required))]

    async def verify(self, credential: str, offer: Offer) -> Verified:
        from x402.http import decode_payment_signature_header
        from x402.schemas import PaymentPayload

        try:
            payload = decode_payment_signature_header(credential)
        except Exception as exc:
            raise PaymentRejected("malformed_credential", "PAYMENT-SIGNATURE cannot be decoded") from exc
        if not isinstance(payload, PaymentPayload):
            raise PaymentRejected("unsupported_version", "only x402 version 2 is accepted")
        requirements = self._requirements(offer)
        accepted = payload.accepted
        if (accepted.scheme, accepted.network, accepted.asset.lower(), accepted.amount,
                accepted.pay_to.lower()) != (requirements.scheme, requirements.network,
                                             requirements.asset.lower(), requirements.amount,
                                             requirements.pay_to.lower()):
            raise PaymentRejected("invalid_challenge", "this payment does not match the offer")
        if payload.resource is not None and payload.resource.url != offer.resource_url:
            raise PaymentRejected("invalid_challenge", "this payment was issued for another request")
        authorization = (payload.payload or {}).get("authorization") or {}
        sender, nonce = str(authorization.get("from") or ""), str(authorization.get("nonce") or "")
        if not sender or not nonce:
            raise PaymentRejected("malformed_credential", "the authorization names no payer or nonce")
        try:
            result = await self._facilitator.verify(payload, requirements)
        except Exception as exc:
            raise PaymentRejected("verification_unavailable", "the facilitator could not verify") from exc
        if not result.is_valid:
            raise PaymentRejected("verification_failed", result.invalid_reason or "invalid payment")
        payer = (result.payer or sender).lower()
        if payer != sender.lower():
            raise PaymentRejected("verification_failed", "the verified payer is not the signer")
        proof = f"x402:{self.network}:{self._asset.lower()}:{payer}:{nonce.lower()}"
        return Verified(protocol=self.protocol, network=self.network, proof_id=proof, payer=payer,
                        amount_micro=offer.amount_micro, credential=(payload, requirements))

    async def settle(self, verified: Verified, offer: Offer) -> Settlement:
        from x402.http import encode_payment_response_header

        payload, requirements = verified.credential
        try:
            result = await self._facilitator.settle(payload, requirements)
        except Exception as exc:
            raise PaymentRejected("settlement_failed", "the facilitator could not settle") from exc
        if not result.success or not result.transaction:
            raise PaymentRejected("settlement_failed", result.error_reason or "settlement failed")
        return Settlement(reference=result.transaction,
                          receipt_headers=(("PAYMENT-RESPONSE", encode_payment_response_header(result)),))
