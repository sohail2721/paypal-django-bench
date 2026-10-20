import re
from datetime import date
from decimal import Decimal

from django.conf import settings
from rest_framework import serializers

from .models import PayPalPayment, PayPalRefund, SavedCard
from .paypal.gateway import CardInput, currency_exponent


def _luhn_ok(number):
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


class BillingAddressSerializer(serializers.Serializer):
    addressLine1 = serializers.CharField(max_length=300, required=False, allow_blank=True)
    addressLine2 = serializers.CharField(max_length=300, required=False, allow_blank=True)
    city = serializers.CharField(max_length=120, required=False, allow_blank=True)
    state = serializers.CharField(max_length=300, required=False, allow_blank=True)
    postalCode = serializers.CharField(max_length=60, required=False, allow_blank=True)
    countryCode = serializers.RegexField(r"^[A-Za-z]{2}$", max_length=2)

    def to_gateway(self, data):
        return {
            "address_line_1": data.get("addressLine1", ""),
            "address_line_2": data.get("addressLine2", ""),
            "city": data.get("city", ""),
            "state": data.get("state", ""),
            "postal_code": data.get("postalCode", ""),
            "country_code": data["countryCode"].upper(),
        }


class CardSerializer(serializers.Serializer):
    """
    Raw card details. Validation messages never echo the submitted values, and the validated card only
    lives in memory for the PayPal call (it is never saved or logged).
    """

    number = serializers.CharField(max_length=32, write_only=True)
    expiry = serializers.CharField(max_length=7, help_text="YYYY-MM (MM/YY and MM/YYYY also accepted)")
    securityCode = serializers.CharField(max_length=4, min_length=3, write_only=True)
    name = serializers.CharField(max_length=300)
    billingAddress = BillingAddressSerializer(required=False)

    def validate_number(self, value):
        digits = re.sub(r"[\s-]", "", value)
        if not digits.isdigit() or not 12 <= len(digits) <= 19 or not _luhn_ok(digits):
            raise serializers.ValidationError("Enter a valid card number.")
        return digits

    def validate_securityCode(self, value):
        if not value.isdigit():
            raise serializers.ValidationError("Enter a valid security code.")
        return value

    def validate_expiry(self, value):
        value = value.strip()
        if m := re.fullmatch(r"(\d{4})-(\d{2})", value):
            year, month = int(m[1]), int(m[2])
        elif m := re.fullmatch(r"(\d{2})/(\d{2}|\d{4})", value):
            month, year = int(m[1]), int(m[2])
            year += 2000 if year < 100 else 0
        else:
            raise serializers.ValidationError("Use the format YYYY-MM.")
        if not 1 <= month <= 12:
            raise serializers.ValidationError("Use the format YYYY-MM.")
        today = date.today()
        if (year, month) < (today.year, today.month):
            raise serializers.ValidationError("The card has expired.")
        return f"{year:04d}-{month:02d}"

    @staticmethod
    def to_card_input(data):
        address = data.get("billingAddress")
        return CardInput(
            number=data["number"],
            expiry=data["expiry"],
            security_code=data["securityCode"],
            name=data["name"],
            billing_address=BillingAddressSerializer().to_gateway(address) if address else None,
        )


class PaySerializer(serializers.Serializer):
    card = CardSerializer(required=False)
    paymentMethodId = serializers.UUIDField(required=False)

    def validate(self, attrs):
        if ("card" in attrs) == ("paymentMethodId" in attrs):
            raise serializers.ValidationError("Provide either 'card' or 'paymentMethodId', not both.")
        return attrs

    def card_input(self):
        if "card" not in self.validated_data:
            return None
        return CardSerializer.to_card_input(self.validated_data["card"])


class RefundRequestSerializer(serializers.Serializer):
    amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0.01"), required=False,
        help_text="Omit for a full refund of what remains.",
    )
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)
    idempotencyKey = serializers.CharField(max_length=255, required=False)

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Refund amount must be positive.")
        places = currency_exponent(settings.PAYPAL_CURRENCY)
        if value != round(value, places):
            raise serializers.ValidationError(f"Use at most {places} decimal places.")
        return value


class SavedCardSerializer(serializers.ModelSerializer):
    paymentMethodId = serializers.UUIDField(source="id", read_only=True)
    lastDigits = serializers.CharField(source="last_digits", read_only=True)
    createdAt = serializers.DateTimeField(source="created", read_only=True)

    class Meta:
        model = SavedCard
        fields = ["paymentMethodId", "brand", "lastDigits", "expiry", "createdAt"]


class RefundSerializer(serializers.ModelSerializer):
    refundId = serializers.UUIDField(source="id", read_only=True)
    paypalRefundId = serializers.CharField(source="paypal_refund_id", read_only=True)
    paypalStatus = serializers.CharField(source="paypal_status", read_only=True)
    createdAt = serializers.DateTimeField(source="created", read_only=True)

    class Meta:
        model = PayPalRefund
        fields = ["refundId", "status", "amount", "currency", "reason", "paypalRefundId", "paypalStatus", "createdAt"]


class PaymentSerializer(serializers.ModelSerializer):
    paymentMethodId = serializers.UUIDField(source="saved_card_id", read_only=True, allow_null=True)
    card = serializers.SerializerMethodField()
    paypalOrderId = serializers.CharField(source="paypal_order_id")
    authorization = serializers.SerializerMethodField()
    capture = serializers.SerializerMethodField()
    refundedAmount = serializers.DecimalField(source="refunded_amount", max_digits=10, decimal_places=2)
    refundableAmount = serializers.DecimalField(source="refundable_amount", max_digits=10, decimal_places=2)
    refunds = RefundSerializer(many=True, read_only=True)
    lastError = serializers.CharField(source="last_error")

    class Meta:
        model = PayPalPayment
        fields = [
            "status", "amount", "currency", "paymentMethodId", "card", "paypalOrderId", "authorization",
            "capture", "refundedAmount", "refundableAmount", "refunds", "lastError",
        ]

    def get_card(self, obj):
        return {"brand": obj.card_brand or None, "lastDigits": obj.card_last_digits or None}

    def get_authorization(self, obj):
        if not obj.authorization_id:
            return None
        return {
            "id": obj.authorization_id,
            "status": obj.authorization_status,
            "authorizedAt": obj.authorized_at,
            "expiresAt": obj.authorization_expires_at,
            "reauthorizationCount": obj.reauthorization_count,
        }

    def get_capture(self, obj):
        if not obj.capture_id:
            return None
        return {
            "id": obj.capture_id,
            "status": obj.capture_status,
            "amount": _money(obj.captured_amount),
            "paypalFee": _money(obj.paypal_fee),
            "netAmount": _money(obj.net_amount),
            "capturedAt": obj.captured_at,
        }


def _money(value):
    return None if value is None else str(value)
