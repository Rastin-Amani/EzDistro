# Billing, plans & managed AI

Billing is **optional and off by default** (`BILLING_ENABLED=false`): a
self-hosted deployment runs with every feature and no paywall. When enabled, the
app sells prepaid plan periods and managed-AI credits through PayRam.

`make bootstrap` seeds an example catalog (`cloud`, `cloud_ai`, `setup`) into
`billing_products`; those rows are yours to re-price, rename or deactivate in the
PocketBase admin, and a re-bootstrap never overwrites existing rows. Commercial
terms are deployment-specific and are not documented in this repository.

## What is metered (and what is not)

| Project setup | Article generation |
|---|---|
| Has an enabled **LLM integration** (BYO keys) | **Unmetered** — the user pays their provider directly. No usage row, no paywall. |
| No LLM integration (**managed AI**) | Consumes **managed-AI credits** (1 per article by default). |

This is the whole model: the only paid counter is managed AI. There is **no
feature gating** — every capability is available on every plan, and projects are
unlimited.

When managed-AI credits run out, generation is refused with a clear message:
add your own provider keys on the Connections tab, or buy a top-up. Only that
path is blocked; everything else keeps working.

## Plans are prepaid periods, not auto-recurring

PayRam performs one-time crypto payments and has no recurring subscription
charge. A plan therefore grants a **prepaid period** (`planDays`, e.g. 30 days)
and a renewal **extends from the current expiry** (buying twice is safe and
stacks). Renewal is manual: the user pays again before/after expiry.

Products are rows in `billing_products` with `type` `plan` or `service`,
`priceCents`, `planDays` and `credits`. Top-up packs are `type=credit_pack`
(set `credits`); add one in the admin if you sell them. Legacy
`starter`/`growth`/`scale` credit packs are retired (set inactive) by the
bootstrap, never deleted, so old invoices still resolve.

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
managed-AI credits; BYO projects keep working unmetered immediately. Old
`billing_usage` free-tier rows remain as history but no longer affect decisions.

## Configuration

Set these in the environment (see `.env.example`); never commit secrets.

| Variable | Required | Meaning |
| --- | --- | --- |
| `BILLING_ENABLED` | to turn on | `true`/`false` (default `false`). |
| `PUBLIC_APP_URL` | yes | Public base URL of this app (checkout return URL). |
| `PAYRAM_BASE_URL` | yes | PayRam Site URL (dashboard → Settings → Site URL). |
| `PAYRAM_API_KEY` | yes | Project API key (server-side only). |
| `PAYRAM_WEBHOOK_SECRET` | no | Distinct signing secret; defaults to the API key. |
| `BILLING_ARTICLE_COST` | no | Managed-AI credits per article (default 1). |
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
Only `paid` grants the plan/credits, exactly once. Overpayment is recorded
(never grants extra). Partial payment grants nothing.

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
  `GET /billing/invoices/{id}/check`, which re-reads authoritative state from
  PayRam and fulfils if paid. Safe to repeat (idempotent).
- **Recover a delayed webhook** — PayRam retries final events for up to 48h; if
  delivery is exhausted, use *Check status* or re-open the return page. Nothing
  requires the browser to stay open.
- **Confirm fulfilment** — a plan invoice has `status=paid`, `fulfilledAt` set,
  a `billing_entitlements` row, and (for Cloud + Managed AI) a
  `credit_transactions` row with `idempotencyKey = invoice:<id>:credits`.
- **Extend a plan manually** — create the entitlement row, or let the user buy
  another period; renewals stack from the current expiry.
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
| "Payment provider not configured" | `PAYRAM_BASE_URL`/`PAYRAM_API_KEY` unset. |
| Checkout button disabled | Same as above, or no active `billing_products`. |
| Webhook returns 401 | Signature mismatch — wrong `PAYRAM_API_KEY`/`PAYRAM_WEBHOOK_SECRET`, or a proxy altered the body. |
| Webhook returns 503 | PayRam unreachable during authoritative re-read; PayRam retries automatically. |
| Paid but no plan | Check the invoice; press **Check status**. Look for a `billing_payment_events` row with `processed=false` and an `error`. |
| "managed-AI allowance is used up" | Expected: that project has no LLM integration. Add keys on Connections, or buy credits. |
| BYO project still charged | It has no enabled `llm` integration — add one; BYO is detected from an enabled `integrations` row in category `llm`. |
| PocketBase permission errors | Re-run `make bootstrap` (collections must be locked/superuser-only). |
