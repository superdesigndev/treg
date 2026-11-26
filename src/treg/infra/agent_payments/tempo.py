"""MPP charge on Tempo, through `pympp` (import name `mpp`)."""

from __future__ import annotations

import base64
import json
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

import httpx

from . import Lookup, Offer, PaymentRejected, Settlement, Unconfirmed, Verified, micro_to_decimal
from .evm import Chain, ChainUnavailable, moved

CONSUMED_HEADERS = ("authorization",)
# Block timestamps and our clock disagree a little; this is the margin past `valid_before`.
EXPIRY_GRACE = timedelta(seconds=60)
RPC_TIMEOUT_S = 30.0


class _Gate:
    """`before_remote` of one `verify`, run before its first RPC request and never again."""

    def __init__(self, hook: Callable[[], Awaitable[None]]) -> None:
        self.hook, self.spent, self.refused = hook, False, None


_gate: ContextVar[_Gate | None] = ContextVar("mpp_verify_gate", default=None)


async def _before_rpc(request: httpx.Request) -> None:
    gate = _gate.get()
    if gate is None or gate.spent:
        return
    gate.spent = True
    try:
        await gate.hook()
    except Exception as exc:
        gate.refused = exc   # pympp may wrap what an RPC raises; `verify` re-raises this
        raise


def _payment_scheme(value: str) -> bool:
    # The HTTP auth scheme name is case-insensitive (RFC 9110 §11.1).
    return value[:8].lower() == "payment "


