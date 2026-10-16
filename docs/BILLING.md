# Billing & Payments

EzDistro meters its core product — **generating one article** (the
`write_article` job). Each user gets a free allowance; beyond that they buy
**credit packs**, paid in crypto through **PayRam**. Billing is off by default so
existing deployments are unchanged until an operator enables it.

Facts only: credits are integer units, prices are integer minor units
(cents), and money never mixes with credits.

## Model

- **Free allowance** — `BILLING_FREE_ALLOWANCE` article generations per user,
  lifetime by default (`BILLING_FREE_RESET=none`) or resetting each UTC calendar
  month (`BILLING_FREE_RESET=monthly`).
- **Credits** — purchased in packs. One generation costs
  `BILLING_ARTICLE_COST` credits (default 1). Free allowance is consumed first.
- **Charge timing** — a generation is *reserved* when the job is queued, and the
  reservation is *released* (credits refunded) if the generation fails or the
  topic is cancelled. It is *completed* when the article assembles successfully.
- **Subscriptions** — not implemented. PayRam does not perform recurring
  auto-charges; the supported model is one-time credit packs. (A future provider
  adapter can add subscriptions without touching the ledger or usage metering.)

## PocketBase schema

Five collections are added by `make bootstrap` (source of truth:
`app/scripts/bootstrap_pb.py`):

| Collection | Purpose |
| --- | --- |
| `billing_products` | Purchasable credit packs (trusted prices/credits). |
| `billing_invoices` | One per checkout; snapshots the purchased terms. |
| `billing_payment_events` | Verified webhook notifications (dedup key unique). |
| `billing_usage` | One row per generation attempt (free/credit metering). |
| `credit_transactions` | **Append-only** ledger; `idempotencyKey` unique. |

All five are bootstrapped **locked** (superuser-only API rules), like the rest of
the schema: the app reads/writes through the superuser client and authorization
lives in `app/api/` + `app/services/billing.py`.

Default credit packs are seeded once (`seed_billing_products`) and are **never
overwritten** — edit them in the PocketBase admin to change pricing, or add rows.

### Existing users

No historical usage exists, so every user starts with the full free allowance and
zero credits. Nothing is fabricated and no allowance is reset on deploy
(`seed_billing_products` only inserts missing products).

## Configuration

Set these in the environment (see `.env.example`); never commit secrets.

| Variable | Required | Meaning |
| --- | --- | --- |
| `BILLING_ENABLED` | to turn on | `true`/`false` (default `false`). |
| `PUBLIC_APP_URL` | yes | Public base URL of this app (checkout return URL). |
| `PAYRAM_BASE_URL` | yes | PayRam Site URL (dashboard → Settings → Site URL). |
| `PAYRAM_API_KEY` | yes | Project API key (server-side only). |
| `PAYRAM_WEBHOOK_SECRET` | no | Distinct signing secret; defaults to the API key. |
| `BILLING_FREE_ALLOWANCE` | no | Free generations per user (default 3). |
| `BILLING_FREE_RESET` | no | `none` (default) or `monthly`. |
| `BILLING_ARTICLE_COST` | no | Credits per generation (default 1). |
| `BILLING_CURRENCY` | no | Display currency (default USD). |
| `BILLING_PROVIDER` | no | `payram` (default). |
| `PAYRAM_TIMEOUT_SECONDS` | no | Provider call timeout (default 20). |

## PayRam setup

1. In the PayRam dashboard create/open the project and copy its **API key**.
2. Find the **BASE_URL** under *Settings → Site URL*; set `PAYRAM_BASE_URL`.
   Include the port if non-default. Use `https://` in production.
3. Register the webhook: *Settings → Webhook → Add Endpoint*, URL
   `https://<PUBLIC_APP_URL>/webhooks/payram`, mark it **active**.
   - PayRam POSTs JSON and signs the **raw body** with HMAC-SHA256 keyed by the
     project API key, in header `X-Payram-Signature: sha256=<hex>`.
   - This app verifies that signature in constant time (legacy `API-KEY` header
     also accepted). It fails closed — an unset secret never verifies.
4. Set the checkout return URL in PayRam to
   `https://<PUBLIC_APP_URL>/billing/checkout/return` (the page reconciles the
   invoice; it never grants access from query parameters).
5. `PAYRAM_API_KEY` must be set **on the server**. It is never rendered to the
   browser and never logged.

### Payment states

PayRam → internal: `OPEN → awaiting_payment`, `PARTIALLY_FILLED →
partially_paid`, `FILLED`/`OVER_FILLED → paid`, `CANCELLED → cancelled`.
Only `paid` grants credits, exactly once. Overpayment is recorded (never grants
extra credits). Partial payment grants nothing.

## Local development

```bash
make bootstrap          # creates the billing collections + default packs
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
  `GET /billing/invoices/{id}/check`, which re-reads authoritative state from
  PayRam and fulfils if paid. Safe to repeat (idempotent).
- **Recover a delayed webhook** — PayRam retries final events for up to 48h; if
  delivery is exhausted, use *Check status* or re-open the return page. Nothing
  requires the browser to stay open.
- **Confirm fulfilment** — a fulfilled invoice has `status=paid`,
  `fulfilledAt` set, and a matching `credit_transactions` row with
  `idempotencyKey = invoice:{id}:grant`.
- **Refunds** — PayRam crypto payments are not auto-refundable here. Record any
  manual refund as an `adjustment` row in `credit_transactions` (never edit
  history) and set the invoice `status=refunded`. Prefer a compensating grant.
- **Rotate the API key** — update `PAYRAM_API_KEY` (and the webhook signing
  secret if PayRam re-signs with it), restart the web process, then send a test
  payment. Existing invoices are unaffected.
- **After an outage** — invoices left `pending`/`awaiting_payment` can be
  reconciled from the UI; no data is lost.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| "Payment provider not configured" | `PAYRAM_BASE_URL`/`PAYRAM_API_KEY` unset. |
| Checkout button disabled | Same as above, or no active `billing_products`. |
| Webhook returns 401 | Signature mismatch — wrong `PAYRAM_API_KEY`/`PAYRAM_WEBHOOK_SECRET`, or a proxy altered the body. |
| Webhook returns 503 | PayRam unreachable during authoritative re-read; PayRam retries automatically. |
| Paid but no credits | Check the invoice; press **Check status**. Look for a `billing_payment_events` row with `processed=false` and an `error`. |
| "used your free generations and have no credits" | Expected paywall; buy a pack on `/billing`. |
| Scheduled writes fail after enabling billing | The project owner has no allowance/credits. Buy credits or raise `BILLING_FREE_ALLOWANCE`. |
| PocketBase permission errors | Re-run `make bootstrap` (collections must be locked/superuser-only). |
