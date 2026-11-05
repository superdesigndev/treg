"""Stripe SDK adapter: blocking calls, object normalization, and webhook verification."""

import json
from collections.abc import Callable

import anyio.to_thread
import stripe as stripe_sdk


CUSTOMER_CREATE = "Customer.create"
PAYMENT_INTENT_RETRIEVE = "PaymentIntent.retrieve"
CHECKOUT_SESSION_CREATE = "checkout.Session.create"
PRICE_RETRIEVE = "Price.retrieve"
PORTAL_SESSION_CREATE = "billing_portal.Session.create"
CHARGE_LIST = "Charge.list"
INVOICE_LIST = "Invoice.list"
INVOICE_CREATE = "Invoice.create"
INVOICE_RETRIEVE = "Invoice.retrieve"
INVOICE_FINALIZE = "Invoice.finalize_invoice"
INVOICE_PAY = "Invoice.pay"
INVOICE_VOID = "Invoice.void_invoice"
INVOICE_ITEM_CREATE = "InvoiceItem.create"
INVOICE_ITEM_LIST = "InvoiceItem.list"
INVOICE_PAYMENT_LIST = "InvoicePayment.list"
PAYMENT_INTENT_CREATE = "PaymentIntent.create"
SETUP_INTENT_RETRIEVE = "SetupIntent.retrieve"

CardError = stripe_sdk.CardError
InvalidRequestError = stripe_sdk.InvalidRequestError
StripeError = stripe_sdk.StripeError


def _operation(name: str) -> Callable:
    operations = {
        CUSTOMER_CREATE: stripe_sdk.Customer.create,
        PAYMENT_INTENT_RETRIEVE: stripe_sdk.PaymentIntent.retrieve,
        CHECKOUT_SESSION_CREATE: stripe_sdk.checkout.Session.create,
        PRICE_RETRIEVE: stripe_sdk.Price.retrieve,
        PORTAL_SESSION_CREATE: stripe_sdk.billing_portal.Session.create,
        CHARGE_LIST: stripe_sdk.Charge.list,
        INVOICE_LIST: stripe_sdk.Invoice.list,
        INVOICE_CREATE: stripe_sdk.Invoice.create,
        INVOICE_RETRIEVE: stripe_sdk.Invoice.retrieve,
        INVOICE_FINALIZE: stripe_sdk.Invoice.finalize_invoice,
        INVOICE_PAY: stripe_sdk.Invoice.pay,
        INVOICE_VOID: stripe_sdk.Invoice.void_invoice,
        INVOICE_ITEM_CREATE: stripe_sdk.InvoiceItem.create,
        INVOICE_ITEM_LIST: stripe_sdk.InvoiceItem.list,
        INVOICE_PAYMENT_LIST: stripe_sdk.InvoicePayment.list,
        PAYMENT_INTENT_CREATE: stripe_sdk.PaymentIntent.create,
        SETUP_INTENT_RETRIEVE: stripe_sdk.SetupIntent.retrieve,
    }
    return operations[name]


async def call(operation: str | Callable, /, *, api_key: str, **kwargs):
    fn = _operation(operation) if isinstance(operation, str) else operation
    result = await anyio.to_thread.run_sync(lambda: fn(api_key=api_key, **kwargs))
    return result.to_dict() if isinstance(result, stripe_sdk.StripeObject) else result


async def schedule_subscription_cancel(subscription_id: str, *, api_key: str) -> dict:
    result = await anyio.to_thread.run_sync(lambda: stripe_sdk.Subscription.modify(
        subscription_id, api_key=api_key, cancel_at_period_end=True))
    return result.to_dict() if isinstance(result, stripe_sdk.StripeObject) else result


# Crypto deposit addresses and on-chain PaymentIntent verification exist only in these preview API
# versions, as Stripe's machine-payments guides use them.
CRYPTO_DEPOSIT_VERSION = "2026-07-29.preview"
CRYPTO_PAYMENT_VERSION = "2026-05-27.preview"


def _raw(api_key: str, version: str, method: str, path: str, **params) -> dict:
    client = stripe_sdk.StripeClient(api_key, stripe_version=version)
    return client.deserialize(client.raw_request(method, path, **params), api_mode="V1").to_dict()


async def create_crypto_deposit_address(network: str, *, api_key: str) -> dict:
    """A reusable address on `network` (`tempo`, `base`) whose deposits Stripe converts to fiat."""
    return await anyio.to_thread.run_sync(lambda: _raw(
        api_key, CRYPTO_DEPOSIT_VERSION, "post", "/v1/crypto/deposit_addresses", network=network))


async def record_crypto_payment(*, amount_cents: int, network: str, transaction_hash: str,
                                api_key: str) -> dict:
    """Record an on-chain payment to a deposit address as a PaymentIntent. The transaction hash
    is the idempotency key, so a retry records it once."""
    return await anyio.to_thread.run_sync(lambda: _raw(
        api_key, CRYPTO_PAYMENT_VERSION, "post", "/v1/payment_intents",
        amount=amount_cents, currency="usd", confirm=True,
        payment_method_data={"type": "crypto"}, payment_method_types=["crypto"],
        payment_method_options={"crypto": {
            "mode": "transaction_verification",
            "transaction_verification_options": {
                "network": network, "transaction_hash": transaction_hash}}},
        idempotency_key=f"agentpay:{transaction_hash}"))


def verify_event(payload: bytes, signature: str, secret: str) -> dict:
    try:
        # `verify_header` signs a STRING (`f"{t}.{payload}"`), so bytes must be decoded first or the
        # HMAC is computed over the literal `b'…'` repr and every genuine event fails to verify.
        body = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        stripe_sdk.WebhookSignature.verify_header(
            body, signature, secret, stripe_sdk.Webhook.DEFAULT_TOLERANCE)
        return json.loads(body)
    except Exception as exc:  # SignatureVerificationError, ValueError, all mean "don't trust this"
        raise ValueError(f"bad signature or payload: {exc}") from exc