class MppTempoRail:
    protocol = "mpp"
    consumed_headers = CONSUMED_HEADERS
    deferrable_payments = False
    stripe_network = "tempo"

    def __init__(self, *, secret: str, realm: str, recipient: str, chain_id: int,
                 rpc_url: str = "", rpc_api_key: str = "", currency: str = "",
                 challenge_ttl_s: int = 120, store=None) -> None:
        from mpp.methods.tempo import rpc_url_for_chain
        from mpp.methods.tempo import server as tempo
        from mpp.server import Mpp

        self.network = f"tempo:{chain_id}"
        self.pay_to = recipient.lower()
        # An IPv6 literal without its brackets, as clients compare the realm with the host they
        # called and `agent_payments._hostname` gives the request's host.
        self.realm = realm.lower().strip("[]")
        self.proof_host = self.realm
        self._challenge_ttl = timedelta(seconds=challenge_ttl_s)
        # Tempo's hosted RPC takes its console API key in this header, not in the URL. pympp sends
        # every RPC through this client, so its hook is where a verification first costs a call.
        client = httpx.AsyncClient(headers={"tempo-api-key": rpc_api_key} if rpc_api_key else None,
                                   timeout=RPC_TIMEOUT_S, event_hooks={"request": [_before_rpc]})
        self._chain = Chain(rpc_url or rpc_url_for_chain(chain_id), client=client)
        methods = tempo.charge(
            recipient=recipient, chain_id=chain_id, rpc_url=rpc_url or None, http_client=client,
            currencies=[currency] if currency else None, decimals=6)
        # pympp refuses to start on a production or multi-instance platform without a shared
        # replay store; `store` is one (`agent_payments.ReplayStore`), tests may omit it.
        self._mpp = Mpp(methods=methods, realm=self.realm, secret_key=secret, store=store)
        # Code before the realm was normalized issued challenges with the realm as configured,
        # and pympp compares it exactly: one still in flight through a rolling deploy (its
        # transfer may already be on chain) verifies and settles against that realm.
        self._legacy = (Mpp(methods=methods, realm=realm, secret_key=secret, store=store)
                        if realm != self.realm else None)

    def _server_for(self, parsed):
        """The pympp server whose realm issued this credential's challenge."""
        if self._legacy is not None and parsed.challenge.realm != self.realm:
            return self._legacy
        return self._mpp

    def presented(self, headers: Mapping[str, str]) -> str | None:
        value = headers.get("authorization") or ""
        return value if _payment_scheme(value) else None

    def _request(self, offer: Offer):
        from mpp.server.http import Request

        url = offer.path + (f"?{offer.query}" if offer.query else "")
        return Request(method=offer.method, url=f"https://payment.invalid{url}")

    def proves(self, offer: Offer) -> bool:
        # Clients refuse to sign a proof whose realm is not the hostname they called (mppx throws
        # instead of choosing another challenge), so a request to any other host gets none.
        return bool(offer.host) and offer.host == self.realm

    async def challenge(self, offer: Offer, *, pay: bool = True,
                        prove: bool = False) -> list[tuple[str, str]]:
        # The zero-amount proof comes first: clients answer the first challenge they can pay.
        headers = []
        if prove and self.proves(offer):
            headers += await self._challenge(offer, "0", f"prove this wallet for {offer.description}",
                                             meta=_terms(offer))
        if pay:
            headers += await self._challenge(offer, micro_to_decimal(offer.amount_micro),
                                             offer.description, meta=_terms(offer))
        return headers

    async def _challenge(self, offer: Offer, amount: str, description: str,
                         meta: dict[str, str] | None = None) -> list[tuple[str, str]]:
        handler = self._mpp["tempo/charge"](
            amount=amount, description=description, expires_in=self._challenge_ttl,
            **({"meta": meta} if meta else {}))
        result = await handler(self._request(offer))
        return [("WWW-Authenticate", value)
                for value in result.challenge.headers.get_list("www-authenticate")]

    async def verify(self, credential: str, offer: Offer, *,
                     before_remote: Callable[[], Awaitable[None]] | None = None) -> Verified:
        from mpp import Credential
        from mpp.errors import PaymentError

        try:
            parsed = Credential.from_authorization(credential)
        except Exception as exc:
            raise PaymentRejected("malformed_credential", "the Payment credential cannot be parsed") from exc
        echoed = _echoed_request(parsed)
        meta = _echoed_meta(parsed)
        proof = (parsed.payload or {}).get("type") == "proof"
        if not proof and offer.amount_micro <= 0:
            raise PaymentRejected("proof_required", "this request takes a zero-amount proof, not a payment")
        # The amount and terms are the challenge's own, HMAC-checked by pympp: a transfer is checked
        # against what its payer was asked for, and the application decides what those terms buy
        # now. A push transfer is already on chain, so refusing it here would lose the money.
        amount = "0" if proof else _echoed_amount(echoed)
        gate = _Gate(before_remote) if before_remote else None
        token = _gate.set(gate)
        try:
            validation = await self._server_for(parsed).validate_credential(
                parsed, intent="charge", http_request=self._request(offer),
                request={"amount": amount}, meta=meta)
        except Exception as exc:
            if gate is not None and gate.refused is not None:
                raise gate.refused from None
            if isinstance(exc, PaymentError):
                raise PaymentRejected(_code(exc), str(exc) or exc.title) from exc
            raise
        finally:
            _gate.reset(token)
        if not validation.payer:
            raise PaymentRejected("verification_failed", "the payment names no payer")
        details = validation.details if isinstance(validation.details, dict) else {}
        if proof:
            if details.get("mode") != "proof":
                raise PaymentRejected("malformed_credential", "the proof was not checked as a proof")
            # The challenge is HMAC-bound to this route and query; its id is single-use (treg
            # claims it), and the challenge expiry bounds how long the claim must last.
            expires = _iso(parsed.challenge.expires) or datetime.now(UTC).replace(tzinfo=None)
            return Verified(protocol=self.protocol, network=self.network,
                            proof_id=f"mpp:{self.network}:proof:{parsed.challenge.id}",
                            payer=validation.payer.lower(), amount_micro=0, asset="", pay_to="",
                            expires_at=expires, deferrable=False, credential=parsed, kind="proof",
                            terms=(meta or {}).get("terms", ""))
        tx_hash = str(details.get("transaction_hash") or "")
        if details.get("mode") not in ("push", "pull") or not tx_hash:
            raise PaymentRejected("malformed_credential", "send a signed or broadcast Tempo transfer")
        now = datetime.now(UTC).replace(tzinfo=None)
        if details["mode"] == "push":
            # The payer already broadcast the transfer, so it is settled, never drawn as credit.
            deferrable, expires_at = False, now
        else:
            # A returning payer proves its wallet with a zero-amount proof, so a real transfer is
            # always settled: treg never holds an MPP transfer it did not send.
            deferrable = False
            expires_at = _timestamp(details.get("valid_before"))
            if expires_at is None:
                # A transfer with no `valid_before` stays valid until its nonce is used, so treg
                # could never tell that it can no longer land.
                raise PaymentRejected("expiry_required",
                                      "sign the Tempo transfer with valid_before (an expiring nonce)")
        # Both modes key on the transaction hash the chain records, so one transfer is one proof
        # whichever mode presents it and under whichever challenge it is wrapped.
        return Verified(protocol=self.protocol, network=self.network,
                        proof_id=f"mpp:{self.network}:tx:{tx_hash.lower()}",
                        payer=validation.payer.lower(), amount_micro=int(echoed["amount"]),
                        asset=str(echoed.get("currency") or "").lower(),
                        pay_to=str(echoed.get("recipient") or "").lower(),
                        expires_at=expires_at, deferrable=deferrable, credential=parsed,
                        terms=(meta or {}).get("terms", ""), moved=details["mode"] == "push")

    async def settle(self, verified: Verified, offer: Offer) -> Settlement:
        from mpp.errors import PaymentError

        from mpp.errors import InternalPaymentError, PaymentOutcomeUnknownError

        try:
            receipt = await self._server_for(verified.credential).broadcast_credential(
                verified.credential, intent="charge", http_request=self._request(offer),
                request={"amount": micro_to_decimal(verified.amount_micro)},
                meta=_echoed_meta(verified.credential))
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
        # Tempo has no reorgs: a confirmed receipt is final.
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


def _terms(offer: Offer) -> dict[str, str] | None:
    """The terms a challenge carries in its HMAC-bound opaque data."""
    return {"terms": offer.terms} if offer.terms else None


def _echoed_meta(parsed) -> dict[str, str] | None:
    """The opaque data the credential echoes, less pympp's own nonce: what `validate_credential`
    must be told to expect, and the terms the payer was offered. pympp checks it against the
    challenge's HMAC."""
    raw = parsed.challenge.opaque or ""
    if not raw:
        return None
    try:
        opaque = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (ValueError, TypeError) as exc:
        raise PaymentRejected("malformed_credential", "the challenge data cannot be read") from exc
    if not isinstance(opaque, dict):
        raise PaymentRejected("malformed_credential", "the challenge data cannot be read")
    return {key: value for key, value in opaque.items() if key != "_mpp_nonce"} or None


def _echoed_amount(echoed: dict) -> str:
    """The challenge's amount, base units (micro-USD), as the decimal pympp compares."""
    try:
        return micro_to_decimal(int(echoed.get("amount")))
    except (TypeError, ValueError) as exc:
        raise PaymentRejected("malformed_credential", "the challenge names no amount") from exc


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


def _iso(stamp) -> datetime | None:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


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
