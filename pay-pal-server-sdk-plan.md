# PayPal Server SDK integration plan — ecommerce-API (Django 5.0.6 / DRF 3.15.1, sync WSGI)

Request: `PHASE-BUILD.md` (whole file in scope). Skill: `python-integrate-pay-pal-server-sdk`.
SDK: `pay-pal-server-sdk` 2.29, import root `pay_pal_server_sdk`. Installed at
`.venv/Lib/site-packages/pay_pal_server_sdk` (dist-info `pay_pal_server_sdk-2.29.dist-info`); map read from
`C:\exp-staging\pr666-pp\sohail-paypal-sdk` (`pyproject.toml` version `2.29`, branch `main`). The installed
package and the source tree are byte-identical apart from line endings (`diff --strip-trailing-cr -rq` clean), so
map facts apply to the installed code.

## 1. Plan (build order)

1. **Settings** (`config/settings.py`): read `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_ENVIRONMENT`,
   `PAYPAL_CURRENCY`, optional `PAYPAL_BASE_URL`. Blank-or-missing `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` /
   `PAYPAL_CURRENCY` raises `ImproperlyConfigured` at import (`.strip() == ""` counts as missing). Leave existing
   bare `os.environ[...]` keys and Braintree settings untouched.
2. **PayPal gateway module** (new, e.g. `payments/paypal/client.py` + `payments/paypal/gateway.py`): the ONLY
   place that imports `pay_pal_server_sdk`. One process-wide sync `Client`, built lazily on first use (fork-safe
   under any WSGI server), closed via `atexit`. Gateway functions return plain app dataclasses/dicts and raise
   one app exception type (`PayPalGatewayError(kind, http_status, safe_message, issue, debug_id)`), never SDK
   types, never raw upstream bodies.
3. **Models** (new PayPal tables in `payments`; do not touch `payments.Transaction` / Braintree): `PayPalPayment`
   (OneToOne to `orders.Order` — the pay claim), `PayPalSettlement` (OneToOne to `PayPalPayment` — the
   capture-or-void claim), `PayPalRefund` (FK payment, `UniqueConstraint(payment, idempotency_key)`),
   `PayPalVaultCustomer` (OneToOne to `customers.Customer`, holds PayPal `customer.id`), `SavedCard`
   (FK customer, `vault_id` unique, display-only fields brand/last_digits/expiry, `deleted_at`),
   `SavedCardDeletion` (OneToOne to `SavedCard`). Add `Order.OrderStatus` members as needed (e.g.
   `authorized`, `refunded`, `partially_refunded`, `payment_failed`); `max_length=16` already — widen if a new
   value exceeds 16 characters. Migrations.
4. **Orders endpoints** (`orders/urls.py`, views, serializers): `POST /api/v1/orders/` (from variant ids +
   quantities, `OrderItem` rows priced from `ProductVariant.actual_price`), `POST orders/{id}/pay/`,
   `POST orders/{id}/fulfil/` (staff), `POST orders/{id}/cancel/` (staff), `POST orders/{id}/refunds/`,
   `GET my-orders/`, `GET reconciliation/` (staff). Trailing slashes on every route.
5. **Saved cards** endpoints: `POST/GET /api/v1/payment-methods/`, `DELETE /api/v1/payment-methods/{id}/`.
6. Tests with a faked transport (`python-testing`), type check, sandbox end-to-end run.

### Flow -> SDK call mapping

