from django.db import transaction
from django.db.models import Prefetch
from django.shortcuts import redirect
from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import reverse, status
from rest_framework.generics import GenericAPIView, get_object_or_404
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from cart.cart import Cart
from customers.serializers import AddressSerializer
from payments import services as paypal_services
from payments.exceptions import PaymentsAPIMixin
from payments.serializers import PaySerializer, RefundRequestSerializer, RefundSerializer

from . import services as order_services
from .models import Order, OrderItem
from .serializers import (
    OrderItemSerializer,
    OrderSerializer,
    PaymentOrderSerializer,
    PlaceOrderSerializer,
)


# Create your views here.
class OrderListView(GenericAPIView, LimitOffsetPagination):
    permission_classes = [IsAuthenticated]
    serializer_class = OrderSerializer
    filterset_fields = ["status"]

    def get_queryset(self):
        queryset = Order.objects.filter(customer=self.request.user)
        queryset = self.filter_queryset(queryset)
        return queryset

    @swagger_auto_schema(
        operation_summary="Get all orders by a customer",
        manual_parameters=[
            openapi.Parameter("status", in_=openapi.IN_QUERY, type=openapi.TYPE_STRING)
        ],
        responses={200: OrderSerializer(many=True)},
        tags=["Order"],
    )
    def get(self, request):
        orders = self.get_queryset()
        page = self.paginate_queryset(orders)

        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(orders, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_summary="Create an order from user's cart items",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=["action"],
            properties={
                "action": openapi.Schema(type=openapi.TYPE_STRING),
                "address": openapi.Schema(type=openapi.TYPE_OBJECT),
            },
            example={
                "action": "checkout",
                "billing_address": {
                    "street_address": "123 Main St",
                    "postal_code": "12345",
                    "city": "Anytown",
                    "state": "CA",
                    "country": "Canada",
                },
            },
        ),
        responses={201: OrderSerializer, 400: "Bad request"},
        tags=["Order"],
    )
    def post(self, request):
        """
        Handle POST request to create an order from the user's cart.
        """
        action = request.data.get("action")
        billing_address = request.data.get("billing_address")
        cart = Cart(
            request, force_refresh=True
        )  # Force refresh to get the latest cart items and prices
        customer = cart.customer

        # Validate the action type
        if action not in ("checkout", "save_order"):
            return Response({"error": "Invalid action"}, status=status.HTTP_400_BAD_REQUEST)

        # Ensure the user's cart is not empty
        if len(cart.cart_items) <= 0:
            return Response(
                {"error": "Can't create an order. Your cart is empty"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            order = Order(customer=customer)

            #  A new billing address is provided or use customer's address
            if not (billing_address or customer.address):
                return Response(
                    {"billing_address": "Billing address was not provided"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if billing_address:
                serializer = AddressSerializer(data=billing_address)
                serializer.is_valid(raise_exception=True)
                order.billing_address = serializer.save()
            else:
                order.billing_address = customer.address

            order.save()
            OrderItem.create_from_cart(order, cart)
            cart.clear()

            match action:
                case "checkout":
                    return redirect(reverse.reverse("payment", args=[order.id]))

                case "save_order":
                    return Response(
                        {"success": "Your Order has been saved"},
                        status=status.HTTP_201_CREATED,
                    )

                case _:
                    return Response(
                        {"error": "Invalid action"}, status=status.HTTP_400_BAD_REQUEST
                    )


class OrderInstanceView(GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = OrderSerializer

    def get_queryset(self):
        return Order.objects.filter(customer=self.request.user)

    @swagger_auto_schema(
        operation_summary="Get an order",
        responses={200: OrderItemSerializer(), 404: "Not Found"},
        tags=["Order"],
    )
    def get(self, request, pk):
        order = self.get_object()
        serializer = self.get_serializer(order)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_summary="Update an order with a new billing address",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={"address": AddressSerializer},
            example={
                "address": {
                    "street_address": "123 Main St",
                    "postal_code": "12345",
                    "city": "Anytown",
                    "state": "CA",
                    "country": "Canada",
                },
            },
        ),
        responses={201: OrderSerializer, 400: "Bad Request"},
        tags=["Order"],
    )
    def put(self, request, pk):
        order = self.get_object()

        # Only pending orders can be modified
        if order.status != Order.OrderStatus.AWAITING_PAYMENT:
            return Response(status=status.HTTP_403_FORBIDDEN)

        address = request.data.get("address")
        data = {"address": address}
        serializer = OrderSerializer(instance=order, data=data)
        serializer.context["request"] = request
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_200_OK)

    @swagger_auto_schema(operation_summary="Delete an order", tags=["Order"])
    def delete(self, request, pk):
        order = self.get_object()
        order.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class OrderItemView(GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = OrderItemSerializer

    def get_queryset(self):
        customer_orders = Order.objects.filter(customer=self.request.user)
        queryset = OrderItem.objects.filter(order__in=customer_orders)
        return queryset

    def get_order_item(self, order_id, order_item_id):
        qs = self.get_queryset()
        return get_object_or_404(qs, id=order_item_id, order_id=order_id)

    def check_order_editable(self, customer, order_id):
        """
        Checks if an order's item can be modified based on its status.
        Only orders with a status of "awaiting_payment" can be edited.

        Args:
            customer (Customer): The customer to whom the order belongs to.
            order_id (UUID): The ID of the order to be modified
        """
        order = get_object_or_404(Order.objects.filter(customer=customer), id=order_id)
        if order.status != Order.OrderStatus.AWAITING_PAYMENT:
            return Response(status=status.HTTP_403_FORBIDDEN)
        return (
            True
            if order.status == Order.OrderStatus.AWAITING_PAYMENT
            else Response(status=status.HTTP_403_FORBIDDEN)
        )

    def dispatch(self, request, *args, **kwargs):
        order = self.get_object()
        if request.method.lower() != "get":
            return self.check_order_editable(request.user, order.id)
        return super().dispatch(request, *args, **kwargs)

    @swagger_auto_schema(
        operation_summary="Get an item from an order",
        responses={200: OrderItemSerializer(), 404: "Not Found"},
        tags=["Order"],
    )
    def get(self, request, order_id, item_id):
        order_item = self.get_order_item(order_id, item_id)
        serializer = self.get_serializer(order_item)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_summary="Delete an item from a user's order",
        tags=["Order"],
    )
    def delete(self, request, order_id, item_id):
        order_item = self.get_order_item(order_id, item_id)
        order_item.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# PayPal checkout: place, pay (authorize), fulfil (capture), cancel (void), refund
def _payment_orders():
    return Order.objects.select_related("paypal_payment").prefetch_related(
        Prefetch("items", queryset=OrderItem.objects.select_related("product", "variant")),
        "paypal_payment__refunds",
    )


def _order_response(order, http_status=status.HTTP_200_OK):
    order = _payment_orders().get(pk=order.pk)
    return Response(PaymentOrderSerializer(order).data, status=http_status)


class PlaceOrderView(PaymentsAPIMixin, APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary="Place an order from catalogue items",
        request_body=PlaceOrderSerializer,
        responses={201: PaymentOrderSerializer},
        tags=["Checkout"],
    )
    def post(self, request):
        serializer = PlaceOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = order_services.place_order(request.user, serializer.validated_data["items"])
        return _order_response(order, status.HTTP_201_CREATED)


class MyOrdersView(PaymentsAPIMixin, GenericAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = PaymentOrderSerializer
    filterset_fields = ["status"]

    def get_queryset(self):
        return _payment_orders().filter(customer=self.request.user)

    @swagger_auto_schema(operation_summary="The caller's orders with their payment state", tags=["Checkout"])
    def get(self, request):
        orders = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(orders)
        if page is not None:
            return self.get_paginated_response(self.get_serializer(page, many=True).data)
        return Response(self.get_serializer(orders, many=True).data)


class OrderActionView(PaymentsAPIMixin, APIView):
    """Base for order actions: shoppers reach only their own orders; operators reach any order."""

    def get_order(self, pk):
        queryset = Order.objects.all()
        if not self.request.user.is_staff:
            queryset = queryset.filter(customer=self.request.user)
        return get_object_or_404(queryset, pk=pk)


class PayOrderView(OrderActionView):
    permission_classes = [IsAuthenticated]

    def get_order(self, pk):
        return get_object_or_404(Order.objects.filter(customer=self.request.user), pk=pk)

    @swagger_auto_schema(
        operation_summary="Authorize (hold) the order total by card or saved card",
        request_body=PaySerializer,
        responses={201: PaymentOrderSerializer},
        tags=["Checkout"],
    )
    def post(self, request, pk):
        order = self.get_order(pk)
        serializer = PaySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        _, created = paypal_services.pay_order(
            order,
            request.user,
            card=serializer.card_input(),
            saved_card_id=serializer.validated_data.get("paymentMethodId"),
        )
        return _order_response(order, status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class FulfilOrderView(OrderActionView):
    permission_classes = [IsAdminUser]

    @swagger_auto_schema(
        operation_summary="Fulfil the order and capture the held funds (operator)", tags=["Checkout"]
    )
    def post(self, request, pk):
        order = self.get_order(pk)
        paypal_services.fulfil_order(order, request.user)
        return _order_response(order)


class CancelOrderView(OrderActionView):
    permission_classes = [IsAdminUser]

    @swagger_auto_schema(
        operation_summary="Cancel before fulfilment and release the held funds (operator)",
        tags=["Checkout"],
    )
    def post(self, request, pk):
        order = self.get_order(pk)
        paypal_services.cancel_order(order, request.user)
        return _order_response(order)


class OrderRefundsView(OrderActionView):
    """Refunds of the caller's own order (operators may refund any order)."""

    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(operation_summary="List the refunds of an order", tags=["Checkout"])
    def get(self, request, pk):
        order = self.get_order(pk)
        payment = getattr(order, "paypal_payment", None)
        refunds = payment.refunds.all() if payment is not None else []
        return Response(RefundSerializer(refunds, many=True).data)

    @swagger_auto_schema(
        operation_summary="Refund a fulfilled order, in full or in part",
        request_body=RefundRequestSerializer,
        responses={201: RefundSerializer},
        tags=["Checkout"],
    )
    def post(self, request, pk):
        order = self.get_order(pk)
        serializer = RefundRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        key = request.headers.get("Idempotency-Key") or serializer.validated_data.get("idempotencyKey") or ""
        key = key.strip()
        if not key:
            return Response(
                {
                    "error": "An Idempotency-Key header (or 'idempotencyKey' field) is required.",
                    "code": "idempotency_key_required",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        refund, created = paypal_services.refund_order(
            order,
            request.user,
            idempotency_key=key[:255],
            amount=serializer.validated_data.get("amount"),
            reason=serializer.validated_data.get("reason", ""),
        )
        order.refresh_from_db()
        body = RefundSerializer(refund).data
        body["orderId"] = str(order.pk)
        body["orderStatus"] = order.status
        return Response(body, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
