"""
PayPal integration tests. The SDK client is real; only its transport is stubbed, so the request-building
pipeline (paths, headers, bodies) is exercised without touching the network.
"""

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx2
from django.test import TestCase
from django.utils import timezone
from pay_pal_server_sdk import PayPalServerSdkClient
from pay_pal_server_sdk.core import ClientCredentials, JsonBody
from rest_framework.test import APIClient

from catalogue.models import Category, Product, ProductVariant
from customers.models import Customer
from orders.models import Order

from .models import PayPalPayment, PayPalRefund, SavedCard
from .paypal import client as paypal_client
from .paypal import gateway

CARD = {"number": "4111 1111 1111 1111", "expiry": "2030-12", "securityCode": "123", "name": "Test Shopper",
        "billingAddress": {"addressLine1": "1 Main St", "city": "San Jose", "state": "CA",
                           "postalCode": "95131", "countryCode": "US"}}


@dataclass
class StubResponse:
    status_code: int = 200
    headers: dict = field(default_factory=dict)
    chunks: list = field(default_factory=list)
    url: str = "https://api.test/"
    closed: bool = False

    def iter_bytes(self, chunk_size):
        yield from self.chunks
        self.close()

    def read(self):
        self.close()
        return b"".join(self.chunks)

    def close(self):
        self.closed = True


def json_response(status, body):
    return StubResponse(status_code=status, headers={"content-type": "application/json"},
                        chunks=[json.dumps(body).encode()])


class RoutingTransport:
    """Answers each request from the first route whose (method, path regex) matches; records every request."""

    def __init__(self):
        self.routes = []
        self.requests = []

    def on(self, method, pattern, responder):
        self.routes.insert(0, (method, re.compile(pattern), responder))

    def send(self, request):
        self.requests.append(request)
        path = request.url.split("?")[0]
        for method, pattern, responder in self.routes:
            if method == request.method and pattern.search(path):
                result = responder(request)
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"unexpected request {request.method} {path}")

    def close(self):
        pass

    def calls(self, method, pattern):
        rx = re.compile(pattern)
        return [r for r in self.requests if r.method == method and rx.search(r.url.split("?")[0])]


def body_of(request):
    assert isinstance(request.body, JsonBody)
    return json.loads(request.body.content)


def money(value, currency="USD"):
    return {"currency_code": currency, "value": value}


def auth_body(auth_id, value, status="CREATED", created=None):
    created = created or timezone.now()
    return {"id": auth_id, "status": status, "amount": money(value),
            "create_time": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expiration_time": (created + timedelta(days=29)).strftime("%Y-%m-%dT%H:%M:%SZ")}


class PayPalStubMixin:
    def setUp(self):
        super().setUp()
        self.transport = RoutingTransport()
        self.transport.on("POST", r"/v1/oauth2/token$", lambda r: json_response(
            200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600}))
        self.client_sdk = PayPalServerSdkClient(
            custom_http_client=self.transport, retry_options=0,
            oauth2=ClientCredentials(client_id="id", client_secret="secret"),
            base_url="https://paypal.test",
        )
        paypal_client.set_client(self.client_sdk)
        self.addCleanup(paypal_client.set_client, None)
        self.auth_seq = 0

    def stub_create_order(self, status="COMPLETED", auth_status="CREATED", payer_action=False):
        def respond(request):
            body = body_of(request)
            value = body["purchase_units"][0]["amount"]["value"]
            self.auth_seq += 1
            payload = {"id": f"ORDER{self.auth_seq}", "status": status, "links": [],
                       "payment_source": {"card": {"brand": "VISA", "last_digits": "1111"}},
                       "purchase_units": [{"payments": {"authorizations": [
                           auth_body(f"AUTH{self.auth_seq}", value, auth_status)]}}]}
            if payer_action:
                payload = {"id": "ORDERX", "status": "PAYER_ACTION_REQUIRED",
                           "links": [{"href": "https://paypal.test/approve", "rel": "payer-action"}]}
            return json_response(201, payload)
        self.transport.on("POST", r"/v2/checkout/orders$", respond)

    def stub_get_authorization(self, status="CREATED"):
        self.transport.on("GET", r"/v2/payments/authorizations/[^/]+$",
                          lambda r: json_response(200, auth_body(r.url.split("?")[0].rsplit("/", 1)[1], "0", status)))

    def stub_capture(self):
        def respond(request):
            value = body_of(request)["amount"]["value"]
            return json_response(201, {
                "id": "CAP1", "status": "COMPLETED", "amount": money(value), "final_capture": True,
                "seller_receivable_breakdown": {"gross_amount": money(value), "paypal_fee": money("0.81"),
                                                "net_amount": money(str(Decimal(value) - Decimal("0.81")))},
            })
        self.transport.on("POST", r"/v2/payments/authorizations/[^/]+/capture$", respond)

    def stub_refund(self):
        counter = {"n": 0}

        def respond(request):
            counter["n"] += 1
            return json_response(201, {"id": f"REF{counter['n']}", "status": "COMPLETED",
                                       "amount": body_of(request)["amount"]})
        self.transport.on("POST", r"/v2/payments/captures/[^/]+/refund$", respond)