| Flow | SDK calls, in order |
| --- | --- |
| Pay, raw card | `orders.create_order` (intent `AUTHORIZE`, `payment_source.card` = number/expiry/security_code/name/billing_address, `prefer="return=representation"`, `pay_pal_request_id`) -> if response `status` is `PAYER_ACTION_REQUIRED` STOP (record + 402/409 "card requires browser approval", report); if `purchase_units[0].payments.authorizations[0]` present use it; else if `status` is `APPROVED`/`CREATED` call `orders.authorize_order(order_id, prefer="return=representation", pay_pal_request_id=...)` and read `purchase_units[0].payments.authorizations[0]` |
| Pay, saved card | same, with `payment_source.card.vault_id=<SavedCard.vault_id>` and no PAN/CVV |
| Save card | `vault.create_payment_token(PaymentTokenRequest(customer=Customer(id=<PayPal customer id>) or Customer(merchant_customer_id=str(user.pk)) on first save, payment_source=PaymentTokenRequestPaymentSource(card=PaymentTokenRequestCard(...))), pay_pal_request_id=...)` -> store `id`, `customer.id`, `payment_source.card.brand/last_digits/expiry` |
| List cards | local `SavedCard` rows (source of truth for ownership); optional refresh via `vault.list_customer_payment_tokens(customer_id, page_size=..., page=...)` looping `page` up to `total_pages` |
| Delete card | `vault.delete_payment_token(vault_id)` (returns `None`), then mark `SavedCard.deleted_at` |
| Fulfil | `payments.get_authorized_payment(auth_id)` (status refresh) -> `payments.capture_authorized_payment(auth_id, body=CaptureRequest(amount=..., final_capture=True), prefer="return=representation", pay_pal_request_id=...)`; on stale authorization -> `payments.reauthorize_payment(auth_id, prefer="return=representation", pay_pal_request_id=...)` once, then capture the NEW authorization id |
| Cancel | `payments.void_payment(auth_id, prefer="return=representation", pay_pal_request_id=...)` |
| Refund | `payments.refund_captured_payment(capture_id, body=RefundRequest(amount=Money(...)), prefer="return=representation", pay_pal_request_id=<derived from caller key>)` |
| Status refresh | `orders.get_order`, `payments.get_authorized_payment`, `payments.get_captured_payment`, `payments.get_refund` |
| Reconciliation | `transaction_search.search_transactions(start_date, end_date, fields="transaction_info", page_size=..., page=n)` for every 31-day window of `[from, to]` and every `page` 1..`total_pages` |

## 2. Contract sheet

### 2.1 Client, configuration, auth

| Item | Contract |
| --- | --- |
| Sync vs async | **Sync** — project is WSGI/runserver, views are sync DRF. Class `pay_pal_server_sdk.Client` (alias of `PayPalServerSdkClient`). `AsyncClient` not used. |
| Construction | keyword-only: `Client(oauth2=ClientCredentials(client_id=..., client_secret=...), base_url=<see below>, timeout=<float>, retry_options=<see 2.4>)`. `ClientCredentials` from `pay_pal_server_sdk.core`. Every keyword optional: omitting `oauth2` sends requests unauthenticated silently — gateway must always pass it. |
| Where it lives | one module-level instance created lazily on first gateway call (not at import, not before a fork), reused by all requests; `client.close()` registered with `atexit`. A closed client cannot be reused. Thread-shared under threaded runserver (token cached by the client). |
| `base_url` | SDK default (`ServerConfig.base_url`, `pay_pal_server_sdk/server/server_config.py`) is `https://api-m.sandbox.paypal.com`. **Omitting `base_url` silently selects sandbox.** Rule: if `PAYPAL_BASE_URL` is set and non-blank, pass it verbatim as `base_url=` (the token fetch goes to `<base_url>/v1/oauth2/token`, so it moves too — sdk-map *Servers & auth*). Else if `PAYPAL_ENVIRONMENT == "sandbox"` pass `base_url=None` (SDK default). Else (any other environment with no override) fail fast at startup: the SDK declares only the sandbox server, so no other host may be hard-coded or recalled. |
| Token endpoint | not configured separately; derived from `base_url`. |
| Token-fetch failure | surfaces as a non-`Error` failure on the first call (`core/auth/models.py`: `OAuthError = OAuthProviderError \| RawError`); gateway maps it to 502 "payment provider unavailable" with no body echo. Details: `python-authentication` / `python-error-handling`. |
| `timeout` | default `30.0` s per wait (`base_client.py`); non-positive raises `ValueError`. Use e.g. `20.0`. Sync code: no whole-call budget exists beyond per-wait timeout x retries; state it, none added. |
| Logging | off by default; this integration does NOT raise the `pay_pal_server_sdk` logger in production and turns on no `logging_options` dumps. **Never enable `log_request_body`**: a JSON body is logged as sent, which would write the PAN/CVV of `create_order`/`create_payment_token`. Default `mask_keys` do not cover `number`/`security_code`. Verification run of non-card calls only under `PAY_PAL_SERVER_SDK_LOG=info` (request line only). |

### 2.2 Operations in scope (all sync; `Async…` peers identical and unused)

All calls end with keyword-only `request_options`. Everything after `*` must be passed by name; no defensive `None`s.
Import models from `pay_pal_server_sdk.models`, enums from `pay_pal_server_sdk.models.enums`, `ApiError`/`RawError`/
`TransportError`/`ClientCredentials` from `pay_pal_server_sdk.core`, error unions from `pay_pal_server_sdk.errors`.

