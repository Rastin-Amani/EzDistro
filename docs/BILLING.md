# Billing, plans & managed AI (optional)

```text
Documentation status:  Verified against app/services/billing.py, app/api/billing.py,
                       app/providers/payments/payram.py, bootstrap schema (v1.27.3)
Last verified:         2026-10-25
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
   `https://<your-host>/webhooks/payram`, mark it **active**.
   - PayRam POSTs JSON and signs the **raw body** with HMAC-SHA256 keyed by the
     project API key, in header `X-Payram-Signature: sha256=<hex>`.
   - This app verifies that signature in constant time (legacy `API-KEY` header
     also accepted). It fails closed — an unset secret never verifies.
4. Set the checkout return URL in PayRam to
   `https://<your-host>/billing/checkout/return` (the page reconciles the
   invoice; it never grants access from query parameters).
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