class GatewayTests(PayPalStubMixin, TestCase):
    def test_authorize_sends_amount_and_card_and_parses_hold(self):
        self.stub_create_order()
        card = gateway.CardInput(number="4111111111111111", expiry="2030-12", security_code="123", name="A B")
        result = gateway.authorize(request_id="pay-1", amount="12.34", currency="USD", custom_id="o1",
                                   description="Order o1", card=card)
        self.assertEqual(result.authorization_id, "AUTH1")
        self.assertEqual(result.amount, "12.34")
        req = self.transport.calls("POST", r"/v2/checkout/orders$")[0]
        self.assertTrue(req.url.startswith("https://paypal.test/"))
        self.assertEqual(req.headers["paypal-request-id"], "pay-1")
        self.assertEqual(req.headers["prefer"], "return=representation")
        body = body_of(req)
        self.assertEqual(body["intent"], "AUTHORIZE")
        self.assertEqual(body["purchase_units"][0]["amount"], money("12.34"))
        self.assertNotIn("4111111111111111", repr(card))

    def test_base_url_override_moves_token_request(self):
        self.stub_get_authorization()
        gateway.get_authorization("A1")
        self.assertTrue(all(r.url.startswith("https://paypal.test/") for r in self.transport.requests))
        self.assertTrue(self.transport.calls("POST", r"/v1/oauth2/token$"))

    def test_payer_action_is_detected(self):
        self.stub_create_order(payer_action=True)
        result = gateway.authorize(request_id="p", amount="1.00", currency="USD", custom_id="o", description="d",
                                   vault_id="V1")
        self.assertTrue(result.payer_action_required)

    def test_typed_error_maps_to_caller_status_with_issue(self):
        self.transport.on("POST", r"/capture$", lambda r: json_response(422, {
            "name": "UNPROCESSABLE_ENTITY", "message": "m", "debug_id": "dbg",
            "details": [{"issue": "AUTHORIZATION_EXPIRED"}]}))
        with self.assertRaises(gateway.PayPalError) as e:
            gateway.capture("A1", amount="1.00", currency="USD", request_id="c")
        self.assertEqual((e.exception.status_code, e.exception.provider_status, e.exception.issue),
                         (422, 422, "AUTHORIZATION_EXPIRED"))
        self.assertFalse(e.exception.outcome_unknown)

    def test_connect_error_is_known_and_read_timeout_is_unknown(self):
        self.transport.on("GET", r"/authorizations/", lambda r: httpx2.ConnectError("refused"))
        with self.assertRaises(gateway.PayPalError) as e:
            gateway.get_authorization("A1")
        self.assertEqual((e.exception.status_code, e.exception.outcome_unknown), (502, False))
        self.transport.on("GET", r"/authorizations/", lambda r: httpx2.ReadTimeout("slow"))
        with self.assertRaises(gateway.PayPalError) as e:
            gateway.get_authorization("A1")
        self.assertEqual((e.exception.status_code, e.exception.outcome_unknown), (504, True))

    def test_bad_credentials_is_a_configuration_error(self):
        self.transport.on("POST", r"/v1/oauth2/token$", lambda r: json_response(401, {"error": "invalid_client"}))
        with self.assertRaises(gateway.PayPalError) as e:
            gateway.get_authorization("A1")
        self.assertEqual(e.exception.status_code, 502)

    def test_truncated_success_is_outcome_unknown(self):
        self.transport.on("POST", r"/capture$", lambda r: json_response(201, {}))
        with self.assertRaises(gateway.PayPalError) as e:
            gateway.capture("A1", amount="1.00", currency="USD", request_id="c")
        self.assertTrue(e.exception.outcome_unknown)

    def test_search_reads_records(self):
        self.transport.on("GET", r"/v1/reporting/transactions$", lambda r: json_response(200, {
            "transaction_details": [{"transaction_info": {"transaction_id": "T1", "transaction_amount": money("5.00")}}],
            "page": 1, "total_pages": 1, "total_items": 1}))
        page = gateway.search_transactions(start_date="2026-10-01T00:00:00Z", end_date="2026-10-02T00:00:00Z", page=1)
        self.assertEqual([r.transaction_id for r in page.records], ["T1"])