| # | Operation | Route | Positional | Keyword-only used | Parsed return | `ApiError.error` union (arms [status]) |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `client.orders.create_order` | `POST /v2/checkout/orders` | `body: OrderRequest \| OrderRequestDict` | `pay_pal_request_id` (docstring: **mandatory for single-step create with card / vault_id**; stored 6 h), `prefer="return=representation"` | `Order` | `CreateOrderErrorBody` = `Error` [400, 401, 422] · `RawError` [other] |
| 2 | `client.orders.authorize_order` | `POST /v2/checkout/orders/{id}/authorize` | `id_: str` | `pay_pal_request_id`, `prefer="return=representation"`, `body` omitted | `OrderAuthorizeResponse` | `AuthorizeOrderErrorBody` = `Error` [400, 401, 403, 404, 422, 500] · `RawError` |
| 3 | `client.orders.get_order` | `GET /v2/checkout/orders/{id}` | `id_: str` | — | `Order` | `GetOrderErrorBody` = `Error` [401, 404] · `RawError` |
| 4 | `client.payments.get_authorized_payment` | `GET /v2/payments/authorizations/{authorization_id}` | `authorization_id: str` | — | `PaymentAuthorization` | `GetAuthorizedPaymentErrorBody` = `Error` [401, 403, 404] · `RawError` [500, other] |
| 5 | `client.payments.capture_authorized_payment` | `POST /v2/payments/authorizations/{authorization_id}/capture` | `authorization_id: str` | `pay_pal_request_id` (stored 45 days), `prefer="return=representation"`, `body: CaptureRequest` | `CapturedPayment` | `CaptureAuthorizedPaymentErrorBody` = `Error` [400, 401, 403, 404, 409, 422] · `RawError` [500, other] |
| 6 | `client.payments.reauthorize_payment` | `POST /v2/payments/authorizations/{authorization_id}/reauthorize` | `authorization_id: str` | `pay_pal_request_id` (45 days), `prefer="return=representation"`, `body: ReauthorizeRequest` optional (only `amount` supported) | `PaymentAuthorization` | `ReauthorizePaymentErrorBody` = `Error` [400, 401, 403, 404, 422] · `RawError` [500, other] |
| 7 | `client.payments.void_payment` | `POST /v2/payments/authorizations/{authorization_id}/void` | `authorization_id: str` | `pay_pal_request_id` (45 days), `prefer="return=representation"` (**required**, see 2.5) | `PaymentAuthorization` | `VoidPaymentErrorBody` = `Error` [401, 403, 404, 409, 422] · `RawError` [500, other] |
| 8 | `client.payments.get_captured_payment` | `GET /v2/payments/captures/{capture_id}` | `capture_id: str` | — | `CapturedPayment` | `GetCapturedPaymentErrorBody` = `Error` [401, 403, 404] · `RawError` [500, other] |
| 9 | `client.payments.refund_captured_payment` | `POST /v2/payments/captures/{capture_id}/refund` | `capture_id: str` | `pay_pal_request_id` (45 days), `prefer="return=representation"`, `body: RefundRequest` | `Refund` | `RefundCapturedPaymentErrorBody` = `Error` [400, 401, 403, 404, 409, 422] · `RawError` [500, other] |
| 10 | `client.payments.get_refund` | `GET /v2/payments/refunds/{refund_id}` | `refund_id: str` | — | `Refund` | `GetRefundErrorBody` = `Error` [401, 403, 404] · `RawError` [500, other] |
| 11 | `client.vault.create_payment_token` | `POST /v3/vault/payment-tokens` | `body: PaymentTokenRequest \| PaymentTokenRequestDict` | `pay_pal_request_id` (stored 3 h) | `PaymentTokenResponse` | `CreatePaymentTokenErrorBody` = `Error` [400, 403, 404, 422, 500] · `RawError` |
| 12 | `client.vault.list_customer_payment_tokens` | `GET /v3/vault/payment-tokens` | `customer_id: str` | `page_size` (default 5), `page` (default 1), `total_required` (default False) | `CustomerVaultPaymentTokensResponse` | `ListCustomerPaymentTokensErrorBody` = `Error` [400, 403, 500] · `RawError` |
| 13 | `client.vault.get_payment_token` | `GET /v3/vault/payment-tokens/{id}` | `id_: str` | — | `PaymentTokenResponse` | `GetPaymentTokenErrorBody` = `Error` [403, 404, 422, 500] · `RawError` |
| 14 | `client.vault.delete_payment_token` | `DELETE /v3/vault/payment-tokens/{id}` | `id_: str` | — | **`None`** — use `.with_raw_response` to see the status | `DeletePaymentTokenErrorBody` = `Error` [400, 403, 500] · `RawError` |
| 15 | `client.transaction_search.search_transactions` | `GET /v1/reporting/transactions` | `start_date: str`, `end_date: str` | `fields` (default `"transaction_info"`), `balance_affecting_records_only` (default `"Y"`), `page_size` (default 100), `page` (default 1) | `SearchResponse` | **Case B**: `RawError` only (no typed arm) |

