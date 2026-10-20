import braintree
from django.conf import settings
from django.template.response import TemplateResponse
from drf_yasg.utils import swagger_auto_schema
from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from orders.models import Order
from orders.serializers import OrderSerializer
from .models import Transaction
from .tasks import send_order_confirmation_email, write_trxn_to_csv, update_stock


# Create your views here.
gateway = braintree.BraintreeGateway(settings.BRAINTREE_CONF)


class Payment(APIView):
    permission_classes = [IsAuthenticated]

    def get_object(self, pk):
        order = Order.objects.select_related("customer", "address")
        return get_object_or_404(order, customer=self.request.user, pk=pk)

    @swagger_auto_schema(tags=["Payment"])
    def get(self, request, pk):
        order = self.get_object(pk)
        serializer = OrderSerializer(order, context={"request": request})
        client_token = gateway.client_token.generate()
        context = {"client_token": client_token, "order": serializer.data}
        return TemplateResponse(request, "payment.html", context)

    def _post_payment_tasks(self, order, transaction_record):
        send_order_confirmation_email.delay(order)
        write_trxn_to_csv.delay(order, order.customer, transaction_record.transaction_id)
        update_stock.delay(order, order.customer)

    def _prepare_customer_data(self, customer, address):
        """
        Prepares customer data for the payment gateway.
        """
        return {
            "first_name": customer.first_name,
            "last_name": customer.last_name,
            "street_address": address.street_address,
            "postal_code": str(address.postal_code),
            "locality": address.city,
            "region": address.state,
            "country_name": address.country,
        }

    @swagger_auto_schema(tags=["Payment"])
    def post(self, request, pk):
        order = self.get_object(pk)

        # Validate order state
        if order.status == "paid":
            return Response({"error": "Order already paid"}, status=status.HTTP_400_BAD_REQUEST)

        customer = order.customer
        address = order.address
        total_cost = order.total_cost()

        if not (nonce_from_client := request.data.get("payment_method_nonce")):
            return Response(
                {"error": "Payment method nonce is required"}, status=status.HTTP_400_BAD_REQUEST
            )

        trxn_record = Transaction.objects.create(
            order=order,
            customer=customer,
            status="pending",
            amount_paid=total_cost,
        )

        customer_kwargs = self._prepare_customer_data(customer, address)
        result = gateway.transaction.sale(
            {
                "amount": f"{total_cost:.2f}",
                "payment_method_nonce": nonce_from_client,
                "shipping": {**customer_kwargs},
                "options": {
                    "submit_for_settlement": True,
                    "store_in_vault_on_success": True,
                },
            }
        )
        trxn_record.transaction_id = result.transaction.id
        if result.is_success:  # If payment is successful
            order.status = "paid"
            order.save(update_fields=["status"])
            trxn_record.status = "successful"
            trxn_record.save(update_fields=["status", "transaction_id"])
            self._post_payment_tasks(order, trxn_record)
            return Response({"success": "Payment was successful"}, status=status.HTTP_200_OK)

        trxn_record.status = "failed"
        trxn_record.save(update_fields=["status", "transaction_id"])
        return Response({"error": f"{result.message}"}, status=status.HTTP_502_BAD_GATEWAY)


# ------------------------------------------------------------------------------------------------
# PayPal: saved cards and reconciliation
# ------------------------------------------------------------------------------------------------
from uuid import uuid4  # noqa: E402

from django.utils import timezone as dj_timezone  # noqa: E402
from django.utils.dateparse import parse_datetime  # noqa: E402
from rest_framework.generics import GenericAPIView  # noqa: E402
from rest_framework.permissions import IsAdminUser  # noqa: E402

from . import services as paypal_services  # noqa: E402
from .exceptions import PaymentsAPIMixin  # noqa: E402
from .models import SavedCard  # noqa: E402
from .serializers import CardSerializer, SavedCardSerializer  # noqa: E402


class PaymentMethodListView(PaymentsAPIMixin, GenericAPIView):
    """The caller's saved cards (stored in PayPal's vault)."""

    permission_classes = [IsAuthenticated]
    serializer_class = SavedCardSerializer
    pagination_class = None

    def get_queryset(self):
        return SavedCard.objects.filter(customer=self.request.user, status=SavedCard.Status.ACTIVE)

    @swagger_auto_schema(tags=["Payment methods"], responses={200: SavedCardSerializer(many=True)})
    def get(self, request):
        return Response(self.get_serializer(self.get_queryset(), many=True).data)

    @swagger_auto_schema(
        tags=["Payment methods"], request_body=CardSerializer, responses={201: SavedCardSerializer}
    )
    def post(self, request):
        """Save a card. An optional Idempotency-Key header makes a repeated request return the same card."""
        card = CardSerializer(data=request.data)
        card.is_valid(raise_exception=True)
        request_key = (request.headers.get("Idempotency-Key") or "").strip()[:255] or f"auto-{uuid4()}"
        saved, created = paypal_services.save_card(
            request.user, CardSerializer.to_card_input(card.validated_data), request_key=request_key
        )
        return Response(
            SavedCardSerializer(saved).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class PaymentMethodInstanceView(PaymentsAPIMixin, APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(tags=["Payment methods"], responses={204: "Deleted", 404: "Not found"})
    def delete(self, request, pk):
        paypal_services.delete_card(request.user, pk)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ReconciliationView(PaymentsAPIMixin, APIView):
    """Operator report: PayPal's transactions for a range, lined up against local orders."""

    permission_classes = [IsAdminUser]

    @swagger_auto_schema(tags=["Reconciliation"])
    def get(self, request):
        start = self._parse(request.query_params.get("from"), "from")
        end = self._parse(request.query_params.get("to"), "to")
        return Response(paypal_services.reconcile(start, end))

    @staticmethod
    def _parse(value, name):
        parsed = parse_datetime(value.strip().replace(" ", "+")) if value else None
        if parsed is None:
            raise paypal_services.PaymentFlowError(
                400, f"'{name}' must be an ISO-8601 date-time, e.g. 2026-10-01T00:00:00Z.", code="invalid_range"
            )
        if dj_timezone.is_naive(parsed):
            parsed = dj_timezone.make_aware(parsed, dj_timezone.get_fixed_timezone(0))
        return parsed