class FlowTests(PayPalStubMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.shopper = Customer.objects.create_user(email="s@example.com", password="pw", first_name="S", last_name="One")
        self.other = Customer.objects.create_user(email="o@example.com", password="pw", first_name="O", last_name="Two")
        self.operator = Customer.objects.create_user(email="op@example.com", password="pw", first_name="Op",
                                                     last_name="Erator", is_staff=True)
        category = Category.objects.create(name="Things")
        product = Product.objects.create(name="Widget", category=category, description="d", base_price=Decimal("10.00"),
                                         shipping_fee=Decimal("2.34"), total_stock_level=0)
        self.variant = ProductVariant.objects.create(product=product, sku="W-1", is_default=True, stock_level=50)
        self.api = APIClient()

    def as_user(self, user):
        self.api.force_authenticate(user)
        return self.api

    def place(self, quantity=1, user=None):
        r = self.as_user(user or self.shopper).post("/api/v1/orders/", {"items": [
            {"variantId": self.variant.pk, "quantity": quantity}]}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return r.data["orderId"]

    def pay(self, order_id, user=None, **body):
        return self.as_user(user or self.shopper).post(f"/api/v1/orders/{order_id}/pay/", body or {"card": CARD},
                                                       format="json")

    def test_place_pay_fulfil_refund(self):
        self.stub_create_order()
        self.stub_get_authorization()
        self.stub_capture()
        self.stub_refund()
        order_id = self.place(quantity=2)
        order = Order.objects.get(pk=order_id)
        self.assertEqual(order.status, "awaiting_payment")
        self.assertEqual(order.total_amount, Decimal("22.34"))

        r = self.pay(order_id)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["status"], "authorized")
        self.assertEqual(r.data["payment"]["authorization"]["id"], "AUTH1")
        sent = body_of(self.transport.calls("POST", r"/v2/checkout/orders$")[0])
        self.assertEqual(sent["purchase_units"][0]["amount"]["value"], "22.34")

        # double-click: no second authorization
        r = self.pay(order_id)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.transport.calls("POST", r"/v2/checkout/orders$")), 1)

        # shoppers cannot fulfil
        self.assertEqual(self.as_user(self.shopper).post(f"/api/v1/orders/{order_id}/fulfil/").status_code, 403)
        r = self.as_user(self.operator).post(f"/api/v1/orders/{order_id}/fulfil/")
        self.assertEqual(r.status_code, 200, r.data)
        cap = r.data["payment"]["capture"]
        self.assertEqual((cap["amount"], cap["paypalFee"], cap["netAmount"]), ("22.34", "0.81", "21.53"))
        self.assertEqual(r.data["status"], "fulfilled")
        self.as_user(self.operator).post(f"/api/v1/orders/{order_id}/fulfil/")
        self.assertEqual(len(self.transport.calls("POST", r"/capture$")), 1)

        # another shopper cannot refund this order
        r = self.as_user(self.other).post(f"/api/v1/orders/{order_id}/refunds/", {"amount": "1.00"},
                                          format="json", HTTP_IDEMPOTENCY_KEY="k0")
        self.assertEqual(r.status_code, 404)

        # partial refund, then the same key again: one PayPal refund
        r = self.as_user(self.shopper).post(f"/api/v1/orders/{order_id}/refunds/", {"amount": "5.00"},
                                            format="json", HTTP_IDEMPOTENCY_KEY="k1")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertIn("refundId", r.data)
        first_refund = r.data["refundId"]
        r = self.as_user(self.shopper).post(f"/api/v1/orders/{order_id}/refunds/", {"amount": "5.00"},
                                            format="json", HTTP_IDEMPOTENCY_KEY="k1")
        self.assertEqual((r.status_code, r.data["refundId"]), (200, first_refund))
        self.assertEqual(len(self.transport.calls("POST", r"/refund$")), 1)

        # a distinct partial refund is legitimate; going past the captured amount is not
        r = self.as_user(self.shopper).post(f"/api/v1/orders/{order_id}/refunds/", {"amount": "10.00"},
                                            format="json", HTTP_IDEMPOTENCY_KEY="k2")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["orderStatus"], "partially_refunded")
        r = self.as_user(self.shopper).post(f"/api/v1/orders/{order_id}/refunds/", {"amount": "7.35"},
                                            format="json", HTTP_IDEMPOTENCY_KEY="k3")
        self.assertEqual((r.status_code, r.data["code"]), (422, "exceeds_refundable"))
        r = self.as_user(self.shopper).post(f"/api/v1/orders/{order_id}/refunds/", {},
                                            format="json", HTTP_IDEMPOTENCY_KEY="k4")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["amount"], "7.34")
        self.assertEqual(r.data["orderStatus"], "refunded")
        self.assertEqual(len(self.transport.calls("POST", r"/refund$")), 3)

        r = self.as_user(self.shopper).get("/api/v1/my-orders/")
        self.assertEqual(r.data["results"][0]["payment"]["refundedAmount"], "22.34")

    def test_cancel_voids_the_hold(self):
        self.stub_create_order()
        self.transport.on("POST", r"/void$", lambda r: json_response(200, auth_body("AUTH1", "12.34", "VOIDED")))
        order_id = self.place()
        self.assertEqual(self.pay(order_id).status_code, 201)
        r = self.as_user(self.operator).post(f"/api/v1/orders/{order_id}/cancel/")
        self.assertEqual((r.status_code, r.data["status"]), (200, "cancelled"))
        self.assertEqual(r.data["payment"]["status"], "voided")
        self.assertEqual(len(self.transport.calls("POST", r"/void$")), 1)
        r = self.as_user(self.operator).post(f"/api/v1/orders/{order_id}/fulfil/")
        self.assertEqual(r.status_code, 409)

    def test_stale_hold_is_renewed_before_capture(self):
        self.stub_create_order()
        self.stub_get_authorization()
        self.stub_capture()
        self.transport.on("POST", r"/reauthorize$", lambda r: json_response(201, auth_body("AUTH-RE", "12.34")))
        order_id = self.place()
        self.pay(order_id)
        PayPalPayment.objects.filter(order_id=order_id).update(
            honor_period_started_at=timezone.now() - timedelta(days=4),
            authorized_at=timezone.now() - timedelta(days=4))
        r = self.as_user(self.operator).post(f"/api/v1/orders/{order_id}/fulfil/")
        self.assertEqual(r.status_code, 200, r.data)
        capture = self.transport.calls("POST", r"/capture$")[0]
        self.assertIn("/AUTH-RE/capture", capture.url)
        self.assertEqual(r.data["payment"]["authorization"]["reauthorizationCount"], 1)

    def test_hold_that_cannot_be_renewed_is_reported_actionably(self):
        self.stub_create_order()
        self.stub_get_authorization()
        self.transport.on("POST", r"/reauthorize$", lambda r: json_response(422, {
            "name": "UNPROCESSABLE_ENTITY", "message": "m", "debug_id": "d",
            "details": [{"issue": "REAUTHORIZATION_NOT_ALLOWED"}]}))
        order_id = self.place()
        self.pay(order_id)
        PayPalPayment.objects.filter(order_id=order_id).update(
            honor_period_started_at=timezone.now() - timedelta(days=5),
            authorized_at=timezone.now() - timedelta(days=5))
        r = self.as_user(self.operator).post(f"/api/v1/orders/{order_id}/fulfil/")
        self.assertEqual((r.status_code, r.data["code"]), (409, "authorization_not_renewable"))
        self.assertIn("cancel the order", r.data["error"])

        PayPalPayment.objects.filter(order_id=order_id).update(
            authorization_expires_at=timezone.now() - timedelta(minutes=1))
        r = self.as_user(self.operator).post(f"/api/v1/orders/{order_id}/fulfil/")
        self.assertEqual((r.status_code, r.data["code"]), (409, "authorization_not_renewable"))

    def test_declined_card_releases_the_claim(self):
        self.stub_create_order(auth_status="DENIED")
        order_id = self.place()
        r = self.pay(order_id)
        self.assertEqual((r.status_code, r.data["code"]), (402, "payment_declined"))
        self.assertFalse(PayPalPayment.objects.filter(order_id=order_id).exists())
        self.assertEqual(Order.objects.get(pk=order_id).status, "awaiting_payment")

    def test_payer_action_required_is_refused(self):
        self.stub_create_order(payer_action=True)
        order_id = self.place()
        r = self.pay(order_id)
        self.assertEqual((r.status_code, r.data["code"]), (402, "payer_action_required"))

    def test_saved_card_lifecycle_and_ownership(self):
        self.transport.on("POST", r"/v3/vault/payment-tokens$", lambda r: json_response(201, {
            "id": "VAULT1", "customer": {"id": "CUST1"},
            "payment_source": {"card": {"brand": "VISA", "last_digits": "1111", "expiry": "2030-12"}}}))
        self.transport.on("DELETE", r"/v3/vault/payment-tokens/VAULT1$", lambda r: StubResponse(status_code=204))
        self.stub_create_order()

        r = self.as_user(self.shopper).post("/api/v1/payment-methods/", CARD, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        pm = r.data["paymentMethodId"]
        self.assertEqual((r.data["brand"], r.data["lastDigits"]), ("VISA", "1111"))
        self.assertNotIn("number", r.data)
        saved = SavedCard.objects.get(pk=pm)
        self.assertNotIn("4111111111111111", json.dumps({f.name: str(getattr(saved, f.name))
                                                          for f in SavedCard._meta.fields}))

        self.assertEqual(self.as_user(self.other).get("/api/v1/payment-methods/").data, [])
        other_order = self.place(user=self.other)
        r = self.pay(other_order, user=self.other, paymentMethodId=pm)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.as_user(self.other).delete(f"/api/v1/payment-methods/{pm}/").status_code, 404)

        order_id = self.place()
        r = self.pay(order_id, paymentMethodId=pm)
        self.assertEqual(r.status_code, 201, r.data)
        sent = body_of(self.transport.calls("POST", r"/v2/checkout/orders$")[-1])
        self.assertEqual(sent["payment_source"]["card"], {"vault_id": "VAULT1"})

        self.assertEqual(self.as_user(self.shopper).delete(f"/api/v1/payment-methods/{pm}/").status_code, 204)
        self.assertEqual(self.as_user(self.shopper).get("/api/v1/payment-methods/").data, [])
        r = self.pay(self.place(), paymentMethodId=pm)
        self.assertEqual(r.status_code, 404)

    def test_reconciliation_walks_every_page_and_lines_up(self):
        self.stub_create_order()
        order_id = self.place()
        self.pay(order_id)
        PayPalPayment.objects.filter(order_id=order_id).update(authorized_at=timezone.now() - timedelta(days=40))

        def respond(request):
            page = int(re.search(r"[?&]page=(\d+)", request.url)[1])
            records = {1: [{"transaction_info": {"transaction_id": "AUTH1", "custom_field": order_id}}],
                       2: [{"transaction_info": {"transaction_id": "STRANGER"}}]}.get(page, [])
            return json_response(200, {"transaction_details": records, "page": page, "total_pages": 2})
        self.transport.on("GET", r"/v1/reporting/transactions$", respond)

        start = (timezone.now() - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(self.as_user(self.shopper).get("/api/v1/reconciliation/", {"from": start,
                         "to": timezone.now().isoformat()}).status_code, 403)
        r = self.as_user(self.operator).get("/api/v1/reconciliation/", {"from": start, "to": timezone.now().isoformat()})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(len(self.transport.calls("GET", r"/reporting/transactions$")), 4)  # 2 windows x 2 pages
        self.assertEqual([m["transactionId"] for m in r.data["matched"]], ["AUTH1"])
        self.assertEqual([m["transactionId"] for m in r.data["unknownToApp"]], ["STRANGER"])

    def test_unexpected_error_never_leaks_details(self):
        self.transport.on("POST", r"/v2/checkout/orders$", lambda r: RuntimeError("secret detail"))
        order_id = self.place()
        r = self.pay(order_id)
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.json(), {"error": "An internal error occurred."})
        # the claim is resolvable on retry rather than stuck "pending"
        self.assertEqual(PayPalPayment.objects.get(order_id=order_id).status, "unknown")
        self.stub_create_order()
        r = self.pay(order_id)
        self.assertEqual(r.status_code, 201, r.data)
        requests = self.transport.calls("POST", r"/v2/checkout/orders$")
        self.assertEqual(requests[0].headers["paypal-request-id"], requests[1].headers["paypal-request-id"])


class StartupConfigTests(TestCase):
    def _check(self, **overrides):
        env = dict(os.environ, **overrides)
        return subprocess.run([sys.executable, "manage.py", "check"], cwd=Path(__file__).resolve().parent.parent,
                              env=env, capture_output=True, text=True, timeout=120)

    def test_blank_or_missing_secret_refuses_to_start(self):
        for value in ("", "   "):
            result = self._check(PAYPAL_CLIENT_SECRET=value)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("PAYPAL_CLIENT_SECRET", result.stderr)
        env_without = {k: v for k, v in os.environ.items() if k != "PAYPAL_CLIENT_SECRET"}
        result = subprocess.run([sys.executable, "manage.py", "check"], cwd=Path(__file__).resolve().parent.parent,
                                env=env_without, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(result.returncode, 0)
