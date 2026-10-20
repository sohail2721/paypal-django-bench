from django.contrib import admin

from .models import PayPalPayment, PayPalRefund, PayPalSettlement, SavedCard


class PayPalRefundInline(admin.TabularInline):
    model = PayPalRefund
    extra = 0
    can_delete = False
    readonly_fields = [
        "amount", "currency", "status", "paypal_refund_id", "paypal_status", "idempotency_key", "created",
    ]
    exclude = ["requested_by", "reason"]


@admin.register(PayPalPayment)
class PayPalPaymentAdmin(admin.ModelAdmin):
    list_display = ["order", "customer", "status", "amount", "currency", "authorization_id", "capture_id", "created"]
    list_filter = ["status"]
    search_fields = ["order__id", "paypal_order_id", "authorization_id", "capture_id"]
    readonly_fields = [f.name for f in PayPalPayment._meta.fields]
    inlines = [PayPalRefundInline]


@admin.register(PayPalSettlement)
class PayPalSettlementAdmin(admin.ModelAdmin):
    list_display = ["payment", "action", "status", "created"]
    readonly_fields = ["payment", "action", "status", "requested_by"]


@admin.register(SavedCard)
class SavedCardAdmin(admin.ModelAdmin):
    list_display = ["id", "customer", "brand", "last_digits", "expiry", "status", "created"]
    list_filter = ["status", "brand"]
    readonly_fields = [f.name for f in SavedCard._meta.fields]
