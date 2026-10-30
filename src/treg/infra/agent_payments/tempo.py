"""MPP charge on Tempo, through `pympp` (import name `mpp`)."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from . import Lookup, Offer, PaymentRejected, Settlement, Unconfirmed, Verified, micro_to_decimal
from .evm import Chain, ChainUnavailable, moved

CONSUMED_HEADERS = ("authorization",)
# A transaction with no expiring nonce can be included at any later time; a day is how long the
# reconciler waits before it calls one that has not landed failed.
UNBOUNDED_VALIDITY = timedelta(days=1)
# Block timestamps and our clock disagree a little; this is the margin past `valid_before`.
EXPIRY_GRACE = timedelta(seconds=60)


def _payment_scheme(value: str) -> bool:
    # The HTTP auth scheme name is case-insensitive (RFC 9110 §11.1).
    return value[:8].lower() == "payment "


class MppTempoRail:
    protocol = "mpp"
    consumed_headers = CONSUMED_HEADERS
    stripe_network = "tempo"

    def __init__(self, *, secret: str, realm: str, recipient: str, chain_id: int,
                 rpc_url: str = "", currency: str = "") -> None:
        from mpp.methods.tempo import server as tempo
        from mpp.methods.tempo._defaults import rpc_url_for_chain
        from mpp.server import Mpp

        self.network = f"tempo:{chain_id}"
        self.pay_to = recipient.lower()
        self._chain = Chain(rpc_url or rpc_url_for_chain(chain_id))
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
        echoed = self._check_binding(parsed, offer)
        try:
            validation = await self._mpp.validate_credential(
                parsed, intent="charge",
                request={"amount": micro_to_decimal(offer.amount_micro)})
        except PaymentError as exc:
            raise PaymentRejected(_code(exc), str(exc) or exc.title) from exc
        if not validation.payer:
            raise PaymentRejected("verification_failed", "the payment names no payer")
        payload = parsed.payload if isinstance(parsed.payload, dict) else {}
        now = datetime.now(UTC).replace(tzinfo=None)
        if payload.get("type") == "hash":
            # Push mode: the payer already broadcast the transfer.
            tx_hash, deferrable, expires_at = str(payload.get("hash") or ""), False, now
        else:
            raw = str(payload.get("signature") or "")
            tx_hash, deferrable = _transaction_hash(raw), _recovered_sender(raw)
            expires_at = _valid_before(raw) or now + UNBOUNDED_VALIDITY
        # Both modes key on the transaction hash the chain records, so one transfer is one proof
        # whichever mode presents it and under whichever challenge it is wrapped.
        return Verified(protocol=self.protocol, network=self.network,
                        proof_id=f"mpp:{self.network}:tx:{tx_hash.lower()}",
                        payer=validation.payer.lower(), amount_micro=offer.amount_micro,
                        asset=str(echoed.get("currency") or "").lower(),
                        pay_to=str(echoed.get("recipient") or "").lower(),
                        expires_at=expires_at, deferrable=deferrable, credential=parsed)

    def _check_binding(self, parsed, offer: Offer) -> dict:
        """The echoed challenge is HMAC-authenticated by `validate_credential`; its signed route
        scope must name this route, or a payment for one query could be spent on another."""
        raw = parsed.challenge.request or ""
        try:
            echoed = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        except (ValueError, TypeError) as exc:
            raise PaymentRejected("malformed_credential", "the challenge request cannot be read") from exc
        if not isinstance(echoed, dict) or echoed.get("_mppx_scope") != self._request(offer).scope():
            raise PaymentRejected("invalid_challenge", "this payment was issued for another request")
        return echoed

    async def settle(self, verified: Verified, offer: Offer) -> Settlement:
        from mpp.errors import PaymentError

        from mpp.errors import InternalPaymentError, PaymentOutcomeUnknownError

        try:
            receipt = await self._mpp.broadcast_credential(
                verified.credential, intent="charge",
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


def _transaction_hash(raw: str) -> str:
    """The hash a Tempo node gives this signed transaction. pympp exposes no public helper; these
    are the ones its own replay store uses (pinned by the `pympp` version range, as is
    `_transaction_valid_before`)."""
    from mpp.methods.tempo.intents import _canonical_raw_transaction, _raw_transaction_hash

    try:
        return _raw_transaction_hash(_canonical_raw_transaction(raw))
    except Exception as exc:
        raise PaymentRejected("malformed_credential", "the signed transaction cannot be read") from exc


def _valid_before(raw: str) -> datetime | None:
    """The expiring-nonce `valid_before` of a signed transaction, or None when it has none."""
    from mpp.methods.tempo.intents import _transaction_valid_before

    stamp = _transaction_valid_before(raw)
    try:
        return datetime.fromtimestamp(stamp, UTC).replace(tzinfo=None) if stamp else None
    except (OverflowError, OSError, ValueError):
        return None


def _recovered_sender(raw: str) -> bool:
    """True when the sender signature is a 65-byte secp256k1 signature, the one kind pympp
    recovers before payment. Keychain, multisig, P256 and WebAuthn senders are only named in the
    transaction and authenticated by the chain at settlement, so they prove nothing until then
    and may never draw credit without settling."""
    from mpp.methods.tempo import _rlp as rlp

    try:
        tx = bytes.fromhex(raw.removeprefix("0x"))
        fields = rlp.decode(tx[1:])
    except (ValueError, rlp.RLPException):
        return False
    return (tx[:1] == b"\x76" and isinstance(fields, list) and len(fields) in (14, 15)
            and isinstance(fields[-1], bytes) and len(fields[-1]) == 65)


def _code(exc) -> str:
    from mpp.errors import InvalidChallengeError, MalformedCredentialError, PaymentExpiredError

    if isinstance(exc, PaymentExpiredError):
        return "payment_expired"
    if isinstance(exc, InvalidChallengeError):
        return "invalid_challenge"
    if isinstance(exc, MalformedCredentialError):
        return "malformed_credential"
    return "verification_failed"
