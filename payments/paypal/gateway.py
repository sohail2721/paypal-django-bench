"""
The one place the PayPal Server SDK is called.

Every function returns plain dataclasses and raises only ``PayPalError``. No SDK type, raw upstream body,
card number or security code leaves this module, and none of them is ever logged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, TypeVar

import httpx2
from pay_pal_server_sdk.core import UNSET, ApiError, Failure, OAuthProviderError, Success, UnsetType
from pay_pal_server_sdk.models import (
    Address,
    AmountWithBreakdown,
    CaptureRequest,
    CardRequest,
    Customer,
    Error,
    LinkDescription,
    Money,
    OrderRequest,
    PaymentSource,
    PaymentTokenRequest,
    PaymentTokenRequestCard,
    PaymentTokenRequestPaymentSource,
    PurchaseUnit,
    PurchaseUnitRequest,
    RefundRequest,
)
from pay_pal_server_sdk.models.enums import CheckoutPaymentIntent, OrderStatus
from pydantic import ValidationError

from .client import get_client

logger = logging.getLogger(__name__)

T = TypeVar("T")

REPRESENTATION = "return=representation"

# ISO 4217 minor units for every currency that does not have two (python-models skill).
_EXPONENT = {
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0, "KRW": 0, "PYG": 0,
    "RWF": 0, "UGX": 0, "UYI": 0, "VND": 0, "VUV": 0, "XAF": 0, "XOF": 0, "XPF": 0,
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
    "CLF": 4, "UYW": 4,
}


def currency_exponent(currency: str) -> int:
    return _EXPONENT.get(currency, 2)


def format_amount(value: Decimal, currency: str) -> str:
    """Format a money value the way PayPal expects it: a string scaled to the currency's minor unit."""
    return str(value.quantize(Decimal(1).scaleb(-currency_exponent(currency))))


