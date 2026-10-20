# Task — Add PayPal payments and saved cards to the ecommerce-API

Make the `ecommerce-API` Django app actually collect money, with **PayPal** as the payment
processor, and let a shopper **save a card** so a later order can be paid without re-entering it.

The app today ends checkout by writing an `Order` row, and its only payment path is Braintree.
This adds PayPal as a payment processor and the operator flows that follow a real payment: hold the
money at checkout, take it at fulfilment, give it back on a return. It is an **additive**
capability — it does not replace the existing catalogue/cart/order flow, and it does not remove or
modify the existing Braintree integration.

You own the design and every implementation decision — architecture, file layout, build order,
patterns. Just honour the mandates and the details below.

---

## What to build

### Flow 1 — Pay for an order

A logged-in shopper places an order and pays for it by card; an operator then fulfils, cancels or
refunds it.

- `POST /api/v1/orders/` — place an order from catalogue items; the request carries catalogue item
  ids and quantities, and reuses the app's existing order/order-item models rather than a parallel
  set (the caller's identity comes from the JWT). The order starts in a state awaiting payment.
- `POST /api/v1/orders/{orderId}/pay/` — **authorize** the order total: put a hold on the money, do
  **not** take it yet. The request either carries card details for a one-off payment, **or** names
  one of the shopper's saved cards (Flow 2) to pay with instead. The amount PayPal holds must equal
  the order total to the cent.
- `POST /api/v1/orders/{orderId}/fulfil/` — an operator marks the order fulfilled, and *that* is
  when the money is actually taken. Afterwards the payment must show what PayPal reported: the
  captured amount, PayPal's fee, and the net proceeds to the merchant. An authorization that has
  gone stale before fulfilment has to be renewed rather than failing the fulfilment outright — and
  one that can no longer be renewed must say so in terms an operator can act on.
- `POST /api/v1/orders/{orderId}/cancel/` — cancel *before* fulfilment: the shopper's held funds are
  released, so no money ever moved.
- `POST /api/v1/orders/{orderId}/refunds/` — return *after* fulfilment: refund the captured payment,
  in full or in part. A partly-refunded order must never become refundable beyond what was captured.
- `GET /api/v1/my-orders/` — the caller's orders with their payment state.
- `GET /api/v1/reconciliation/?from={from}&to={to}` — a report listing PayPal's own record of
  transactions for a date range and lining them up against local orders, so a payment PayPal knows
  about and this app doesn't — or the reverse — is visible. It covers the **whole range, not just
  the first page of it**. `from` and `to` are ISO-8601 date-times.

Amounts come from catalogue prices; the currency comes from configuration (below). Payment
operations must be **idempotent in effect**: a double-click never authorizes or captures the shopper
twice. Refunds carry a caller-supplied idempotency key — repeating a request under the same key must
not refund twice, while two distinct partial refunds of the same capture remain legitimate.

The payment has to carry enough of the state PayPal owns (ids and current status for the hold, the
capture, the refunds) that a later request can act on it, not only the one that started it.

### Flow 2 — Saved cards

A logged-in shopper saves a card once and reuses it for later orders.

- `POST /api/v1/payment-methods/` — save a card for the signed-in shopper. The response identifies
  the saved card and describes it safely enough for the shopper to recognise which card it is —
  never full card details.
- `GET /api/v1/payment-methods/` — the caller's saved cards.
- `DELETE /api/v1/payment-methods/{paymentMethodId}/` — remove a saved card. Afterwards it must no
  longer appear among the caller's saved cards, and must no longer be usable to pay.

A saved card belongs to the shopper who saved it: one shopper must never see, use, or delete
another's. The same goes for orders. **Full card details are never stored in the application's own
database and never written to logs.**

---

## Where it goes

Expose all capabilities as DRF endpoints routed under `/api/v1/`, exactly as named above, following
the project's existing app/serializer/view conventions (`config/urls.py` already mounts each app's
`urls.py` under that prefix). Authentication is the project's existing
`djangorestframework-simplejwt` setup — the caller's identity comes from the token, obtained from
`/auth/token/`. Every flow above has to be drivable through the API alone, and each action a caller
can take stays **separately invocable** — not one do-everything call that pays, fulfils and refunds
behind a single route. No storefront UI is required.

Fulfil, cancel and reconciliation are **operator actions**: restrict them to the project's existing
admin/staff role. Every other endpoint is shopper-scoped and acts only on the caller's own data.

**Trailing slashes matter.** Django's `APPEND_SLASH` will 301-redirect a slashless request, which
some clients will not follow on a POST. Register the routes with the trailing slash shown above.

### Response identifiers

So the flows can be driven end to end by a caller, a response that creates something returns its
identifier as a **top-level field** of the response body: `orderId` from `POST /api/v1/orders/`,
`paymentMethodId` from `POST /api/v1/payment-methods/`, and `refundId` from
`POST /api/v1/orders/{orderId}/refunds/`. Everything else about the response shape is your call.

---

## PayPal tooling — non-negotiable

- Use the **paypal-sdk** plugin (from the **maxio-subagent-exp** marketplace) for every
  PayPal interaction. It is your sole reference for how to talk to PayPal.
