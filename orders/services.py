from decimal import Decimal

from django.db import transaction
from rest_framework.exceptions import ValidationError

from catalogue.models import ProductVariant

from .models import Order, OrderItem


def _resolve_variant(line):
    variants = ProductVariant.active_objects.select_related("product").filter(product__is_active=True)
    if "variantId" in line:
        variant = variants.filter(pk=line["variantId"]).first()
        if variant is None:
            raise ValidationError({"items": f"Catalogue item variant {line['variantId']} does not exist."})
        return variant
    variants = variants.filter(product_id=line["productId"])
    variant = variants.filter(is_default=True).first() or variants.order_by("pk").first()
    if variant is None:
        raise ValidationError({"items": f"Catalogue item {line['productId']} does not exist."})
    return variant


def place_order(customer, lines):
    """
    Create an order awaiting payment from catalogue items, priced from the catalogue (never by the
    caller). Reuses the existing Order / OrderItem models.
    """
    quantities = {}
    variants = {}
    for line in lines:
        variant = _resolve_variant(line)
        variants[variant.pk] = variant
        quantities[variant.pk] = quantities.get(variant.pk, 0) + line["quantity"]

    for variant_id, quantity in quantities.items():
        variant = variants[variant_id]
        if not variant.product.is_available or variant.stock_level < quantity:
            raise ValidationError({"items": f"'{variant.product.name}' does not have {quantity} in stock."})

    with transaction.atomic():
        order = Order(customer=customer, billing_address=customer.address)
        order.save()
        items = []
        for variant_id, quantity in quantities.items():
            variant = variants[variant_id]
            unit_price = variant.actual_price
            items.append(OrderItem(
                order=order,
                variant=variant,
                product=variant.product,
                unit_price=unit_price,
                quantity=quantity,
                total_price=unit_price * quantity,
                shipping=variant.product.shipping_fee or Decimal("0"),
            ))
        OrderItem.objects.bulk_create(items)
        order.save()  # recompute total_amount from the items
    return order