Not used (available, out of scope): `orders.capture_order`, `orders.confirm_order`, `orders.patch_order`,
tracking ops, `vault.create_setup_token`/`get_setup_token` (see A&B #3), `search_balances`, subscriptions.

Of the in-scope operations only `delete_payment_token` returns `None` (patch/tracking ops also do, not used).

### 2.3 Request models the task sets (required vs `UNSET`)

`Optional[T]` here is `pay_pal_server_sdk.core.Optional` — defaults to `UNSET`, has **no `None` arm**; omit a field
instead of passing `None`. Dict companions (`…Dict`, `NotRequired`) are accepted wherever a signature names them.

| Model (module under `pay_pal_server_sdk/models/`) | Members used | Required? |
| --- | --- | --- |
| `OrderRequest` (`order_request.py`) | `intent: CheckoutPaymentIntentOrStr` -> `CheckoutPaymentIntent.AUTHORIZE` (`"AUTHORIZE"`) · `purchase_units: list[PurchaseUnitRequest]` · `payment_source: PaymentSource` | intent, purchase_units **required**; payment_source UNSET-able (always set here) |
| `PurchaseUnitRequest` (`purchase_unit_request.py`) | `amount: AmountWithBreakdown` (required) · `reference_id` · `custom_id` (set to local `Order.id` — used by reconciliation) · `invoice_id` · `description` | amount **required**; rest UNSET |
| `AmountWithBreakdown` (`amount_with_breakdown.py`) | `currency_code: str` = `settings.PAYPAL_CURRENCY` · `value: str` = `str(order.total_amount.quantize(Decimal("0.01")))` · `breakdown` not set | currency_code, value **required** |
| `PaymentSource` (`payment_source.py`) | `card: CardRequest` | UNSET |
| `CardRequest` (`card_request.py`) | raw: `number`, `expiry` (**`YYYY-MM`**), `security_code`, `name`, `billing_address: Address` · vaulted: `vault_id` only | all UNSET-able; set raw group XOR `vault_id` |
| `Address` (`address.py`) | `address_line_1`, `address_line_2`, `admin_area_2` (city), `admin_area_1` (state), `postal_code`, `country_code: str` | **`country_code` required**; rest UNSET |
| `CaptureRequest` (`capture_request.py`) | `amount: Money` (= authorized amount) · `final_capture: bool` -> **set `True` explicitly** (field default is `False`) · `invoice_id` optional | all optional; final_capture defaults False |
| `ReauthorizeRequest` (`reauthorize_request.py`) | `amount: Money` (omit -> original amount) | UNSET |
| `RefundRequest` (`refund_request.py`) | `amount: Money` (omit -> full refund of the capture; set for partial) · `note_to_payer` optional | all UNSET |
| `Money` (`money.py`) | `currency_code: str`, `value: str` | both **required** |
| `PaymentTokenRequest` (`payment_token_request.py`) | `customer: Customer` (UNSET) · `payment_source: PaymentTokenRequestPaymentSource` | payment_source **required** |
| `Customer` (`customer.py`) | `id` ("unique ID for a customer generated by PayPal") · `merchant_customer_id` (associate PayPal customer to ours) | both UNSET |
| `PaymentTokenRequestPaymentSource` (`payment_token_request_payment_source.py`) | `card: PaymentTokenRequestCard` (other arm: `token: VaultTokenRequest`) | UNSET |
| `PaymentTokenRequestCard` (`payment_token_request_card.py`) | `name`, `number`, `expiry` (`YYYY-MM`), `security_code`, `brand: CardBrandOrStr` (omit), `billing_address: Address` | all UNSET |

### 2.4 Response members the implementer must read and assert on

Every response member below is `Optional` (UNSET-able); only `SellerReceivableBreakdown.gross_amount` and
`Money.currency_code/value` and `Error.name/message/debug_id`, `ErrorDetails.issue` are required on decode. A
truncated 2xx body therefore decodes "successfully" with UNSET members — **the gateway must assert each member it
uses is set (`is not UNSET` / isinstance str) and treat a missing one as an upstream failure**, never write
`UNSET` into the DB.

| Response | Members to read / assert |
| --- | --- |
| `Order` (`order.py`), `OrderAuthorizeResponse` (`order_authorize_response.py`) | `id`; `status: OrderStatusOrStr` — enum `OrderStatus`: `CREATED, SAVED, APPROVED, VOIDED, COMPLETED, PAYER_ACTION_REQUIRED`; `purchase_units[0].payments` (`PaymentCollection`, `payment_collection.py`) -> `.authorizations: list[AuthorizationWithAdditionalData]` / `.captures: list[OrdersCapture]` / `.refunds: list[Refund]`; `links: list[LinkDescription]` (`href`, `rel`, `method`) — a `rel == "payer-action"` link or status `PAYER_ACTION_REQUIRED` = 3DS/browser challenge -> STOP; `payment_source.card` (`CardResponse`: `brand`, `last_digits`, `expiry`, `name`) for safe display |
| `AuthorizationWithAdditionalData` (`authorization_with_additional_data.py`), `PaymentAuthorization` (`payment_authorization.py`) | `id`, `status: AuthorizationStatusOrStr` — enum `AuthorizationStatus`: `CREATED, CAPTURED, DENIED, PARTIALLY_CAPTURED, VOIDED, PENDING`; `status_details.reason` (`AuthorizationIncompleteReasonOrStr`); `amount: Money`; `expiration_time: str`; `create_time` |
| `CapturedPayment` (`captured_payment.py`) | `id`, `status: CaptureStatusOrStr` — enum `CaptureStatus`: `COMPLETED, DECLINED, PARTIALLY_REFUNDED, PENDING, REFUNDED, FAILED`; `status_details.reason`; `amount: Money`; `final_capture: bool`; **`seller_receivable_breakdown: SellerReceivableBreakdown`** (`seller_receivable_breakdown.py`): `gross_amount: Money` (required), `paypal_fee: Money` (UNSET-able), `net_amount: Money` (UNSET-able) |
| `Refund` (`refund.py`) | `id`, `status: RefundStatusOrStr` — enum `RefundStatus`: `CANCELLED, FAILED, PENDING, COMPLETED`; `amount: Money`; `status_details.reason` |
| `PaymentTokenResponse` (`payment_token_response.py`) | `id` (the vault id); `customer: CustomerResponse` -> `.id` (PayPal customer id to persist); `payment_source: PaymentTokenResponsePaymentSource` -> `.card: CardPaymentTokenEntity` (`card_payment_token_entity.py`): `brand: CardBrandOrStr`, `last_digits`, `expiry`, `name`, `verification_status` |
| `CustomerVaultPaymentTokensResponse` (`customer_vault_payment_tokens_response.py`) | `payment_tokens: list[PaymentTokenResponse]`, `total_pages`, `total_items` |
| `SearchResponse` (`search_response.py`) | `transaction_details: list[TransactionDetails]` -> `.transaction_info: TransactionInformation` (`transaction_information.py`): `transaction_id`, `paypal_reference_id`, `transaction_event_code`, `transaction_initiation_date`, `transaction_amount: Money`, `fee_amount: Money`, `transaction_status` (`D`/`P`/`S`/`V` per docstring), `invoice_id`, `custom_field`; plus `page`, `total_pages`, `total_items` |
| `Error` (`error.py`) — typed error arm | `name: str`, `message: str`, `debug_id: str` (required), `details: list[ErrorDetails]` -> `issue: str` (required), `description`, `field` |

Enums are open (`…OrStr`): a value this SDK version does not know arrives as a plain `str`; compare with
`== AuthorizationStatus.CREATED` (str-Enum) or against `.value`, and treat unknown values as "unknown, refresh later".

### 2.5 Semantics read from docstrings (`apis/*.py`) that decide what to pass

| Fact | Source / consequence |
| --- | --- |
| `prefer` defaults to `"return=minimal"` on every POST in scope; minimal = "id, status and HATEOAS links" only | Pass `prefer="return=representation"` on create_order, authorize_order, capture, reauthorize, void, refund — otherwise `purchase_units[].payments`, `seller_receivable_breakdown` etc. are absent. |
| `void_payment` docstring: the 200 JSON body "is returned when the Prefer header is set to return=representation"; decoder is `json_decoder[PaymentAuthorization]` | With minimal Prefer the empty 204 body makes the decoder raise `ValueError` (`core/_internal/wire.py::parse_json`) in BOTH response modes. Always pass representation; if a `ValueError` still occurs, confirm via `get_authorized_payment` (status `VOIDED`) before reporting. |
| `create_order` `pay_pal_request_id`: mandatory for single-step create with card/vault_id; keys stored 6 h | Always send a deterministic id from the claim row (e.g. `f"pay-{payment.pk}-{attempt}"`). |
| `reauthorize_payment`: reauthorize after the 3-day honor period; allowed within the 29-day period; after 30 days a new authorization is required; supports only `amount` | Fulfil: capture first; on a capture `ApiError` 422 (`Error` arm) -> reauthorize once -> capture the new authorization id. A reauthorize `ApiError` (any `Error` 4xx), or authorization older than 29 days, or status `VOIDED`/`DENIED` -> 409 "authorization expired and cannot be renewed; re-collect payment from the shopper", carrying `details[0].issue` and `debug_id` (not the raw body). |
| `search_transactions`: dates RFC 3339 with seconds; **max range 31 days**; up to 3 h reporting lag; `page` starts at 1 | Split `[from, to]` into <=31-day windows; for each, loop `page=1..total_pages` (stop when `page >= total_pages` or `transaction_details` empty). Use `page_size=100` (default) — upper bound not stated by SDK, see A&B. An empty result is a valid sandbox result. |
| `capture_authorized_payment` `final_capture` | `CaptureRequest.final_capture` defaults `False`; set `True` (one capture per order). |
| SDK always adds its own `Idempotency-Key: uuid4()` header on these POSTs (`apis/payments.py`) | Not an idempotency mechanism (random per call). PayPal dedupe is `PayPal-Request-Id` only. |
| `CardRequest` docstring: raw PAN/CVV via API requires PCI SAQ D | Cards pass through memory only; never persisted, never logged (see 2.1 Logging). |

### 2.6 Errors, decode failures, transport

| Case | What raises | Gateway handling |
| --- | --- | --- |
| API error status (parsed call) | `ApiError` (`core/exceptions.py`): `.error` (union per row in 2.2), `.status_code: int`, `.headers` | `isinstance(e.error, Error)` -> map by status: 400/422 -> 422/409 app error with `details[0].issue`; 401/403 -> 502 "provider auth/permission"; 404 -> 404/409; 409 -> 409 "operation in progress, retry"; `RawError` -> 502. Never return `e.error.text()` or the `Error` body to the client; log `debug_id`, status, issue only. |
| Case B (`search_transactions`) | `ApiError` with `.error: RawError` (`status_code`, `content`, `text()`, `json()`) | 502 "reporting unavailable"; a 403 means the account lacks reporting entitlement (A&B #4). |
| 2xx body undecodable | `pydantic.ValidationError` / `ValueError` — **not** `ApiError` — in both response modes | catch separately -> 502; for void see 2.5. |
| No response / connection error | the `httpx2` exception itself, unwrapped (the SDK raises `TransportError` only inside its retry loop): `ConnectError`/`ConnectTimeout`/`PoolTimeout`/`ProxyError` = never sent (502), other `httpx2.RequestError` = outcome unknown (504) | 502/503; for POSTs the outcome is unknown: keep the claim, mark the row `unknown`, and resolve with the GET status refresh on retry (same `PayPal-Request-Id` makes the replay safe). |
| Token fetch failure | not an `Error`-arm `ApiError`; `.error` is `OAuthProviderError \| RawError` (`core/auth/models.py`) | 502 "payment provider authentication failed"; never echo. |

### 2.7 Retries

Keep the SDK default policy (`max_retries=3`, statuses `408, 429, 500, 502, 503, 504`, methods
`GET, HEAD, PUT, OPTIONS`): GETs (get_order, get_authorized_payment, get_captured_payment, get_refund,
list_customer_payment_tokens, get_payment_token, search_transactions) are retried; **POSTs and DELETE are not
retried** by the SDK. No app-level retry loop. A user-level retry of a POST reuses the same `PayPal-Request-Id`
from the claim row.

## 3. DUPLICATE CLAIMS

Claims live in the Django default DB (SQLite `db.sqlite`), enforced by UNIQUE constraints — the same mechanism the
codebase already uses (`unique=True`, `unique_together`, `OneToOneField`). The claim insert is committed
(`transaction.atomic()` exits) **before** the SDK call; the second caller's insert raises
`django.db.IntegrityError`. Order: claim -> SDK call -> record result; on a provider refusal (`ApiError` with
4xx `Error` arm, or a declined/denied status) release the claim (delete the row) so a corrected retry can claim
again. On an outcome-unknown failure (read timeout, 5xx, unreadable 2xx) keep the claim and resolve via status refresh.

| Write | Where the claim is stored | What rejects the second one | Where that rejection is caught | Where in the code |
| --- | --- | --- | --- | --- |
| Authorize an order (`orders.create_order` + `orders.authorize_order`, `pay/`) | `payments.PayPalPayment` row, `order = OneToOneField(orders.Order)`, inserted with status `pending` before `create_order`; its pk seeds `PayPal-Request-Id` | SQLite UNIQUE on `PayPalPayment.order_id` (OneToOne) | `except IntegrityError` around the claim insert in the pay service -> 409 "payment already in progress or done" | `payments.services.pay_order` (`PayPalPayment.objects.create`) -> `payments.paypal.gateway.authorize` |
| Capture at fulfilment (`payments.capture_authorized_payment`, incl. its `reauthorize_payment`) | `payments.PayPalSettlementClaim` row, `payment = OneToOneField(PayPalPayment)`, `action="capture"` | SQLite UNIQUE on `PayPalSettlementClaim.payment_id` (also blocks a racing void) | `except IntegrityError` around the claim insert in the fulfil service -> 409 "already fulfilled/cancelled or in progress" | `payments.services._claim_settlement` (`PayPalSettlement.objects.create`, action capture) -> `payments.paypal.gateway.capture` (and `gateway.reauthorize` via `services._renew_authorization`) |
| Void at cancel (`payments.void_payment`) | same `PayPalSettlement` table, `action="void"` | SQLite UNIQUE on `PayPalSettlementClaim.payment_id` (a capture claim and a void claim cannot coexist) | `except IntegrityError` around the claim insert in the cancel service -> 409 | `payments.services._claim_settlement` (`PayPalSettlement.objects.create`, action void) -> `payments.paypal.gateway.void` |
| Refund (`payments.refund_captured_payment`) | `payments.PayPalRefund` row (`payment` FK, `idempotency_key` = caller key, `amount`, status `pending`), `UniqueConstraint(fields=["payment", "idempotency_key"])`; `PayPal-Request-Id` derived from the row (`refund-{payment_id}-{key}`). Over-refund guard: inside the SAME `transaction.atomic()` as the insert, after the insert, sum `amount` of this payment's non-failed refund rows (pending included) and raise (rollback) if it exceeds the captured amount — the insert takes SQLite's write lock first, so a concurrent distinct-key refund blocks until this one commits and then sees its amount | SQLite UNIQUE on `(payment_id, idempotency_key)`; distinct keys are separate legitimate rows | `except IntegrityError` around the claim insert in the refund service -> return the existing row's `refundId`/status (200) if same amount, 409 if the key was reused with a different amount; the over-refund check raises an app `ValidationError` -> 422 | `payments.services.refund_order` (`PayPalRefund.objects.create` + over-refund guard) -> `payments.paypal.gateway.refund` |
| Save a card (`vault.create_payment_token`) | `payments.SavedCard` row inserted with status `pending` before the call, `UniqueConstraint(fields=["customer", "request_key"])`, `request_key` = caller `Idempotency-Key` header (server UUID if absent); `vault_id` filled after | SQLite UNIQUE on `(customer_id, request_key)` | `except IntegrityError` around the claim insert in the save-card service -> 200 with the existing `paymentMethodId` (or 409 while pending) | `payments.services.save_card` (`SavedCard.objects.create`) -> `payments.paypal.gateway.vault_card` |
| First vault customer for a shopper (the `customer` sent with the first `create_payment_token`) | `payments.PayPalVaultCustomer` row, `customer = OneToOneField(customers.Customer)`, `paypal_customer_id` null until the first token response sets it | SQLite UNIQUE on `PayPalVaultCustomer.customer_id` | `except IntegrityError` around the claim insert -> 409 "another card save in progress, retry" | `payments.services.save_card` (`PayPalVaultCustomer.objects.create`) -> `payments.paypal.gateway.vault_card` |
| Delete a saved card (`vault.delete_payment_token`) | `payments.SavedCardDeletion` row, `saved_card = OneToOneField(SavedCard)`; `SavedCard.deleted_at` set after success (and excluded from list / pay lookups as soon as the claim exists) | SQLite UNIQUE on `SavedCardDeletion.saved_card_id` | `except IntegrityError` around the claim insert in the delete service -> 204 (already deleted / in progress) | `payments.services.delete_card` (`SavedCardDeletion.objects.create`) -> `payments.paypal.gateway.delete_vaulted_card` |

## 4. Assumptions & Blockers

**Blockers: none.** Every capability in the request is exposed by SDK 2.29: direct-card AUTHORIZE order
(`create_order` with `payment_source.card` raw fields), vaulted-card payment (`CardRequest.vault_id`), inline or
separate authorization (`create_order` representation / `authorize_order`), payer-action detection
(`OrderStatus.PAYER_ACTION_REQUIRED`, `links[].rel`), browserless vaulting directly from card
(`vault.create_payment_token` with `PaymentTokenRequestPaymentSource.card`), list/delete tokens, capture with
`seller_receivable_breakdown` (`gross_amount`, `paypal_fee`, `net_amount`), reauthorize, void, refund with
`PayPal-Request-Id`, get authorization/capture/order/refund, paginated transaction search, and a `base_url` that also
moves the token fetch.

Assumptions (minor unless marked):

1. **(major, runtime-only)** The sandbox may answer a card `create_order` or `create_payment_token` with
   `PAYER_ACTION_REQUIRED` / a `payer-action` link (3DS). The code detects and stops (no approval round-trip); if it
   happens during verification, the task says STOP and report — that is a runtime finding, not an SDK gap.
2. **(major, runtime-only)** Entitlements: vaulting (`/v3/vault/payment-tokens`) and reporting
   (`/v1/reporting/transactions`) can answer `403` if the REST app lacks those features. The main agent must smoke
   `create_payment_token` (then delete it) and `search_transactions` against the real credential before editing;
   a 403 comes back to the planner as a revision.
3. Vault path chosen: `create_payment_token` directly from card (one call, no browser). The setup-token path
   (`create_setup_token` -> `create_payment_token` with `VaultTokenRequest(type=SETUP_TOKEN)`) is the fallback if
   PayPal rejects direct card tokenization; setup tokens can return `PaymentTokenStatus.PAYER_ACTION_REQUIRED`.
4. PayPal customer id: the first save sends `Customer(merchant_customer_id=str(user.pk))` (no `id`), and the
   response `customer.id` is persisted in `PayPalVaultCustomer`; later saves send `Customer(id=<that>)`. Listing
   for the API is served from local `SavedCard` rows (ownership is local); `list_customer_payment_tokens` is used
   only for an optional sync, paging `page` 1..`total_pages`.
5. Card on a vaulted payment: only `vault_id` is sent; no `stored_credential` (customer-initiated, customer present).
6. Amount formatting assumes a 2-decimal currency (`Decimal.quantize(Decimal("0.01"))`); a zero-decimal
   `PAYPAL_CURRENCY` (e.g. JPY) would need a different exponent — the SDK carries no currency-exponent table.
7. Stale-authorization detection: the SDK exposes no list of PayPal `issue` codes, so the code must not branch on
   specific issue literals. Rule: capture 422 -> reauthorize once -> capture the new id; reauthorize failure or
   authorization older than 29 days -> operator-actionable 409. Issue text is surfaced from `ErrorDetails.issue`.
8. `search_transactions` `page_size` maximum is not stated by the SDK; use the default 100 and always loop pages.
   Reconciliation matches `transaction_info.transaction_id` against stored capture/refund/authorization ids and
   `custom_field`/`invoice_id` against `Order.id` (sent as `custom_id`/`invoice_id`).
9. `PAYPAL_ENVIRONMENT` other than `sandbox` without `PAYPAL_BASE_URL` fails fast: the SDK declares only the
   sandbox server and no other host may be hard-coded from memory.
10. SQLite serialization for the refund over-refund guard relies on the claim INSERT taking the write lock before
    the SUM read inside the same atomic block (Django 5.0 SQLite transactions are deferred). Keep the insert first.
11. `Order.save()` recomputes `total_amount` from items; the PayPal amount is read from the saved order after items
    are created.

## 5. REQUIRED READING (load every one before writing code)

| Step / hazard | Pointer |
| --- | --- |
| Every `try/except` around SDK calls; `ApiError` unions, `RawError`, decode (`ValidationError`/`ValueError`) and `TransportError`, token-fetch failure | MUST load `python-error-handling` |
| Constructing the one sync `Client`, its lifetime under WSGI, `close()` | MUST load `python-client-initialization` |
| `oauth2=ClientCredentials(...)`, token caching, silent unauthenticated sends | MUST load `python-authentication` |
| First call per operation: positional/keyword-only split, `with_raw_response` for `delete_payment_token` (returns `None`), `request_options` | MUST load `python-calling-endpoints` |
| Building `OrderRequest`/`CardRequest`/`PaymentTokenRequest`/`CaptureRequest`/`RefundRequest`; `UNSET` vs `None`; open enums; `type_` alias on `Token`/`VaultTokenRequest`/`CardResponse` | MUST load `python-models` |
| `base_url` override (incl. token fetch), timeout, default retries kept, logging kept off (card bodies), `PayPal-Request-Id` + duplicate claims | MUST load `python-configuration-resilience` |
| Any test or throwaway verification script that fakes the transport | MUST load `python-testing` |
