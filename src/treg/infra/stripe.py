"""Stripe SDK adapter: blocking calls, object normalization, and webhook verification."""

import json
from collections.abc import Callable

import anyio.to_thread
import stripe as stripe_sdk


CUSTOMER_CREATE = "Customer.create"
PAYMENT_INTENT_RETRIEVE = "PaymentIntent.retrieve"
EVENT_LIST = "Event.list"
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
        EVENT_LIST: stripe_sdk.Event.list,
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
