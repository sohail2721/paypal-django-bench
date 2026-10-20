"""
PayPal payment flows: authorize at checkout, capture at fulfilment, void on cancel, refund on a return,
saved cards, and reconciliation against PayPal's transaction report.

Every PayPal write follows one order: claim -> PayPal call -> record the result. The claim is a row whose
unique constraint makes the database refuse a second caller *before* it reaches PayPal; it is released
when PayPal definitively refuses the call, and kept (marked ``unknown``) when the outcome is unknown, so
a repeat of the request re-sends the same PayPal-Request-Id and PayPal answers with the original result.
"""

import logging
from datetime import timedelta, timezone as dt_timezone
from decimal import Decimal
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from orders.models import Order

from .models import (
    PayPalPayment,
    PayPalRefund,
    PayPalSettlement,
    PayPalVaultCustomer,
    SavedCard,
    SavedCardDeletion,
)
from .paypal import gateway
from .paypal.gateway import PayPalError

logger = logging.getLogger(__name__)

# PayPal: an authorization is honoured for 3 days; after that it can be reauthorized (new 3-day honour
# period) until 29 days after the original authorization.
HONOR_PERIOD = timedelta(days=3)
AUTHORIZATION_VALIDITY = timedelta(days=29)

HELD_AUTHORIZATION_STATUSES = {"CREATED", "PENDING"}
DEAD_AUTHORIZATION_STATUSES = {"VOIDED", "DENIED", "EXPIRED"}

# PayPal transaction search accepts at most 31 days per request.
SEARCH_WINDOW = timedelta(days=31)
MAX_RECONCILIATION_RANGE = timedelta(days=366)


