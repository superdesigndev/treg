"""x402 v2 `exact` (EIP-3009) through `pympp`'s x402 support and an HTTP facilitator."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from . import Lookup, Offer, PaymentRejected, Settlement, Unconfirmed, Verified
from .evm import AUTHORIZATION_USED_TOPIC, Chain, ChainUnavailable, moved, topic_address

PUBLIC_RPC = {"eip155:8453": "https://mainnet.base.org", "eip155:84532": "https://sepolia.base.org"}
STRIPE_NETWORK = {"eip155:8453": "base", "eip155:84532": "base"}
# Block timestamps and our clock disagree a little; this is the margin around the window.
EXPIRY_GRACE = timedelta(seconds=60)

SIGNATURE_HEADER = "payment-signature"
LEGACY_HEADER = "x-payment"  # x402 v1; refused, but still consumed so it never reaches upstream
CONSUMED_HEADERS = (SIGNATURE_HEADER, LEGACY_HEADER)


class X402Rail:
    protocol = "x402"
    consumed_headers = CONSUMED_HEADERS

    def __init__(self, *, pay_to: str, network: str, asset: str, asset_name: str,
                 asset_version: str, facilitator_url: str, max_timeout_s: int,
                 rpc_url: str = "", cdp_api_key_id: str = "", cdp_api_key_secret: str = "",
                 facilitator=None, chain: Chain | None = None) -> None:
        self.network = network
        self.pay_to = pay_to.lower()
        self.stripe_network = STRIPE_NETWORK.get(network)
        url = rpc_url or PUBLIC_RPC.get(network, "")
        self._chain = chain or (Chain(url) if url else None)
        self._pay_to = pay_to
        self._asset = asset
        self._extra = {"name": asset_name, "version": asset_version}
        self._max_timeout_s = max_timeout_s
        if facilitator is None:
            from mpp.x402 import HttpFacilitator

            auth = None
            if cdp_api_key_id:
                from mpp.x402.cdp import create_cdp_auth_headers

                auth = create_cdp_auth_headers(cdp_api_key_id, cdp_api_key_secret)
            facilitator = HttpFacilitator(facilitator_url, create_auth_headers=auth)
        self._facilitator = facilitator

    def presented(self, headers: Mapping[str, str]) -> str | None:
        if headers.get(LEGACY_HEADER) and not headers.get(SIGNATURE_HEADER):
            return LEGACY_HEADER
        return headers.get(SIGNATURE_HEADER) or None

    def _requirements(self, offer: Offer) -> dict:
        return {"scheme": "exact", "network": self.network, "asset": self._asset,
                "amount": str(offer.amount_micro), "payTo": self._pay_to,
                "maxTimeoutSeconds": self._max_timeout_s, "extra": dict(self._extra)}

    async def challenge(self, offer: Offer) -> list[tuple[str, str]]:
        from mpp.x402 import encode_payment_required

        required = {"x402Version": 2, "error": "payment required",
                    "resource": {"url": offer.resource_url, "description": offer.description},
                    "accepts": [self._requirements(offer)]}
        return [("PAYMENT-REQUIRED", encode_payment_required(required))]

    async def verify(self, credential: str, offer: Offer) -> Verified:
        from mpp.x402 import InvalidHeaderError, decode_payment_signature

        if credential == LEGACY_HEADER:
            raise PaymentRejected("unsupported_version", "only x402 version 2 is accepted")
        try:
            payload = decode_payment_signature(credential)
        except InvalidHeaderError as exc:
            raise PaymentRejected("malformed_credential", "PAYMENT-SIGNATURE cannot be decoded") from exc
        requirements = self._requirements(offer)
        accepted = payload["accepted"]
        if (accepted["scheme"], accepted["network"], accepted["asset"].lower(), accepted["amount"],
                accepted["payTo"].lower()) != (requirements["scheme"], requirements["network"],
                                               requirements["asset"].lower(), requirements["amount"],
                                               requirements["payTo"].lower()):
            raise PaymentRejected("invalid_challenge", "this payment does not match the offer")
        resource = payload.get("resource")
        if resource is not None and resource["url"] != offer.resource_url:
            raise PaymentRejected("invalid_challenge", "this payment was issued for another request")
        authorization = payload["payload"].get("authorization") or {}
        sender, nonce = str(authorization.get("from") or ""), str(authorization.get("nonce") or "")
        if not sender or not nonce:
            raise PaymentRejected("malformed_credential", "the authorization names no payer or nonce")
        try:
            result = await self._facilitator.verify(payload, requirements)
        except Exception as exc:
            raise PaymentRejected("verification_unavailable", "the facilitator could not verify") from exc
        if not result["isValid"]:
            raise PaymentRejected("verification_failed", result.get("invalidReason") or "invalid payment")
        payer = (result.get("payer") or sender).lower()
        if payer != sender.lower():
            raise PaymentRejected("verification_failed", "the verified payer is not the signer")
        proof = f"x402:{self.network}:{self._asset.lower()}:{payer}:{nonce.lower()}"
        try:
            valid_before = datetime.fromtimestamp(int(authorization.get("validBefore")), UTC)
        except (TypeError, ValueError, OverflowError) as exc:
            raise PaymentRejected("malformed_credential", "the authorization has no validBefore") from exc
        return Verified(protocol=self.protocol, network=self.network, proof_id=proof, payer=payer,
                        amount_micro=offer.amount_micro, asset=self._asset.lower(),
                        pay_to=self._pay_to.lower(), expires_at=valid_before.replace(tzinfo=None),
                        credential=(payload, requirements))

    async def settle(self, verified: Verified, offer: Offer) -> Settlement:
        from mpp.x402 import encode_payment_response

        payload, requirements = verified.credential
        try:
            result = await self._facilitator.settle(payload, requirements)
        except Exception as exc:
            # The facilitator may have submitted the transfer before the error reached us.
            raise PaymentRejected("settlement_unknown", "the facilitator did not answer") from exc
        transaction = result.get("transaction") or ""
        if result.get("errorReason") == "settlement_pending" or (result["success"] and not transaction):
            raise PaymentRejected("settlement_unknown", "the settlement is still pending")
        if not result["success"]:
            raise PaymentRejected("settlement_failed", result.get("errorReason") or "settlement failed")
        return Settlement(reference=transaction,
                          receipt_headers=(("PAYMENT-RESPONSE", encode_payment_response(result)),))

    async def find(self, payment: Unconfirmed) -> Lookup:
        """EIP-3009 marks a used nonce with `AuthorizationUsed(authorizer, nonce)`; its transaction
        must then carry our transfer. The search covers only the window the authorization could
        settle in: from shortly before the payment was claimed to shortly after `validBefore`."""
        if self._chain is None:
            return Lookup("pending", reason="no_rpc_url")
        nonce = payment.proof_id.rsplit(":", 1)[-1]
        try:
            head, head_time = await self._chain.head()
            start = await self._chain.block_at(payment.created_at - EXPIRY_GRACE)
            closed = head_time > payment.expires_at + EXPIRY_GRACE
            end = await self._chain.block_at(payment.expires_at + EXPIRY_GRACE) if closed else head
            logs = await self._chain.logs(
                address=payment.asset, from_block=start, to_block=end,
                topics=[AUTHORIZATION_USED_TOPIC, topic_address(payment.payer), nonce])
            receipt = (await self._chain.receipt(logs[0]["transactionHash"])) if logs else None
        except ChainUnavailable as exc:
            return Lookup("pending", reason=str(exc))
        if receipt is not None:
            tx_hash = str(receipt.get("transactionHash") or logs[0]["transactionHash"])
            if moved(receipt, asset=payment.asset, payer=payment.payer, pay_to=payment.pay_to,
                     amount=payment.amount_micro):
                return Lookup("landed", reference=tx_hash)
            return Lookup("absent", reference=tx_hash, reason="transfer_mismatch")
        if closed:
            return Lookup("absent", reason="expired_unsettled")
        return Lookup("pending", reason="not_yet_settled")

