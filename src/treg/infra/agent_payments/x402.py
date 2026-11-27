"""x402 v2 `exact` (EIP-3009) through `pympp`'s x402 support and an HTTP facilitator, and the
`sign-in-with-x` (SIWX) extension, with which a wallet proves its address and pays nothing."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from . import Lookup, Offer, PaymentRejected, Settlement, Unconfirmed, Verified
from .evm import AUTHORIZATION_USED_TOPIC, Chain, ChainUnavailable, moved, topic_address

PUBLIC_RPC = {"eip155:8453": "https://mainnet.base.org", "eip155:84532": "https://sepolia.base.org"}
STRIPE_NETWORK = {"eip155:8453": "base", "eip155:84532": "base"}
# Block timestamps and our clock disagree a little; this is the margin around the window.
EXPIRY_GRACE = timedelta(seconds=60)

SIGNATURE_HEADER = "payment-signature"
LEGACY_HEADER = "x-payment"  # x402 v1; refused, but still consumed so it never reaches upstream
SIWX_HEADER = "sign-in-with-x"
CONSUMED_HEADERS = (SIGNATURE_HEADER, LEGACY_HEADER, SIWX_HEADER)
# Where an offer's terms ride in its requirements' `extra`.
TERMS_KEY = "tregTerms"
# `presented` marks a SIWX proof so `verify` can tell it from a payment.
SIWX_MARK = "siwx "


class X402Rail:
    protocol = "x402"
    consumed_headers = CONSUMED_HEADERS
    deferrable_payments = True

    def __init__(self, *, pay_to: str, network: str, asset: str, asset_name: str,
                 asset_version: str, facilitator_url: str, max_timeout_s: int,
                 rpc_url: str = "", cdp_api_key_id: str = "", cdp_api_key_secret: str = "",
                 facilitator=None, chain: Chain | None = None, block_tag: str = "finalized",
                 origin: str = "") -> None:
        self.network = network
        self._chain_id = int(network.split(":", 1)[1])
        # SIWX proofs name the public origin, never the request's Host, which a caller controls.
        self._origin = origin.rstrip("/")
        self._domain = (urlsplit(self._origin).hostname or "") if self._origin else ""
        self.proof_host = self._domain
        self._block_tag = block_tag
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
        if payment := headers.get(SIGNATURE_HEADER):
            return payment
        proof = headers.get(SIWX_HEADER)
        return SIWX_MARK + proof if proof else None

    def proves(self, offer: Offer) -> bool:
        # Clients sign SIWX only for the host they called (pympp's and mppx's SiwxTransport).
        return bool(self._domain) and offer.host == self._domain

    def _requirements(self, offer: Offer, terms: str | None = None) -> dict:
        # `accepted` is echoed whole, so the terms ride in `extra` beside the EIP-712 domain.
        terms = offer.terms if terms is None else terms
        return {"scheme": "exact", "network": self.network, "asset": self._asset,
                "amount": str(offer.amount_micro), "payTo": self._pay_to,
                "maxTimeoutSeconds": self._max_timeout_s,
                "extra": {**self._extra, **({TERMS_KEY: terms} if terms else {})}}

    async def challenge(self, offer: Offer, *, pay: bool = True,
                        prove: bool = False) -> list[tuple[str, str]]:
        from mpp.x402 import encode_payment_required
        from mpp.x402.siwx import EXTENSION_KEY, create_extension

        prove = prove and self.proves(offer)
        if not (pay or prove):
            return []
        # x402 requires a non-empty `accepts`; a route that takes only a proof still lists it,
        # and refuses the payment.
        required = {"x402Version": 2, "error": "payment required",
                    "resource": {"url": offer.resource_url, "description": offer.description},
                    "accepts": [self._requirements(offer)]}
        if prove:
            extension = create_extension(
                chain_id=self._chain_id, origin=self._origin, url=offer.resource_url)
            if offer.terms:
                # The signed message carries its nonce, so the terms are the wallet's signature's.
                extension["info"]["nonce"] = offer.terms
            required["extensions"] = {EXTENSION_KEY: extension}
        return [("PAYMENT-REQUIRED", encode_payment_required(required))]

    def offered_terms(self, credential: str) -> str:
        from mpp.x402 import X402FormatError, decode_payment_signature, siwx

        try:
            if credential.startswith(SIWX_MARK):
                terms = siwx.decode(credential[len(SIWX_MARK):])["nonce"]
            else:
                terms = decode_payment_signature(credential)["accepted"]["extra"][TERMS_KEY]
        except (X402FormatError, siwx.InvalidProofError, ValueError, KeyError, TypeError,
                AttributeError):
            return ""
        return terms if isinstance(terms, str) else ""

    async def _verify_proof(self, value: str, offer: Offer) -> Verified:
        from mpp.x402 import siwx

        try:
            payload = siwx.decode(value)
            # The nonce is claimed by treg (`agent_payments._claim_proof`), not by pympp's store.
            proven = await siwx.verify(chain_id=self._chain_id, origin=self._origin, store=None,
                                       payload=payload, claim_nonce=False)
        except siwx.InvalidProofError as exc:
            raise PaymentRejected("verification_failed", f"the SIWX proof was refused ({exc.code})") from exc
        except ValueError as exc:
            raise PaymentRejected("malformed_credential", "the SIWX proof cannot be read") from exc
        # SIWX binds only the origin; treg also binds the proof to this route and query, as an
        # MPP proof is bound.
        if payload["uri"] != offer.resource_url:
            raise PaymentRejected("invalid_challenge", "this proof was not signed for this request")
        issued = datetime.fromisoformat(payload["issuedAt"])   # `verify` has parsed it
        issued = issued if issued.tzinfo else issued.replace(tzinfo=UTC)
        expires = issued.astimezone(UTC) + timedelta(seconds=siwx.TTL_SECONDS) + EXPIRY_GRACE
        payer = proven.address.lower()
        return Verified(protocol=self.protocol, network=self.network,
                        proof_id=f"x402:{self.network}:siwx:{payer}:{payload['nonce']}",
                        payer=payer, amount_micro=0, asset="", pay_to="",
                        expires_at=expires.replace(tzinfo=None), deferrable=False, kind="proof",
                        terms=payload["nonce"])

    async def verify(self, credential: str, offer: Offer, *,
                     before_remote: Callable[[], Awaitable[None]] | None = None) -> Verified:
        from mpp.x402 import X402FormatError, decode_payment_signature

        if credential.startswith(SIWX_MARK):
            return await self._verify_proof(credential[len(SIWX_MARK):], offer)
        if credential == LEGACY_HEADER:
            raise PaymentRejected("unsupported_version", "only x402 version 2 is accepted")
        try:
            payload = decode_payment_signature(credential)
        except X402FormatError as exc:  # also the parent of InvalidHeaderError
            raise PaymentRejected("malformed_credential", "PAYMENT-SIGNATURE cannot be decoded") from exc
        try:
            accepted = payload["accepted"]
            offered = (accepted["scheme"], accepted["network"], str(accepted["asset"]).lower(),
                       accepted["amount"], str(accepted["payTo"]).lower())
            extra = accepted.get("extra") or {}
            terms = extra.get(TERMS_KEY, "") if isinstance(extra, dict) else None
            resource_url = (payload.get("resource") or {}).get("url")
            authorization = payload["payload"].get("authorization") or {}
        except (KeyError, TypeError, AttributeError) as exc:
            raise PaymentRejected("malformed_credential", "PAYMENT-SIGNATURE is not an x402 v2 payment") from exc
        if not isinstance(terms, str) or (TERMS_KEY in extra and not terms):
            # Missing terms are a challenge issued without them; terms that are there but not a
            # token string are a damaged payment, never today's terms.
            raise PaymentRejected("malformed_credential", "the payment's terms cannot be read")
        # The facilitator checks the payment against the terms it was signed under.
        requirements = self._requirements(offer, terms)
        if offered != (requirements["scheme"], requirements["network"], requirements["asset"].lower(),
                       requirements["amount"], requirements["payTo"].lower()):
            raise PaymentRejected("invalid_challenge", "this payment does not match the offer")
        if resource_url != offer.resource_url:
            raise PaymentRejected("invalid_challenge", "this payment was not issued for this request")
        sender, nonce = str(authorization.get("from") or ""), str(authorization.get("nonce") or "")
        if not sender or not nonce:
            raise PaymentRejected("malformed_credential", "the authorization names no payer or nonce")
        try:
            valid_before = datetime.fromtimestamp(int(authorization.get("validBefore")), UTC)
        except (TypeError, ValueError, OverflowError) as exc:
            raise PaymentRejected("malformed_credential", "the authorization has no validBefore") from exc
        if before_remote is not None:
            await before_remote()
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
        return Verified(protocol=self.protocol, network=self.network, proof_id=proof, payer=payer,
                        amount_micro=offer.amount_micro, asset=self._asset.lower(),
                        pay_to=self._pay_to.lower(), expires_at=valid_before.replace(tzinfo=None),
                        credential=(payload, requirements), terms=requirements["extra"].get(TERMS_KEY, ""))

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
        if not result["success"] and transaction:
            # A failure that names a transaction may still have moved the money; the chain decides.
            raise PaymentRejected("settlement_unknown", result.get("errorReason") or "settlement unclear")
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
            # Only blocks at or below this head count: on `finalized`, no reorg can undo them.
            head, head_time = await self._chain.head(self._block_tag)
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

