# Billing, plans & managed AI (optional)

```text
Documentation status:  Verified against app/services/billing.py, app/api/billing.py,
                       app/api/checkout.py, app/providers/payments/payram.py,
                       bootstrap schema (v1.30.2)
Last verified:         2026-11-01
```

Billing is **optional and off by default** (`BILLING_ENABLED=false`): a
self-hosted deployment runs with every feature and no paywall, and a project with
its own provider keys is never metered.

When enabled, the app sells prepaid plan periods and managed-AI credits through
PayRam. `make bootstrap` seeds an example catalog into `billing_products`; those rows
are yours to re-price, rename or deactivate in the PocketBase admin, and a re-bootstrap
never overwrites existing rows. The seeded example prices are **$49/30 days**
(`cloud`), **$99/30 days + 30 credits** (`cloud_ai`) and **$299 one-time** (`setup`);
they are defaults, not a published price list.

## What is metered (and what is not)

| Project setup | Article generation |
|---|---|
| Has an enabled **LLM integration** (BYO keys) | **Unmetered** — the user pays their provider directly. No usage row, no paywall. |
| No LLM integration (**managed AI**) | A managed-AI credit is reserved per article (1 by default). |

The only paid counter is managed AI. There is **no feature gating**: plans change
nothing in the app except the recorded entitlement. Projects are unlimited on every
plan, and no code checks `plan_active` for authorization.

### Known limitation: managed-AI execution is not wired

The provider registry builds an LLM **only from a project's `llm` integration**;
there is no platform-level LLM key. So a project with no LLM connection:

- is classified as managed-AI and consumes credits, and
- **cannot generate an article** — the registry raises "no active llm integration".

The two paths are mutually exclusive today. Treat credits and the `cloud_ai` catalog
row as recorded entitlements until an operator-facing platform model exists. In
practice every project should have its own LLM connection (unmetered), and
`BILLING_ENABLED` should stay `false` unless you are selling hosting/setup rather
than metered AI.

> **Operator warning:** setting `BILLING_ENABLED=true` without a PayRam config turns
> metering on and checkout off — users without provider keys are blocked and cannot
> buy credits. Configure PayRam first.

## Plans are prepaid periods, not auto-recurring

PayRam performs one-time crypto payments and has no recurring subscription charge. A
plan therefore grants a **prepaid period** (`planDays`, e.g. 30 days). A renewal
extends the expiry **only while the current entitlement is still unexpired**; after
expiry the new period starts at `now`. Nothing in the code flips an entitlement to
`expired` — `current()` filters on `expiresAt`, so expiry is effective without a
status write. Renewal is manual: the user pays again before/after expiry.

Products are rows in `billing_products` with `type` `plan`, `credit_pack` or
`service`, `priceCents`, `planDays` and `credits`. The seeded `cloud_ai` copy says
"~30 managed-AI articles per month", but credits are granted **once per invoice**
(there is no monthly refill), and no `credit_pack` row is seeded — add one in the
admin if you sell top-ups. Legacy `starter`/`growth`/`scale` credit packs are retired
(set inactive) by the bootstrap, never deleted, so old invoices still resolve.

## Self-hosted checkout page

The customer pays on **our** domain at `/checkout/{reference}` — a single card
(order summary, coin/network picker, deposit address, QR, countdown) instead of
PayRam's hosted page. PayRam remains the payment rail.

All browser traffic goes through our routes; `PAYRAM_API_KEY` never reaches the
client bundle, HTML or logs:

| Route | Purpose |
| --- | --- |
| `POST /api/checkout` | Creates the local order, then `POST {BASE_URL}/api/v1/payment`. Body `amountInUSD`, optional `invoiceID` (idempotent re-post), `productCode` (catalog price wins over the client amount), or `currency` + `network` together (restricts the payment and hides the card hand-off). `customerEmail`/`customerID` are accepted for API compatibility but the signed-in user is authoritative. Returns `{referenceID, invoiceID, url}`. |
| `GET /api/checkout/{reference}` | Re-reads PayRam status and returns **only** `{amountInUSD, paymentState, expiresAt}` — never the raw payload (it carries internal ids). Fulfils a paid order exactly once. |
| `GET /api/checkout/{reference}/options` | Maps `blockchain-currency` to coin/network options and assigns one deposit address per blockchain family (Ethereum/Base/Polygon share `ETH_Family`). |
| `GET /checkout/{reference}` | The page itself. Owner-scoped and behind sign-in; a garbage reference renders a clean 404. |
| `GET /checkout/{reference}/return` | Landing page after the hosted card flow. Polls once on mount, then offers a manual refresh; it never fulfils from the URL. |
| `POST /webhooks/payram` (alias `/api/payram/webhook`) | Signed, idempotent webhook. Replay is a no-op. |