class PaymentFlowError(Exception):
    """A business-rule failure with a message written for the API caller (shopper or operator)."""

    def __init__(self, status_code, message, *, code=None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


def _currency():
    return settings.PAYPAL_CURRENCY


def _parse_time(value):
    return parse_datetime(value) if value else None


def _to_amount(value, currency):
    """Validate an app amount fits the currency's minor unit and format it for PayPal."""
    formatted = gateway.format_amount(value, currency)
    if Decimal(formatted) != value:
        raise PaymentFlowError(
            422, f"Amount {value} cannot be expressed in {currency}'s minor unit.", code="invalid_amount"
        )
    return formatted


def _set_status(claim, status):
    """Mark a claim after an unexpected failure mid-call, so it is resolvable rather than stuck pending."""
    type(claim).objects.filter(pk=claim.pk).update(status=status)


def _flow_error_from(e: PayPalError, message=None, *, code="provider_error"):
    text = message or e.message
    return PaymentFlowError(e.status_code, text, code=code)


# --------------------------------------------------------------------------------------------------
# Pay (authorize)
# --------------------------------------------------------------------------------------------------


def pay_order(order, user, *, card=None, saved_card_id=None):
    """Put a hold on the order total (PayPal authorization). Never captures."""
    existing = PayPalPayment.objects.filter(order=order).first()
    if order.status != Order.OrderStatus.AWAITING_PAYMENT:
        if existing is not None and existing.status not in (PayPalPayment.Status.PENDING, PayPalPayment.Status.UNKNOWN):
            return existing, False  # repeat of a pay that already succeeded
        raise PaymentFlowError(
            409, f"Order is '{order.status}'; only orders awaiting payment can be paid.", code="invalid_state"
        )

    total = order.total_amount
    if total is None or total <= 0:
        raise PaymentFlowError(422, "Order has no payable total.", code="invalid_amount")
    currency = _currency()
    amount = _to_amount(total, currency)

    saved_card = None
    if saved_card_id is not None:
        saved_card = SavedCard.objects.filter(
            pk=saved_card_id, customer=user, status=SavedCard.Status.ACTIVE, vault_id__isnull=False
        ).first()
        if saved_card is None:
            raise PaymentFlowError(404, "Saved payment method not found.", code="payment_method_not_found")

    # Claim: one PayPalPayment per order (OneToOne). The second caller's INSERT is refused.
    try:
        with transaction.atomic():
            payment = PayPalPayment.objects.create(
                order=order, customer=order.customer, amount=total, currency=currency,
                saved_card=saved_card, status=PayPalPayment.Status.PENDING,
            )
    except IntegrityError:
        payment = PayPalPayment.objects.get(order=order)
        if payment.status == PayPalPayment.Status.PENDING:
            raise PaymentFlowError(409, "A payment for this order is already in progress.", code="in_progress")
        if payment.status != PayPalPayment.Status.UNKNOWN:
            return payment, False
        # The previous attempt's outcome is unknown: take it over (compare-and-set) and re-send it with the
        # same PayPal-Request-Id, so PayPal returns the original result instead of authorizing again.
        if not PayPalPayment.objects.filter(pk=payment.pk, status=PayPalPayment.Status.UNKNOWN).update(
            status=PayPalPayment.Status.PENDING
        ):
            raise PaymentFlowError(409, "A payment for this order is already in progress.", code="in_progress")
        payment.refresh_from_db()
        if saved_card is not None:
            payment.saved_card = saved_card

    try:
        result = gateway.authorize(
            request_id=payment.request_id,
            amount=amount,
            currency=currency,
            custom_id=str(order.id),
            description=f"Order {order.id}",
            card=card,
            vault_id=saved_card.vault_id if saved_card is not None else None,
        )
    except PayPalError as e:
        if e.outcome_unknown:
            payment.status = PayPalPayment.Status.UNKNOWN
            payment.last_error = e.message[:255]
            payment.save(update_fields=["status", "last_error", "updated"])
            raise _flow_error_from(
                e, "The payment provider did not confirm the payment. Retry the same request to resolve it."
            )
        payment.delete()  # release the claim: PayPal refused, nothing was held
        raise _flow_error_from(e, code="payment_declined" if e.status_code == 422 else "provider_error")
    except Exception:
        _set_status(payment, PayPalPayment.Status.UNKNOWN)
        raise

    if result.payer_action_required:
        payment.delete()
        logger.warning("PayPal asked for payer action (browser approval) on order %s", order.id)
        raise PaymentFlowError(
            402,
            "This card requires the shopper to approve the payment in a browser (e.g. 3-D Secure), "
            "which this API does not support. Use a different card.",
            code="payer_action_required",
        )

    if not result.authorization_id or result.authorization_status not in HELD_AUTHORIZATION_STATUSES:
        payment.delete()
        raise PaymentFlowError(
            402,
            f"The card was not authorized (PayPal status: {result.authorization_status or result.order_status}).",
            code="payment_declined",
        )

    if result.amount != amount or result.currency != currency:
        # Defensive: never keep a hold that does not equal the order total to the cent.
        logger.error("PayPal authorized %s %s for order %s, expected %s %s",
                     result.amount, result.currency, order.id, amount, currency)
        try:
            gateway.void(result.authorization_id, request_id=f"{payment.request_id}-mismatch-void")
        except PayPalError:
            logger.error("Could not void mismatched authorization for order %s", order.id)
        payment.delete()
        raise PaymentFlowError(502, "The payment provider held an unexpected amount; the hold was released.")

    now = timezone.now()
    authorized_at = _parse_time(result.created_at) or now
    with transaction.atomic():
        payment.status = PayPalPayment.Status.AUTHORIZED
        payment.paypal_order_id = result.paypal_order_id
        payment.paypal_order_status = result.order_status
        payment.authorization_id = result.authorization_id
        payment.authorization_status = result.authorization_status
        payment.authorized_at = authorized_at
        payment.honor_period_started_at = authorized_at
        payment.authorization_expires_at = (
            _parse_time(result.expires_at) or authorized_at + AUTHORIZATION_VALIDITY
        )
        payment.card_brand = (result.card_brand or (saved_card.brand if saved_card else ""))[:32]
        payment.card_last_digits = (result.card_last_digits or (saved_card.last_digits if saved_card else ""))[:4]
        payment.last_error = ""
        payment.save()
        order.status = Order.OrderStatus.AUTHORIZED
        order.save(update_fields=["status", "updated"])
    logger.info("Order %s authorized: PayPal authorization %s", order.id, payment.authorization_id)
    return payment, True


# --------------------------------------------------------------------------------------------------
# Fulfil (capture) and cancel (void)
# --------------------------------------------------------------------------------------------------


def _claim_settlement(payment, action, operator):
    """Take the one-per-payment settlement claim, or explain why it cannot be taken."""
    try:
        with transaction.atomic():
            return PayPalSettlement.objects.create(payment=payment, action=action, requested_by=operator), False
    except IntegrityError:
        settlement = PayPalSettlement.objects.get(payment=payment)
    if settlement.action != action:
        what = "fulfilled" if settlement.action == PayPalSettlement.Action.CAPTURE else "cancelled"
        raise PaymentFlowError(409, f"This order is already being {what} or was {what}.", code="invalid_state")
    if settlement.status == PayPalSettlement.Status.DONE:
        return settlement, True
    if settlement.status == PayPalSettlement.Status.UNKNOWN and PayPalSettlement.objects.filter(
        pk=settlement.pk, status=PayPalSettlement.Status.UNKNOWN
    ).update(status=PayPalSettlement.Status.PENDING):
        settlement.refresh_from_db()
        return settlement, False
    raise PaymentFlowError(409, "This order is already being processed; retry shortly.", code="in_progress")


def _mark_unknown(settlement, payment, e):
    settlement.status = PayPalSettlement.Status.UNKNOWN
    settlement.save(update_fields=["status", "updated"])
    payment.last_error = e.message[:255]
    payment.save(update_fields=["last_error", "updated"])


def _not_renewable(payment, detail):
    return PaymentFlowError(
        409,
        "The payment hold on this order has expired and can no longer be renewed"
        f"{f' ({detail})' if detail else ''}. The funds cannot be captured: cancel the order and ask the "
        "shopper to place and pay for a new one.",
        code="authorization_not_renewable",
    )


def _renew_authorization(payment, settlement):
    """Reauthorize a stale hold; raise an operator-actionable error when PayPal will not renew it."""
    old_id = payment.authorization_id
    try:
        state = gateway.reauthorize(old_id, request_id=settlement.request_id(f"reauth-{old_id}"))
    except PayPalError as e:
        if e.outcome_unknown:
            _mark_unknown(settlement, payment, e)
            raise _flow_error_from(e, "The payment provider did not confirm renewing the hold. Retry fulfilment.")
        settlement.delete()
        payment.last_error = f"reauthorize refused: {e.issue or e.provider_status}"[:255]
        payment.save(update_fields=["last_error", "updated"])
        raise _not_renewable(payment, f"PayPal: {e.issue}" if e.issue else None)
    if state.status not in HELD_AUTHORIZATION_STATUSES:
        settlement.delete()
        raise _not_renewable(payment, f"PayPal status: {state.status}")
    payment.authorization_id = state.id
    payment.authorization_status = state.status
    payment.honor_period_started_at = _parse_time(state.created_at) or timezone.now()
    payment.reauthorization_count += 1
    payment.save(update_fields=[
        "authorization_id", "authorization_status", "honor_period_started_at", "reauthorization_count", "updated",
    ])
    logger.info("Order %s: authorization %s renewed as %s", payment.order_id, old_id, state.id)
    return state.id


def fulfil_order(order, operator):
    """Mark the order fulfilled and capture the held funds. Renews a stale hold first."""
    payment = PayPalPayment.objects.filter(order=order).first()
    if payment is None:
        raise PaymentFlowError(409, "This order has no PayPal payment to capture.", code="invalid_state")
    if payment.capture_id:
        return payment, False  # already captured
    if payment.status == PayPalPayment.Status.EXPIRED:
        raise _not_renewable(payment, None)
    if payment.status != PayPalPayment.Status.AUTHORIZED or order.status != Order.OrderStatus.AUTHORIZED:
        raise PaymentFlowError(
            409, f"Order is '{order.status}'; only authorized orders can be fulfilled.", code="invalid_state"
        )

    settlement, done = _claim_settlement(payment, PayPalSettlement.Action.CAPTURE, operator)
    if done:
        payment.refresh_from_db()
        return payment, False
    try:
        return _fulfil_claimed(order, payment, settlement)
    except PaymentFlowError:
        raise
    except Exception:
        _set_status(settlement, PayPalSettlement.Status.UNKNOWN)
        raise


def _fulfil_claimed(order, payment, settlement):
    now = timezone.now()
    original = payment.authorized_at or payment.created
    if (payment.authorization_expires_at and now >= payment.authorization_expires_at) or (
        now >= original + AUTHORIZATION_VALIDITY
    ):
        settlement.delete()
        payment.status = PayPalPayment.Status.EXPIRED
        payment.save(update_fields=["status", "updated"])
        raise _not_renewable(payment, "the 29-day authorization period has passed")

    # Refresh what PayPal holds before acting on it.
    try:
        state = gateway.get_authorization(payment.authorization_id)
    except PayPalError as e:
        settlement.delete()
        raise _flow_error_from(e)
    payment.authorization_status = state.status
    payment.save(update_fields=["authorization_status", "updated"])
    if state.status in DEAD_AUTHORIZATION_STATUSES:
        settlement.delete()
        payment.status = PayPalPayment.Status.EXPIRED
        payment.save(update_fields=["status", "updated"])
        raise _not_renewable(payment, f"PayPal status: {state.status}")

    renewed = False
    if now >= (payment.honor_period_started_at or original) + HONOR_PERIOD:
        _renew_authorization(payment, settlement)
        renewed = True

    try:
        result = _capture(payment, settlement)
    except PayPalError as e:
        if e.outcome_unknown:
            _mark_unknown(settlement, payment, e)
            raise _flow_error_from(e, "The payment provider did not confirm the capture. Retry fulfilment.")
        if e.provider_status == 422 and not renewed and now >= original + HONOR_PERIOD:
            # PayPal refused to capture a hold past its honour period: renew it once and capture again.
            _renew_authorization(payment, settlement)
            try:
                result = _capture(payment, settlement)
            except PayPalError as e2:
                if e2.outcome_unknown:
                    _mark_unknown(settlement, payment, e2)
                    raise _flow_error_from(e2, "The payment provider did not confirm the capture. Retry fulfilment.")
                settlement.delete()
                raise _flow_error_from(e2, f"PayPal refused the capture ({e2.issue or e2.provider_status}).")
        else:
            settlement.delete()
            payment.last_error = f"capture refused: {e.issue or e.provider_status}"[:255]
            payment.save(update_fields=["last_error", "updated"])
            raise _flow_error_from(e, f"PayPal refused the capture ({e.issue or e.provider_status}).")

    if result.status in ("DECLINED", "FAILED"):
        settlement.delete()
        payment.capture_status = result.status
        payment.save(update_fields=["capture_status", "updated"])
        raise PaymentFlowError(402, f"PayPal could not capture the payment (status: {result.status}).",
                               code="capture_failed")

    with transaction.atomic():
        payment.capture_id = result.id
        payment.capture_status = result.status
        payment.captured_amount = Decimal(result.amount) if result.amount else payment.amount
        payment.paypal_fee = Decimal(result.paypal_fee) if result.paypal_fee else None
        payment.net_amount = Decimal(result.net_amount) if result.net_amount else None
        payment.captured_at = timezone.now()
        payment.authorization_status = "CAPTURED"
        payment.status = (PayPalPayment.Status.CAPTURED if result.status == "COMPLETED"
                          else PayPalPayment.Status.CAPTURE_PENDING)
        payment.last_error = ""
        payment.save()
        settlement.status = PayPalSettlement.Status.DONE
        settlement.save(update_fields=["status", "updated"])
        order.status = Order.OrderStatus.FULFILLED
        order.save(update_fields=["status", "updated"])
    logger.info("Order %s fulfilled: PayPal capture %s (%s)", order.id, payment.capture_id, result.status)
    return payment, True


def _capture(payment, settlement):
    return gateway.capture(
        payment.authorization_id,
        amount=_to_amount(payment.amount, payment.currency),
        currency=payment.currency,
        request_id=settlement.request_id(payment.authorization_id),
    )


def cancel_order(order, operator):
    """Cancel before fulfilment: release the shopper's held funds (void). No money moves."""
    if order.status == Order.OrderStatus.CANCELLED:
        return order, False
    payment = PayPalPayment.objects.filter(order=order).first()
    if payment is None:
        if order.status != Order.OrderStatus.AWAITING_PAYMENT:
            raise PaymentFlowError(409, f"Order is '{order.status}' and cannot be cancelled.", code="invalid_state")
        order.status = Order.OrderStatus.CANCELLED
        order.save(update_fields=["status", "updated"])
        return order, True
    if payment.status in (PayPalPayment.Status.PENDING, PayPalPayment.Status.UNKNOWN):
        raise PaymentFlowError(
            409, "A payment attempt for this order has not settled yet; retry once it has.", code="in_progress"
        )
    if payment.status not in (PayPalPayment.Status.AUTHORIZED, PayPalPayment.Status.EXPIRED):
        raise PaymentFlowError(
            409, "The payment was already captured; refund the order instead of cancelling it.",
            code="invalid_state",
        )

    settlement, done = _claim_settlement(payment, PayPalSettlement.Action.VOID, operator)
    if not done and payment.status == PayPalPayment.Status.AUTHORIZED:
        try:
            state = gateway.void(payment.authorization_id,
                                 request_id=settlement.request_id(payment.authorization_id))
            payment.authorization_status = state.status
        except PayPalError as e:
            if e.outcome_unknown:
                _mark_unknown(settlement, payment, e)
                raise _flow_error_from(e, "The payment provider did not confirm releasing the hold. Retry.")
            # A hold that is already gone at PayPal (voided or expired) has released the funds anyway.
            try:
                current = gateway.get_authorization(payment.authorization_id)
            except PayPalError:
                current = None
            if current is None or current.status not in DEAD_AUTHORIZATION_STATUSES:
                settlement.delete()
                raise _flow_error_from(e, f"PayPal refused to release the hold ({e.issue or e.provider_status}).")
            payment.authorization_status = current.status
        except Exception:
            _set_status(settlement, PayPalSettlement.Status.UNKNOWN)
            raise

    with transaction.atomic():
        payment.status = PayPalPayment.Status.VOIDED
        payment.voided_at = payment.voided_at or timezone.now()
        payment.save()
        settlement.status = PayPalSettlement.Status.DONE
        settlement.save(update_fields=["status", "updated"])
        order.status = Order.OrderStatus.CANCELLED
        order.save(update_fields=["status", "updated"])
    logger.info("Order %s cancelled: authorization %s voided", order.id, payment.authorization_id)
    return order, True


# --------------------------------------------------------------------------------------------------
# Refunds
# --------------------------------------------------------------------------------------------------

_REFUND_STATUS = {
    "COMPLETED": PayPalRefund.Status.COMPLETED,
    "PENDING": PayPalRefund.Status.PROVIDER_PENDING,
    "FAILED": PayPalRefund.Status.FAILED,
    "CANCELLED": PayPalRefund.Status.CANCELLED,
}


def refund_order(order, user, *, idempotency_key, amount=None, reason=""):
    """Refund a captured payment, in full (amount=None: whatever is left) or in part."""
    payment = PayPalPayment.objects.filter(order=order).first()
    if payment is None or not payment.capture_id:
        raise PaymentFlowError(
            409, "Only fulfilled (captured) orders can be refunded; cancel an unfulfilled order instead.",
            code="invalid_state",
        )
    if payment.status not in (PayPalPayment.Status.CAPTURED, PayPalPayment.Status.PARTIALLY_REFUNDED,
                              PayPalPayment.Status.REFUNDED):
        raise PaymentFlowError(409, f"The payment is '{payment.status}' and cannot be refunded yet.",
                               code="invalid_state")
    if amount is not None:
        _to_amount(amount, payment.currency)

    # Claim: (payment, idempotency_key) is unique. The over-refund guard runs in the same transaction,
    # after the INSERT has taken SQLite's write lock, so concurrent distinct-key refunds serialize here.
    try:
        with transaction.atomic():
            refund = PayPalRefund.objects.create(
                payment=payment, idempotency_key=idempotency_key, amount=amount or Decimal("0"),
                currency=payment.currency, reason=reason[:255], requested_by=user,
            )
            already = PayPalRefund.objects.filter(payment=payment).exclude(pk=refund.pk).exclude(
                status__in=[PayPalRefund.Status.FAILED, PayPalRefund.Status.CANCELLED]
            ).values_list("amount", flat=True)
            remaining = payment.captured_amount - sum(already, Decimal("0"))
            if amount is None:
                amount = remaining
            if remaining <= 0:
                raise PaymentFlowError(422, "This order has already been refunded in full.", code="fully_refunded")
            if amount <= 0:
                raise PaymentFlowError(422, "Refund amount must be positive.", code="invalid_amount")
            if amount > remaining:
                raise PaymentFlowError(
                    422, f"Refund of {amount} exceeds the refundable balance of {remaining} {payment.currency}.",
                    code="exceeds_refundable",
                )
            refund.amount = amount
            refund.save(update_fields=["amount"])
    except IntegrityError:
        refund = PayPalRefund.objects.get(payment=payment, idempotency_key=idempotency_key)
        if amount is not None and refund.amount != amount:
            raise PaymentFlowError(
                409, "This idempotency key was already used for a refund of a different amount.",
                code="idempotency_key_reused",
            )
        if refund.status == PayPalRefund.Status.PENDING:
            raise PaymentFlowError(409, "This refund is already in progress.", code="in_progress")
        if refund.status != PayPalRefund.Status.UNKNOWN:
            return refund, False
        if not PayPalRefund.objects.filter(pk=refund.pk, status=PayPalRefund.Status.UNKNOWN).update(
            status=PayPalRefund.Status.PENDING
        ):
            raise PaymentFlowError(409, "This refund is already in progress.", code="in_progress")
        refund.refresh_from_db()

    try:
        result = gateway.refund(
            payment.capture_id,
            amount=_to_amount(refund.amount, refund.currency),
            currency=refund.currency,
            request_id=refund.request_id,
            note=refund.reason or None,
        )
    except PayPalError as e:
        if e.outcome_unknown:
            refund.status = PayPalRefund.Status.UNKNOWN
            refund.save(update_fields=["status", "updated"])
            raise _flow_error_from(
                e, "The payment provider did not confirm the refund. Repeat the request with the same "
                   "idempotency key to resolve it."
            )
        refund.delete()  # release the claim: PayPal refused, nothing was refunded
        raise _flow_error_from(e, f"PayPal refused the refund ({e.issue or e.provider_status}).")
    except Exception:
        _set_status(refund, PayPalRefund.Status.UNKNOWN)
        raise

    with transaction.atomic():
        refund.paypal_refund_id = result.id
        refund.paypal_status = result.status
        refund.status = _REFUND_STATUS.get(result.status, PayPalRefund.Status.PROVIDER_PENDING)
        refund.save()
        payment.refresh_from_db()
        if payment.refundable_amount <= 0:
            payment.status, order.status = PayPalPayment.Status.REFUNDED, Order.OrderStatus.REFUNDED
        else:
            payment.status = PayPalPayment.Status.PARTIALLY_REFUNDED
            order.status = Order.OrderStatus.PARTIALLY_REFUNDED
        payment.save(update_fields=["status", "updated"])
        order.save(update_fields=["status", "updated"])
    logger.info("Order %s refund %s: PayPal refund %s (%s)", order.id, refund.pk, result.id, result.status)
    return refund, True


# --------------------------------------------------------------------------------------------------
# Saved cards
# --------------------------------------------------------------------------------------------------


def save_card(user, card, *, request_key):
    """Vault a card at PayPal for this shopper. Only PayPal's token id and display data are stored."""
    try:
        with transaction.atomic():
            saved = SavedCard.objects.create(customer=user, request_key=request_key)
    except IntegrityError:
        saved = SavedCard.objects.get(customer=user, request_key=request_key)
        if saved.status == SavedCard.Status.ACTIVE:
            return saved, False
        if saved.status == SavedCard.Status.PENDING:
            raise PaymentFlowError(409, "This card is already being saved.", code="in_progress")
        raise PaymentFlowError(409, "This idempotency key belongs to a deleted card.", code="idempotency_key_reused")

    # Claim the shopper's PayPal vault customer, so two first-time saves cannot create two of them.
    vault_customer_created = False
    try:
        with transaction.atomic():
            vault_customer = PayPalVaultCustomer.objects.create(customer=user)
            vault_customer_created = True
    except IntegrityError:
        vault_customer = PayPalVaultCustomer.objects.get(customer=user)
        if not vault_customer.paypal_customer_id:
            saved.delete()
            raise PaymentFlowError(409, "Another card is being saved for this account; retry shortly.",
                                   code="in_progress")

    try:
        result = gateway.vault_card(
            card,
            request_id=saved.request_id,
            paypal_customer_id=vault_customer.paypal_customer_id or None,
            merchant_customer_id=str(user.pk),
        )
    except PayPalError as e:
        if e.outcome_unknown:
            logger.warning("Vaulting a card for customer %s had an unknown outcome", user.pk)
        saved.delete()
        if vault_customer_created:
            vault_customer.delete()
        raise _flow_error_from(e, f"PayPal could not save the card ({e.issue or e.provider_status}).")

    if result.payer_action_required:
        try:
            gateway.delete_vaulted_card(result.vault_id)
        except PayPalError:
            logger.error("Could not delete a vault token that required payer action")
        saved.delete()
        if vault_customer_created:
            vault_customer.delete()
        raise PaymentFlowError(
            402, "This card requires the shopper to approve it in a browser, which this API does not support.",
            code="payer_action_required",
        )

    with transaction.atomic():
        if vault_customer_created or not vault_customer.paypal_customer_id:
            vault_customer.paypal_customer_id = result.paypal_customer_id or ""
            vault_customer.save(update_fields=["paypal_customer_id", "updated"])
        saved.vault_id = result.vault_id
        saved.brand = (result.brand or "")[:32]
        saved.last_digits = (result.last_digits or card.number[-4:])[:4]
        saved.expiry = (result.expiry or card.expiry)[:7]
        saved.status = SavedCard.Status.ACTIVE
        saved.save()
    logger.info("Customer %s saved card %s (%s ending %s)", user.pk, saved.pk, saved.brand, saved.last_digits)
    return saved, True


def delete_card(user, saved_card_id):
    """Remove a saved card at PayPal and locally. It can no longer be listed or used to pay."""
    saved = SavedCard.objects.filter(pk=saved_card_id, customer=user).exclude(status=SavedCard.Status.DELETED).first()
    if saved is None:
        raise PaymentFlowError(404, "Saved payment method not found.", code="payment_method_not_found")
    if saved.status == SavedCard.Status.PENDING:
        raise PaymentFlowError(409, "This card is still being saved; retry shortly.", code="in_progress")

    try:
        with transaction.atomic():
            SavedCardDeletion.objects.create(saved_card=saved)
            SavedCard.objects.filter(pk=saved.pk).update(status=SavedCard.Status.DELETING)
    except IntegrityError:
        return  # already deleted or being deleted

    try:
        gateway.delete_vaulted_card(saved.vault_id)
    except PayPalError as e:
        with transaction.atomic():
            SavedCardDeletion.objects.filter(saved_card=saved).delete()
            SavedCard.objects.filter(pk=saved.pk).update(status=SavedCard.Status.ACTIVE)
        raise _flow_error_from(e, f"PayPal could not delete the card ({e.issue or e.provider_status}).")

    SavedCard.objects.filter(pk=saved.pk).update(status=SavedCard.Status.DELETED, deleted_at=timezone.now())
    logger.info("Customer %s deleted saved card %s", user.pk, saved.pk)


# --------------------------------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------------------------------


def _paypal_time(value):
    return value.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_paypal_transactions(start, end):
    """Every transaction PayPal reports for [start, end]: all 31-day windows, every page of each."""
    records, pages = {}, 0
    window_start = start
    while window_start < end:
        window_end = min(window_start + SEARCH_WINDOW, end)
        page = 1
        while True:
            result = gateway.search_transactions(
                start_date=_paypal_time(window_start), end_date=_paypal_time(window_end), page=page
            )
            pages += 1
            for record in result.records:
                records.setdefault(record.transaction_id, record)  # windows share a boundary second
            if page >= result.total_pages or not result.records:
                break
            page += 1
        window_start = window_end
    return list(records.values()), pages


def _chunks(values, size=500):
    values = list(values)
    for i in range(0, len(values), size):
        yield values[i:i + size]


def reconcile(start, end):
    if end <= start:
        raise PaymentFlowError(400, "'to' must be later than 'from'.", code="invalid_range")
    now = timezone.now()
    end = min(end, now)  # PayPal reports up to now
    if end <= start:
        raise PaymentFlowError(400, "'from' must be in the past.", code="invalid_range")
    if end - start > MAX_RECONCILIATION_RANGE:
        raise PaymentFlowError(400, "The range may span at most 366 days.", code="invalid_range")

    try:
        remote, pages = fetch_paypal_transactions(start, end)
    except PayPalError as e:
        raise _flow_error_from(e, "PayPal's transaction report is unavailable; retry later.")

    remote_ids = {r.transaction_id for r in remote}
    local_by_id = {}  # PayPal id -> (order id, kind, local amount)
    for chunk in _chunks(remote_ids):
        for p in PayPalPayment.objects.filter(Q(authorization_id__in=chunk) | Q(capture_id__in=chunk)):
            if p.authorization_id in remote_ids:
                local_by_id[p.authorization_id] = (str(p.order_id), "authorization", p.amount)
            if p.capture_id in remote_ids:
                local_by_id[p.capture_id] = (str(p.order_id), "capture", p.captured_amount)
        for r in PayPalRefund.objects.filter(paypal_refund_id__in=chunk).select_related("payment"):
            local_by_id[r.paypal_refund_id] = (str(r.payment.order_id), "refund", r.amount)
    custom_ids = {r.custom_field for r in remote if r.custom_field}
    known_orders = set()
    for chunk in _chunks(custom_ids):
        known_orders.update(
            str(o) for o in PayPalPayment.objects.filter(order_id__in=[c for c in chunk if _is_uuid(c)])
            .values_list("order_id", flat=True)
        )

    matched, unknown_to_app = [], []
    for r in sorted(remote, key=lambda r: r.initiated_at or ""):
        row = {
            "transactionId": r.transaction_id,
            "paypalReferenceId": r.paypal_reference_id,
            "eventCode": r.event_code,
            "status": r.status,
            "amount": r.amount,
            "currency": r.currency,
            "fee": r.fee,
            "initiatedAt": r.initiated_at,
            "customField": r.custom_field,
        }
        local = local_by_id.get(r.transaction_id)
        if local is not None:
            order_id, kind, local_amount = local
            row.update({"orderId": order_id, "kind": kind,
                        "localAmount": str(local_amount) if local_amount is not None else None})
            matched.append(row)
        elif r.custom_field in known_orders:
            row.update({"orderId": r.custom_field, "kind": "related"})
            matched.append(row)
        else:
            unknown_to_app.append(row)

    missing_at_paypal = []
    payments = PayPalPayment.objects.filter(
        Q(authorized_at__range=(start, end)) | Q(captured_at__range=(start, end))
    ).exclude(authorization_id="")
    for p in payments:
        if p.authorized_at and start <= p.authorized_at <= end and p.authorization_id not in remote_ids \
                and str(p.order_id) not in custom_ids:
            missing_at_paypal.append({"orderId": str(p.order_id), "kind": "authorization",
                                      "paypalId": p.authorization_id, "amount": str(p.amount),
                                      "at": p.authorized_at.isoformat()})
        if p.capture_id and p.captured_at and start <= p.captured_at <= end and p.capture_id not in remote_ids:
            missing_at_paypal.append({"orderId": str(p.order_id), "kind": "capture", "paypalId": p.capture_id,
                                      "amount": str(p.captured_amount), "at": p.captured_at.isoformat()})
    for r in PayPalRefund.objects.filter(created__range=(start, end)).exclude(paypal_refund_id="").select_related("payment"):
        if r.paypal_refund_id not in remote_ids:
            missing_at_paypal.append({"orderId": str(r.payment.order_id), "kind": "refund",
                                      "paypalId": r.paypal_refund_id, "amount": str(r.amount),
                                      "at": r.created.isoformat()})

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "pagesFetched": pages,
        "summary": {
            "paypalTransactions": len(remote),
            "matched": len(matched),
            "unknownToApp": len(unknown_to_app),
            "missingAtPayPal": len(missing_at_paypal),
        },
        "matched": matched,
        "unknownToApp": unknown_to_app,
        "missingAtPayPal": missing_at_paypal,
        "note": "PayPal's transaction report can lag live activity by up to three hours; very recent "
                "local payments may appear under missingAtPayPal until PayPal reports them.",
    }


def _is_uuid(value):
    try:
        UUID(value)
        return True
    except (ValueError, TypeError):
        return False