class PayPalError(Exception):
    """
    The single failure type of the PayPal boundary.

    ``status_code`` is the HTTP status this app should answer with; ``outcome_unknown`` says whether the
    call may have taken effect at PayPal (so the caller must reconcile rather than assume it failed).
    ``issue`` is PayPal's machine-readable issue code, when it sent one.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        outcome_unknown: bool = False,
        provider_status: int | None = None,
        issue: str | None = None,
        debug_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.outcome_unknown = outcome_unknown
        self.provider_status = provider_status
        self.issue = issue
        self.debug_id = debug_id


# --------------------------------------------------------------------------------------------------
# Plain value types handed to / returned from the rest of the app
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, repr=False)
class CardInput:
    """Raw card details. Held in memory only for the duration of one request; never persisted."""

    number: str
    expiry: str  # YYYY-MM
    security_code: str
    name: str
    billing_address: dict[str, str] | None = None

    def __repr__(self) -> str:  # never print the PAN or CVV, whatever logs this object
        return f"CardInput(last4={self.number[-4:]!r}, expiry={self.expiry!r})"


@dataclass(frozen=True)
class AuthorizationResult:
    paypal_order_id: str
    order_status: str
    authorization_id: str | None
    authorization_status: str | None
    amount: str | None
    currency: str | None
    created_at: str | None
    expires_at: str | None
    card_brand: str | None
    card_last_digits: str | None
    payer_action_required: bool


@dataclass(frozen=True)
class AuthorizationState:
    id: str
    status: str
    amount: str | None
    currency: str | None
    created_at: str | None
    expires_at: str | None


@dataclass(frozen=True)
class CaptureResult:
    id: str
    status: str
    amount: str | None
    currency: str | None
    gross_amount: str | None
    paypal_fee: str | None
    net_amount: str | None


@dataclass(frozen=True)
class RefundResult:
    id: str
    status: str
    amount: str | None
    currency: str | None


@dataclass(frozen=True)
class VaultedCard:
    vault_id: str
    paypal_customer_id: str | None
    brand: str | None
    last_digits: str | None
    expiry: str | None
    payer_action_required: bool


@dataclass(frozen=True)
class TransactionRecord:
    transaction_id: str
    paypal_reference_id: str | None
    event_code: str | None
    status: str | None
    initiated_at: str | None
    updated_at: str | None
    amount: str | None
    currency: str | None
    fee: str | None
    invoice_id: str | None
    custom_field: str | None


@dataclass(frozen=True)
class TransactionPage:
    records: list[TransactionRecord]
    page: int
    total_pages: int
    total_items: int | None


# --------------------------------------------------------------------------------------------------
# Error boundary
# --------------------------------------------------------------------------------------------------


def _value(v: T | UnsetType) -> T | None:
    return None if isinstance(v, UnsetType) else v


def _str(v: Any) -> str | None:
    """Stringify an optional SDK scalar or open enum (a str-Enum stringifies to its wire value)."""
    if v is None or isinstance(v, UnsetType):
        return None
    return str(v.value) if hasattr(v, "value") else str(v)


def provider_error(operation: str, status: int, error: object) -> PayPalError:
    issue: str | None = None
    debug_id: str | None = None
    if isinstance(error, Error):
        debug_id = error.debug_id
        details = _value(error.details)
        if details:
            issue = details[0].issue
        elif error.name:
            issue = error.name
    logger.warning(
        "PayPal %s rejected: status=%s issue=%s debug_id=%s", operation, status, issue, debug_id
    )

    if isinstance(error, OAuthProviderError):
        return PayPalError(502, "The payment provider rejected this merchant's credentials.",
                           provider_status=status)
    if status in (401, 403):
        return PayPalError(502, "The payment provider refused this merchant's credentials or permissions.",
                           provider_status=status, issue=issue, debug_id=debug_id)
    if status == 429:
        return PayPalError(503, "The payment provider is rate-limiting requests; retry shortly.",
                           provider_status=status, issue=issue, debug_id=debug_id)
    if status in (400, 422):
        return PayPalError(422, f"The payment provider declined the request ({issue or 'UNPROCESSABLE'}).",
                           provider_status=status, issue=issue, debug_id=debug_id)
    if status in (404, 409):
        return PayPalError(409, f"The payment provider reports a conflict ({issue or 'CONFLICT'}).",
                           provider_status=status, issue=issue, debug_id=debug_id)
    if 400 <= status < 500:
        return PayPalError(422, "The payment provider rejected the request.",
                           provider_status=status, issue=issue, debug_id=debug_id)
    return PayPalError(502, "The payment provider is unavailable.", outcome_unknown=True,
                       provider_status=status, issue=issue, debug_id=debug_id)


def _call(operation: str, fn: Callable[[], T]) -> T:
    try:
        return fn()
    except ApiError as e:
        raise provider_error(operation, e.status_code, e.error) from e
    except ValueError as e:  # pydantic.ValidationError subclasses ValueError: an unreadable 2xx body
        logger.error("PayPal %s returned an unreadable response (%s)", operation, type(e).__name__)
        raise PayPalError(502, "The payment provider returned an unreadable response.",
                          outcome_unknown=True) from None
    except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout, httpx2.ProxyError) as e:
        logger.error("PayPal %s was never sent (%s)", operation, type(e).__name__)
        raise PayPalError(502, "The payment provider could not be reached.") from e
    except httpx2.RequestError as e:
        logger.error("PayPal %s got no response (%s)", operation, type(e).__name__)
        raise PayPalError(504, "The payment provider did not respond; the outcome is unknown.",
                          outcome_unknown=True) from e


def _missing(operation: str, member: str) -> PayPalError:
    logger.error("PayPal %s response is missing %s", operation, member)
    return PayPalError(502, "The payment provider returned an incomplete response.", outcome_unknown=True)


def _build(fn: Callable[[], T]) -> T:
    """Build a request model from caller input. A ValidationError here may echo card data, so drop it."""
    try:
        return fn()
    except ValidationError:
        raise PayPalError(400, "The payment details are invalid.") from None


def _address(data: dict[str, str] | None) -> Address | UnsetType:
    if not data:
        return UNSET
    return Address(
        address_line_1=data.get("address_line_1") or UNSET,
        address_line_2=data.get("address_line_2") or UNSET,
        admin_area_2=data.get("city") or UNSET,
        admin_area_1=data.get("state") or UNSET,
        postal_code=data.get("postal_code") or UNSET,
        country_code=data["country_code"],
    )


def _needs_payer_action(status: object, links: list[LinkDescription] | UnsetType) -> bool:
    if status == OrderStatus.PAYER_ACTION_REQUIRED or _str(status) == "PAYER_ACTION_REQUIRED":
        return True
    return any(link.rel == "payer-action" for link in (_value(links) or []))


# --------------------------------------------------------------------------------------------------
# Orders: authorize a card payment
# --------------------------------------------------------------------------------------------------


def authorize(
    *,
    request_id: str,
    amount: str,
    currency: str,
    custom_id: str,
    description: str,
    card: CardInput | None = None,
    vault_id: str | None = None,
) -> AuthorizationResult:
    """Create an AUTHORIZE-intent PayPal order paid by a card (raw or vaulted) and return its hold."""
    if (card is None) == (vault_id is None):
        raise ValueError("exactly one of card / vault_id is required")

    def card_request() -> CardRequest:
        if vault_id is not None:
            return CardRequest(vault_id=vault_id)
        assert card is not None
        return CardRequest(
            number=card.number,
            expiry=card.expiry,
            security_code=card.security_code,
            name=card.name,
            billing_address=_address(card.billing_address),
        )

    body = _build(lambda: OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[PurchaseUnitRequest(
            amount=AmountWithBreakdown(currency_code=currency, value=amount),
            reference_id=custom_id,
            custom_id=custom_id,
            description=description[:127],
        )],
        payment_source=PaymentSource(card=card_request()),
    ))
    client = get_client()
    order = _call("create_order", lambda: client.orders.create_order(
        body, pay_pal_request_id=request_id, prefer=REPRESENTATION,
    ))
    order_id = _value(order.id)
    if not order_id:
        raise _missing("create_order", "id")

    status = order.status
    links = order.links
    purchase_units = order.purchase_units
    payment_source = order.payment_source
    if _needs_payer_action(status, links):
        return AuthorizationResult(order_id, "PAYER_ACTION_REQUIRED", None, None, None, None, None, None,
                                   None, None, payer_action_required=True)

    auth = _first_authorization(_value(purchase_units))
    if auth is None and _str(status) == OrderStatus.APPROVED.value:
        authorized = _call("authorize_order", lambda: client.orders.authorize_order(
            order_id, pay_pal_request_id=f"{request_id}-authorize", prefer=REPRESENTATION,
        ))
        status, purchase_units = authorized.status, authorized.purchase_units
        auth = _first_authorization(_value(purchase_units))
        brand, last_digits = _card_display(_value(authorized.payment_source))
    else:
        brand, last_digits = _card_display(_value(payment_source))

    if auth is None:
        # No hold was created (for example the order was declined outright).
        return AuthorizationResult(order_id, _str(status) or "UNKNOWN", None, None, None, None, None, None,
                                   brand, last_digits, payer_action_required=False)

    auth_id = _value(auth.id)
    if not auth_id:
        raise _missing("create_order", "authorization id")
    money = _value(auth.amount)
    return AuthorizationResult(
        paypal_order_id=order_id,
        order_status=_str(status) or "UNKNOWN",
        authorization_id=auth_id,
        authorization_status=_str(auth.status),
        amount=money.value if money else None,
        currency=money.currency_code if money else None,
        created_at=_value(auth.create_time),
        expires_at=_value(auth.expiration_time),
        card_brand=brand,
        card_last_digits=last_digits,
        payer_action_required=False,
    )


def _first_authorization(purchase_units: list[PurchaseUnit] | None) -> Any:
    if not purchase_units:
        return None
    payments = _value(purchase_units[0].payments)
    if payments is None:
        return None
    auths = _value(payments.authorizations)
    return auths[0] if auths else None


def _card_display(payment_source: Any) -> tuple[str | None, str | None]:
    if payment_source is None:
        return None, None
    card = _value(payment_source.card)
    if card is None:
        return None, None
    return _str(card.brand), _value(card.last_digits)


# --------------------------------------------------------------------------------------------------
# Payments: authorization lifecycle, capture, refund
# --------------------------------------------------------------------------------------------------


def _authorization_state(operation: str, auth: Any) -> AuthorizationState:
    auth_id = _value(auth.id)
    status = _str(auth.status)
    if not auth_id or not status:
        raise _missing(operation, "authorization id/status")
    money = _value(auth.amount)
    return AuthorizationState(
        id=auth_id,
        status=status,
        amount=money.value if money else None,
        currency=money.currency_code if money else None,
        created_at=_value(auth.create_time),
        expires_at=_value(auth.expiration_time),
    )


def get_authorization(authorization_id: str) -> AuthorizationState:
    client = get_client()
    auth = _call("get_authorized_payment",
                 lambda: client.payments.get_authorized_payment(authorization_id))
    return _authorization_state("get_authorized_payment", auth)


def reauthorize(authorization_id: str, *, request_id: str) -> AuthorizationState:
    client = get_client()
    auth = _call("reauthorize_payment", lambda: client.payments.reauthorize_payment(
        authorization_id, pay_pal_request_id=request_id, prefer=REPRESENTATION,
    ))
    return _authorization_state("reauthorize_payment", auth)


def void(authorization_id: str, *, request_id: str) -> AuthorizationState:
    client = get_client()
    auth = _call("void_payment", lambda: client.payments.void_payment(
        authorization_id, pay_pal_request_id=request_id, prefer=REPRESENTATION,
    ))
    return _authorization_state("void_payment", auth)


def capture(authorization_id: str, *, amount: str, currency: str, request_id: str,
            invoice_id: str | None = None) -> CaptureResult:
    client = get_client()
    body = CaptureRequest(
        amount=Money(currency_code=currency, value=amount),
        final_capture=True,
        invoice_id=invoice_id if invoice_id else UNSET,
    )
    cap = _call("capture_authorized_payment", lambda: client.payments.capture_authorized_payment(
        authorization_id, body=body, pay_pal_request_id=request_id, prefer=REPRESENTATION,
    ))
    capture_id = _value(cap.id)
    status = _str(cap.status)
    if not capture_id or not status:
        raise _missing("capture_authorized_payment", "capture id/status")
    money = _value(cap.amount)
    breakdown = _value(cap.seller_receivable_breakdown)
    fee = _value(breakdown.paypal_fee) if breakdown else None
    net = _value(breakdown.net_amount) if breakdown else None
    return CaptureResult(
        id=capture_id,
        status=status,
        amount=money.value if money else None,
        currency=money.currency_code if money else None,
        gross_amount=breakdown.gross_amount.value if breakdown else None,
        paypal_fee=fee.value if fee else None,
        net_amount=net.value if net else None,
    )


def refund(capture_id: str, *, amount: str, currency: str, request_id: str,
           note: str | None = None) -> RefundResult:
    client = get_client()
    body = RefundRequest(
        amount=Money(currency_code=currency, value=amount),
        note_to_payer=note[:255] if note else UNSET,
    )
    result = _call("refund_captured_payment", lambda: client.payments.refund_captured_payment(
        capture_id, body=body, pay_pal_request_id=request_id, prefer=REPRESENTATION,
    ))
    refund_id = _value(result.id)
    status = _str(result.status)
    if not refund_id or not status:
        raise _missing("refund_captured_payment", "refund id/status")
    money = _value(result.amount)
    return RefundResult(
        id=refund_id,
        status=status,
        amount=money.value if money else None,
        currency=money.currency_code if money else None,
    )


# --------------------------------------------------------------------------------------------------
# Vault: saved cards
# --------------------------------------------------------------------------------------------------


def vault_card(card: CardInput, *, request_id: str, paypal_customer_id: str | None,
               merchant_customer_id: str) -> VaultedCard:
    """Save a card in PayPal's vault, directly from card details (no browser step)."""
    customer = (Customer(id=paypal_customer_id) if paypal_customer_id
                else Customer(merchant_customer_id=merchant_customer_id))
    body = _build(lambda: PaymentTokenRequest(
        customer=customer,
        payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(
            number=card.number,
            expiry=card.expiry,
            security_code=card.security_code,
            name=card.name,
            billing_address=_address(card.billing_address),
        )),
    ))
    client = get_client()
    token = _call("create_payment_token", lambda: client.vault.create_payment_token(
        body, pay_pal_request_id=request_id,
    ))
    vault_id = _value(token.id)
    if not vault_id:
        raise _missing("create_payment_token", "id")
    links = _value(token.links) or []
    payer_action = any(link.rel == "payer-action" for link in links)
    token_customer = _value(token.customer)
    source = _value(token.payment_source)
    card_entity = _value(source.card) if source else None
    return VaultedCard(
        vault_id=vault_id,
        paypal_customer_id=_value(token_customer.id) if token_customer else None,
        brand=_str(card_entity.brand) if card_entity else None,
        last_digits=_value(card_entity.last_digits) if card_entity else None,
        expiry=_value(card_entity.expiry) if card_entity else None,
        payer_action_required=payer_action,
    )