The page polls `GET /api/checkout/{reference}` every 5 s only while the payment
is `OPEN`, stops on `FILLED` / `PARTIALLY_FILLED` / `CANCELLED` / `FAILED` or when
the 24-hour window (PayRam's default) runs out, and refetches once when the tab
becomes visible again. Prices always come from the server: `productCode`
purchases ignore the body amount, and one-off orders are validated to
`$1.00–$100,000.00`.

The QR code is generated in the browser by `app/static/js/qrcode.js` (byte mode,
ECC level M, versions 1–6) from the deposit address — no image API and no new
dependency. It encodes the address, not a crypto amount: PayRam quotes in USD,
so an amount in the QR would be a guessed exchange rate.

## Card / fiat on-ramp (hand-off only)

Card, Apple Pay, bank and local methods are a **channel on PayRam's hosted
page**, not an API. The customer creates a PayRam Wallet, completes a one-time
KYC, and pays through the provider's widget; card data never touches this app.
There is no card form here.

This PayRam version exposes no documented create-onramp-session endpoint:
`/swagger` redirects to `/swagger/`, which answers
`{"error":"API documentation not available"}`, and `/api-docs`,
`/openapi.json` and `/docs` are 404. The live payment payload has no channel or
on-ramp field (top-level keys: `amountInUSD`, `paymentState`, `currencySymbol`,
`blockchainSymbol`, `successURL`, `cancelURL`, …). So the checkout **hands off**
to the hosted page and does not invent an endpoint.

**When the tab appears** — both must hold:

1. `PAYRAM_CARD_ONRAMP_ENABLED=true` (default), and Cards enabled for the
   project in PayRam: *Settings → Account → Project → Payment Options → Cards*
   (on by default).
2. The order is **not** restricted to a currency/network pair. A restricted
   payment (created with `currency` + `network`, e.g. `USDT`/`TRON`) hides the
   tab: the providers settle on specific chains (PayRam Wallet → Base,
   Changelly → Ethereum, Wert → Ethereum/Base/Polygon). The restriction is kept
   on the local invoice (`metadata.currency`/`metadata.network`) because
   PayRam's status payload does not expose it.

**The flow** — the chooser shows "Pay with card / Apple Pay / bank" (Instant).
Choosing it renders an explainer, then **Continue to card payment** navigates in
the same tab to:

```text
{PAYRAM_BASE_URL}/payments?reference_id={reference}&host={PAYRAM_BASE_URL}
```

It is never iframed: KYC widgets and frame headers break in a frame.

**Return path** — in the PayRam dashboard set the project's **Redirection URL**
to `https://<your-host>/checkout/{reference}/return` (this app cannot change
PayRam's settings). The route ignores the query string, reads authoritative
status on mount, and shows:

| Status | Page |
| --- | --- |
| `FILLED` / `OVER_FILLED` | thank-you |
| `OPEN` / `PARTIALLY_FILLED` | "still processing" + **Refresh status** + `SUPPORT_URL` |
| `CANCELLED` | expired |

The webhook remains the source of truth for fulfilment. The return page never
grants anything from the redirect alone — it only reads PayRam's status, and
any fulfilment still goes through the idempotent ledger.

## PocketBase schema

Six collections are added by `make bootstrap` (source of truth:
`app/scripts/bootstrap_pb.py`):

| Collection | Purpose |
| --- | --- |
| `billing_products` | Plans, managed-AI top-ups and one-time services. |
| `billing_entitlements` | Prepaid plan periods per user (one per paid invoice). |
| `billing_invoices` | One per checkout; snapshots the purchased terms. |
| `billing_payment_events` | Verified webhook notifications (dedup key unique). |
| `billing_usage` | Managed-AI generation attempts only. |
| `credit_transactions` | **Append-only** managed-AI credit ledger. |

All are bootstrapped **locked** (superuser-only API rules), like the rest of the
schema: the app reads/writes through the superuser client and authorization
lives in `app/api/` + `app/services/billing.py`.

### Existing users

No historical usage is fabricated. Every user starts with no plan and no
managed-AI credits; BYO projects keep working unmetered immediately. Legacy
`billing_usage` rows with `source=free` remain as history — note that an existing
non-released usage row for the same `operationKey` is returned as the reservation, so
a legacy row can still let that one operation through without a balance check.

## Configuration

Set these in the environment (see `.env.example`); never commit secrets.

| Variable | Required | Meaning |
| --- | --- | --- |
| `BILLING_ENABLED` | to turn on | `true`/`false` (default `false`). Gates both the `/billing` page and metering. |
| `PAYRAM_BASE_URL` | for checkout | PayRam Site URL (dashboard → Settings → Site URL). |
| `PAYRAM_API_KEY` | for checkout | Project API key (server-side only). |
| `PAYRAM_WEBHOOK_SECRET` | no | Distinct signing secret; defaults to the API key. |
| `BILLING_PROVIDER` | no | `payram` (default). |
| `BILLING_CURRENCY` | no | Fallback label when a product row has no `currency` (default `USD`). Every charge is USD and the UI renders `$`. |
| `BILLING_ARTICLE_COST` | no | Managed-AI credits per article (default 1). |
| `BILLING_CHECKOUT_COOLDOWN_SECONDS` | no | Reuse a just-created open invoice instead of stacking duplicates (default 5). |
| `BILLING_OVERPAYMENT_POLICY` | no | Defined (`record`) but **not read**; overpayment is always recorded, never granted. |
| `PAYRAM_TIMEOUT_SECONDS` | no | Provider call timeout (default 20). |

`PUBLIC_APP_URL` has no billing role: `create_payment` sends no return URL. The
return page `/billing/checkout/return` works from any origin and reconciles the
invoice without trusting query parameters.

## PayRam setup

1. In the PayRam dashboard create/open the project and copy its **API key**.
2. Find the **BASE_URL** under *Settings → Site URL*; set `PAYRAM_BASE_URL`.
   Include the port if non-default. Use `https://` in production.
3. Register the webhook: *Settings → Webhook → Add Endpoint*, URL
   `https://<your-host>/webhooks/payram` (the alias `/api/payram/webhook` is
   identical), mark it **active**.
   - PayRam POSTs JSON and signs the **raw body** with HMAC-SHA256 keyed by the
     project API key, in header `X-Payram-Signature: sha256=<hex>`.
   - This app verifies that signature in constant time (legacy `API-KEY` header
     also accepted). It fails closed — an unset secret never verifies.
4. Set the checkout return URL in PayRam to
   `https://<your-host>/billing/checkout/return` (the page reconciles the
   invoice; it never grants access from query parameters). The customer does not
   use PayRam's hosted page any more — see the checkout page below.
5. `PAYRAM_API_KEY` must be set **on the server**. It is never rendered to the
   browser and never logged.

### Payment states

PayRam → internal: `OPEN → awaiting_payment`, `PARTIALLY_FILLED →
partially_paid`, `FILLED`/`OVER_FILLED → paid`, `CANCELLED → cancelled`; any other
provider status (`EXPIRED`, `FAILED`, …) maps to `failed`. Only `paid` grants the
plan/credits, exactly once. Overpayment is recorded (never grants extra). Partial
payment grants nothing.

Invoice statuses the flow can write: `pending` (before checkout), `awaiting_payment`,
`partially_paid`, `paid`, `failed`, `expired`, `cancelled`, `refunded`,
`needs_review` (provider call failed after the invoice row was created — reconcile
manually or retry checkout).

## Local development

```bash
make bootstrap          # creates billing collections + plan catalog
BILLING_ENABLED=true PAYRAM_BASE_URL=... PAYRAM_API_KEY=... make web
```

Tests use an in-memory fake provider and never touch PayRam:

```bash
.venv/bin/pytest tests/test_billing.py -q
```

## Operations

- **Investigate a pending payment** — open the user's `/billing` page → the
  invoice card, or the PocketBase `billing_invoices` / `billing_payment_events`
  rows. The invoice's `status` and `error` fields show what happened.
- **Retry reconciliation** — the invoice card's **Check status** button calls
  `POST /billing/invoices/{id}/check`, which re-reads authoritative state from
  PayRam and fulfils if paid. Safe to repeat (idempotent).
- **Recover a delayed webhook** — PayRam retries final events for up to 48h; if
  delivery is exhausted, use **Check status** or re-open the return page. Nothing
  requires the browser to stay open.
- **Confirm fulfilment** — a plan invoice has `status=paid`, `fulfilledAt` set,
  a `billing_entitlements` row, and (for Cloud + Managed AI) a
  `credit_transactions` row with `idempotencyKey = invoice:<id>:credits`.
- **Extend a plan manually** — create the entitlement row, or let the user buy
  another period; renewals stack from the current expiry while it is unexpired.
- **Grant managed-AI credits** — append a `grant` row to `credit_transactions`
  with a unique `idempotencyKey` (never edit history).
- **Refunds** — PayRam crypto payments are not auto-refundable here. Record a
  compensating `adjustment` row and set the invoice `status=refunded`.
- **Rotate the API key** — update `PAYRAM_API_KEY` (and the webhook signing
  secret if PayRam re-signs with it), restart the web process, then send a test
  payment. Existing invoices are unaffected.
- **After an outage** — invoices left `pending`/`awaiting_payment` can be
  reconciled from the UI; no data is lost.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| "Payment provider not configured" | `PAYRAM_BASE_URL`/`PAYRAM_API_KEY` unset; restart the web process after setting them. |
| No plan buttons at all | No active `billing_products` (the page shows the empty state). The button is only disabled for "not configured". |
| "Billing is disabled." after clicking | `BILLING_ENABLED=false` while PayRam is configured — the buttons render but the service refuses. |
| Webhook returns 401 | Signature mismatch — wrong `PAYRAM_API_KEY`/`PAYRAM_WEBHOOK_SECRET`, or a proxy altered the body. |
| Webhook returns 503 | PayRam unreachable during authoritative re-read; PayRam retries automatically. |
| Webhook for an unknown reference | Stored with `processed=true` and `error="unknown provider reference"`, returns 200 — inspect `billing_payment_events`. |
| Paid but no plan | Check the invoice; press **Check status**. Look for a `billing_payment_events` row with an `error`, or an invoice in `needs_review`. |
| "managed-AI allowance is used up" | Expected: that project has no LLM integration. Buy credits **and** add keys on Connections — credits alone cannot generate. |
| BYO project still charged | It has no enabled `llm` integration — add one; BYO is detected from an enabled `integrations` row in category `llm`. |
| PocketBase permission errors | Re-run `make bootstrap` (collections must be locked/superuser-only). |