- Do **not** web-search or rely on general/external knowledge for PayPal API details.
- If the plugin does not expose a capability you need, **STOP and report the gap** — do not invent or
  work around it.

---

## Sandbox entities & test fixtures

Nothing is pre-seeded on the PayPal side — orders, payments and saved cards are all created
dynamically. A sandbox business account is the merchant, and the app's REST credentials (client id /
secret) belong to it.

That account is enabled for direct card processing and for vaulting cards, so the whole task is
drivable without a browser. Verify the payment, saved-card, fulfilment, cancel and refund flows with
a direct card payment using PayPal's sandbox test card: **Visa 4111 1111 1111 1111**, any future
expiry date, any CVC, any name and billing address. No card number is ever kept by this app.

PayPal's transaction reporting lags live activity, so a reconciliation range covering payments you
have just created may legitimately come back empty. That is an expected sandbox result, not a
missing capability — build the report so it is correct over a range that *does* have data, and do not
report the empty range as a gap.

If PayPal answers a card payment with a challenge that requires a shopper to approve in a browser,
**STOP and report it** — do not build an approval round-trip instead.

---

## Configuration

Settings arrive as environment variables. `config/settings.py` already reads its existing keys with a
bare `os.environ[...]`; follow whichever pattern you judge right for the new ones, subject to the two
rules below.

| Env var | Meaning |
| --- | --- |
| `PAYPAL_CLIENT_ID` | REST client id |
| `PAYPAL_CLIENT_SECRET` | REST client secret |
| `PAYPAL_ENVIRONMENT` | `sandbox` |
| `PAYPAL_CURRENCY` | ISO currency code for all amounts |
| `PAYPAL_BASE_URL` | **optional override** — see below |

**Rule 1 — `PAYPAL_BASE_URL` is an override you must honour.** When it is set, use it **verbatim as
the API base address for every PayPal call — including the credential/token request** — instead of
deriving one from the environment. Hard-code none of these values: the same build has to run against
a different PayPal account, and against a different base address, than the one above.

**Rule 2 — required configuration must fail fast at startup, and blank counts as missing.** If
`PAYPAL_CLIENT_SECRET` is absent **or present-but-empty**, the app must refuse to start (or refuse to
serve) rather than starting and failing at the first request. Note the boundary: a check that only
tests for `None`/absence is **not** sufficient — an empty string must be rejected too.

Leave the existing Braintree settings and code path working as they are.

---

## Environment notes (this machine)

- **Database.** `DATABASES['default']` is SQLite at `BASE_DIR/db.sqlite`. **No PostgreSQL is
  required** — the `PG_DB` alias is unused. Run `python manage.py migrate` before first use.
- **`DEBUG`.** `settings.py` reads `DEBUG = os.environ['DEBUG']`, which yields a *string*, so any
  non-empty value — `"False"` included — is truthy. Do not rely on it being off. Never let a
  traceback, an internal type name, a secret, or a raw upstream response body reach an HTTP response
  body, regardless of `DEBUG`.
- **Existing env keys.** `settings.py` reads `DB_NAME`, `POSTGRES_USER`, `POSTGRES_PASSWORD`,
  `DB_HOST`, `DB_PORT`, `BRAINTREE_MERCHANT_ID`, `BRAINTREE_PUBLIC_KEY`, `BRAINTREE_PRIVATE_KEY` and
  `DEBUG` with a bare index, so all of them must be present in the environment for the app to import
  at all. Do not change that contract; just be aware of it.
- **Python.** Create the project's virtual environment with Python 3.12, at
  `C:\exp-staging\pr666-pp\python312\python.exe`: `pillow==10.3.0` has no wheel for the 3.13 on
  `PATH`, and `drf-yasg` imports `pkg_resources`, so the environment also needs `setuptools<81`. The
  project pins Django 5.0.6 and DRF 3.15.1. Add dependencies to `requirements.txt`.
- **No new infra.** No Docker, no broker, no PostgreSQL, no Redis. Celery is already a dependency and
  `payments/tasks.py` exists — using it is your call, but do not introduce a new broker requirement
  for the flows above to work synchronously.
- **Ports.** Bind the dev server to `127.0.0.1` on a free port. Stop your previous instance before starting another.

---

## Rules of engagement

We want a production-grade integration — you decide what production-grade looks like.

When done, self-verify that the app runs and the flows actually work — a real authorization on the
sandbox card, a real capture at fulfilment, a real refund, and a saved card reused to pay a second
order. No browser step is required. Then give me a concise, step-by-step guide to verify the working
integration myself.


---

## Constraints

- **Secrets never enter the repository.** Read the API credentials from the environment
  variables above, at run time, and never write their **values** into any file inside this
  repository — not into a settings module, not into `.env`, not into a script, a test fixture,
  a comment, or a commit message. Referencing the variable **names** is fine, the values are
  not.
- **Report a gap only when it is genuinely a gap.** Stop and report when the source you were
  given does not cover a capability this integration requires. A design decision being hard,
  open-ended, or left to your judgment is **not** a gap — decide it and proceed.
- **You are running headless — there is no one to answer you.** Work until the integration
  is fully complete. Never hand back, never end with a question, and never defer remaining
  work to the user: decide and proceed.

