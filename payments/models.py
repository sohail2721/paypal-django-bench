from decimal import Decimal
from uuid import uuid4

from django.db import models

from catalogue.abstract import Timestamp


# Create your models here.
class Transaction(Timestamp):
    TRANSACTION_STATUS = [
        ("successful", "Successful"),
        ("failed", "Failed"),
        ("pending", "Pending"),
        ("refunded", "Refunded"),
    ]

    order = models.OneToOneField("orders.Order", on_delete=models.CASCADE)
    transaction_id = models.CharField(max_length=255, blank=True, unique=True)
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2)
    customer = models.ForeignKey("customers.Customer", on_delete=models.CASCADE)
    date_paid = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=50, default="pending", choices=TRANSACTION_STATUS)

    def __str__(self):
        return f"Payment for Order: {self.order}"


# PayPal
#
# Each row below that is created *before* a PayPal call is a claim: a unique constraint makes the
# database refuse a second claim for the same operation, so a double-click never reaches PayPal twice.
# Card numbers and security codes are never stored; only PayPal's ids/status and safe card display data.
class PayPalPayment(Timestamp):
    """The PayPal side of one order: the hold (authorization) and the capture taken at fulfilment."""

    class Status(models.TextChoices):
        PENDING = "pending"  # claim taken, authorization in flight
        UNKNOWN = "unknown"  # outcome of the PayPal call unknown (no response); resolved on retry
        AUTHORIZED = "authorized"
        CAPTURE_PENDING = "capture_pending"
        CAPTURED = "captured"
        PARTIALLY_REFUNDED = "partially_refunded"
        REFUNDED = "refunded"
        VOIDED = "voided"
        EXPIRED = "expired"  # the hold lapsed and can no longer be renewed

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    order = models.OneToOneField("orders.Order", on_delete=models.PROTECT, related_name="paypal_payment")
    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.PROTECT, related_name="paypal_payments"
    )
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING)
    currency = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    saved_card = models.ForeignKey(
        "payments.SavedCard", on_delete=models.SET_NULL, null=True, blank=True, related_name="payments"
    )
    card_brand = models.CharField(max_length=32, blank=True)
    card_last_digits = models.CharField(max_length=4, blank=True)

    # PayPal order + authorization (the hold)
    paypal_order_id = models.CharField(max_length=64, blank=True, db_index=True)
    paypal_order_status = models.CharField(max_length=32, blank=True)
    authorization_id = models.CharField(max_length=64, blank=True, db_index=True)
    authorization_status = models.CharField(max_length=32, blank=True)
    authorized_at = models.DateTimeField(null=True, blank=True)  # the original authorization
    honor_period_started_at = models.DateTimeField(null=True, blank=True)  # the latest (re)authorization
    authorization_expires_at = models.DateTimeField(null=True, blank=True)
    reauthorization_count = models.PositiveSmallIntegerField(default=0)

    # Capture (taken at fulfilment), as PayPal reported it
    capture_id = models.CharField(max_length=64, blank=True, db_index=True)
    capture_status = models.CharField(max_length=32, blank=True)
    captured_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    paypal_fee = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    net_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)

    last_error = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return f"PayPal payment for order {self.order_id}"

    @property
    def request_id(self):
        """PayPal-Request-Id for this payment's PayPal order: stable across retries of the same claim."""
        return f"pay-{self.pk}"

    @property
    def refunded_amount(self):
        total = self.refunds.exclude(
            status__in=[PayPalRefund.Status.FAILED, PayPalRefund.Status.CANCELLED]
        ).aggregate(total=models.Sum("amount"))["total"]
        return total or Decimal("0")

    @property
    def refundable_amount(self):
        if self.captured_amount is None:
            return Decimal("0")
        return self.captured_amount - self.refunded_amount


class PayPalSettlement(Timestamp):
    """Claim for settling a hold exactly once: capture it (fulfil) or void it (cancel), never both."""

    class Action(models.TextChoices):
        CAPTURE = "capture"
        VOID = "void"

    class Status(models.TextChoices):
        PENDING = "pending"
        UNKNOWN = "unknown"
        DONE = "done"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    payment = models.OneToOneField(PayPalPayment, on_delete=models.CASCADE, related_name="settlement")
    action = models.CharField(max_length=16, choices=Action.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    requested_by = models.ForeignKey(
        "customers.Customer", on_delete=models.SET_NULL, null=True, related_name="+"
    )

    def __str__(self):
        return f"{self.action} of {self.payment_id}"

    def request_id(self, authorization_id):
        return f"{self.action}-{self.pk}-{authorization_id}"


class PayPalRefund(Timestamp):
    """One refund of a captured payment. ``(payment, idempotency_key)`` is the claim."""

    class Status(models.TextChoices):
        PENDING = "pending"  # claim taken, refund in flight
        UNKNOWN = "unknown"  # outcome of the PayPal call unknown; repeating the same key resolves it
        PROVIDER_PENDING = "provider_pending"  # PayPal accepted it but has not completed it yet
        COMPLETED = "completed"
        FAILED = "failed"
        CANCELLED = "cancelled"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    payment = models.ForeignKey(PayPalPayment, on_delete=models.PROTECT, related_name="refunds")
    idempotency_key = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)
    reason = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING)
    paypal_refund_id = models.CharField(max_length=64, blank=True, db_index=True)
    paypal_status = models.CharField(max_length=32, blank=True)
    requested_by = models.ForeignKey(
        "customers.Customer", on_delete=models.SET_NULL, null=True, related_name="+"
    )

    class Meta:
        ordering = ["created"]
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "idempotency_key"], name="uniq_refund_idempotency_key"
            ),
        ]

    def __str__(self):
        return f"Refund {self.amount} of {self.payment_id}"

    @property
    def request_id(self):
        return f"refund-{self.pk}"


class PayPalVaultCustomer(Timestamp):
    """The PayPal vault customer a shopper's saved cards live under. The OneToOne is the claim."""

    customer = models.OneToOneField(
        "customers.Customer", on_delete=models.CASCADE, related_name="paypal_vault"
    )
    paypal_customer_id = models.CharField(max_length=64, blank=True)

    def __str__(self):
        return f"PayPal vault customer for {self.customer_id}"


class SavedCard(Timestamp):
    """A card saved in PayPal's vault. Only PayPal's token id and safe display data are kept."""

    class Status(models.TextChoices):
        PENDING = "pending"
        ACTIVE = "active"
        DELETING = "deleting"
        DELETED = "deleted"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.CASCADE, related_name="saved_cards"
    )
    request_key = models.CharField(max_length=255)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    vault_id = models.CharField(max_length=64, null=True, blank=True, unique=True)
    brand = models.CharField(max_length=32, blank=True)
    last_digits = models.CharField(max_length=4, blank=True)
    expiry = models.CharField(max_length=7, blank=True)  # YYYY-MM
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created"]
        constraints = [
            models.UniqueConstraint(
                fields=["customer", "request_key"], name="uniq_saved_card_request_key"
            ),
        ]

    def __str__(self):
        return f"{self.brand} ending {self.last_digits}"

    @property
    def request_id(self):
        return f"vault-{self.pk}"


class SavedCardDeletion(Timestamp):
    """Claim for deleting a saved card exactly once."""

    saved_card = models.OneToOneField(SavedCard, on_delete=models.CASCADE, related_name="deletion")

    def __str__(self):
        return f"Deletion of {self.saved_card_id}"