def delete_vaulted_card(vault_id: str) -> None:
    """Delete a vaulted card. A token PayPal no longer knows (404) counts as already deleted."""
    client = get_client()
    result = _call("delete_payment_token",
                   lambda: client.vault.with_raw_response.delete_payment_token(vault_id))
    match result:
        case Success():
            return
        case Failure(status_code=404):
            logger.info("PayPal vault token already gone")
            return
        case Failure(error=err, status_code=status):
            raise provider_error("delete_payment_token", status, err)


# --------------------------------------------------------------------------------------------------
# Reporting: transaction search
# --------------------------------------------------------------------------------------------------


def search_transactions(*, start_date: str, end_date: str, page: int, page_size: int = 100) -> TransactionPage:
    """One page of PayPal's transaction report. Dates are RFC 3339 with seconds; a range spans <= 31 days."""
    client = get_client()
    response = _call("search_transactions", lambda: client.transaction_search.search_transactions(
        start_date, end_date,
        fields="transaction_info",
        # 'N' includes non-balance-affecting events too (authorizations, voids), not only money movement.
        balance_affecting_records_only="N",
        page_size=page_size,
        page=page,
    ))
    records: list[TransactionRecord] = []
    for detail in _value(response.transaction_details) or []:
        info = _value(detail.transaction_info)
        if info is None:
            continue
        transaction_id = _value(info.transaction_id)
        if not transaction_id:
            continue
        money = _value(info.transaction_amount)
        fee = _value(info.fee_amount)
        records.append(TransactionRecord(
            transaction_id=transaction_id,
            paypal_reference_id=_value(info.paypal_reference_id),
            event_code=_value(info.transaction_event_code),
            status=_value(info.transaction_status),
            initiated_at=_value(info.transaction_initiation_date),
            updated_at=_value(info.transaction_updated_date),
            amount=money.value if money else None,
            currency=money.currency_code if money else None,
            fee=fee.value if fee else None,
            invoice_id=_value(info.invoice_id),
            custom_field=_value(info.custom_field),
        ))
    return TransactionPage(
        records=records,
        page=_value(response.page) or page,
        total_pages=_value(response.total_pages) or 0,
        total_items=_value(response.total_items),
    )
