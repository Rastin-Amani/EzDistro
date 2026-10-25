"""Billing service — plans + managed-AI metering.

Model:

* Self-hosted / BYO provider keys → **unmetered**. The user pays their provider
  directly, so article generation costs nothing here.
* Managed AI (a project with no enabled LLM integration) → metered against
  purchased **managed-AI credits**, granted by a plan's credit allowance or a
  top-up pack. Over the counter → add your own keys or buy more.
* **Plans** are prepaid hosting periods (PayRam cannot auto-charge a recurring
  subscription), so a renewal extends from the current expiry.
* No feature gating: every capability is available on every plan.

Money and credits stay separate: invoices carry integer minor-unit prices;
managed-AI credits are integer units; the ledger is append-only.
"""

from __future__ import annotations

import datetime as dt
import secrets
import threading
from typing import Any

from structlog import get_logger

from app.config import settings
from app.providers.payments.base import (
    PAID_STATUSES,
    STATUS_AWAITING,
    STATUS_PAID,
    STATUS_PARTIALLY_PAID,
    BillingConfigError,
    PaymentProviderError,
    get_payment_provider,
)
from app.repositories.billing import (
    BillingProductRepo,
    CreditRepo,
    EntitlementRepo,
    InvoiceRepo,
    PaymentEventRepo,
    UsageRepo,
)
from app.repositories.integrations import IntegrationRepo
from app.repositories.jobs import now_utc, pb_dt
from app.repositories.members import MemberRepo

logger = get_logger(__name__)

UNIT = "article"
_CREDIT = "credit"

# Billing is per-user. When billing is disabled there is no metering at all, and
# BYO projects are never metered even when billing is on.
LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class BillingError(ValueError):
    """A user-facing billing problem (allowance exhausted, bad product, …)."""


class WebhookVerificationError(Exception):
    """The provider signature could not be verified — the request must be rejected."""


def _user_lock(user_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = LOCAL_LOCKS.get(user_id)
        if lock is None:
            lock = threading.Lock()
            LOCAL_LOCKS[user_id] = lock
        return lock


def clear_billing_locks() -> None:
    """Test/rotation helper — drop cached per-user locks."""
    with _LOCKS_GUARD:
        LOCAL_LOCKS.clear()


def _parse_pb_dt(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.UTC)
    except (TypeError, ValueError):
        return None


def owner_id_for_project(pb: Any, project_id: str) -> str:
    """Billing attribution for system-triggered jobs: the project owner."""
    if not project_id:
        return ""
    members = MemberRepo(pb).list_for_project(project_id)
    owners = [m for m in members if str(m.get("role")) == "owner"]
    if owners:
        return str(owners[0].get("user") or "")
    return str(members[0].get("user") or "") if members else ""


class BillingService:
    def __init__(self, pb: Any) -> None:
        self._pb = pb
        self.products = BillingProductRepo(pb)
        self.invoices = InvoiceRepo(pb)
        self.usage = UsageRepo(pb)
        self.credits = CreditRepo(pb)
        self.events = PaymentEventRepo(pb)
        self.entitlements = EntitlementRepo(pb)
        self._integrations = IntegrationRepo(pb)

    # -- configuration -----------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(settings.billing_enabled)

    @property
    def article_cost(self) -> int:
        return max(1, int(settings.billing_article_cost))

    def is_byo(self, project_id: str) -> bool:
        """True when the project brings its own LLM keys (unmetered).

        A project with an enabled ``llm`` integration pays its provider directly,
        so generation must not consume managed-AI credits.
        """
        if not project_id:
            return False
        return self._integrations.get_active(project_id, "llm") is not None

    # -- read model --------------------------------------------------------------
    def credit_balance(self, user_id: str) -> int:
        """Managed-AI credits remaining (append-only ledger sum)."""
        return self.credits.balance(user_id)

    def current_plan(self, user_id: str) -> dict[str, Any] | None:
        return self.entitlements.current(user_id)

    def overview(self, user_id: str) -> dict[str, Any]:
        balance = self.credit_balance(user_id)
        cost = self.article_cost
        plan = self.current_plan(user_id)
        return {
            "billing_enabled": self.enabled,
            "configured": settings.payram_configured,
            "currency": settings.billing_currency,
            "article_cost": cost,
            "managed_credits": balance,
            "managed_articles_remaining": balance // cost,
            "plan": plan,
            "plan_active": plan is not None,
            "products": self.products.list_active(),
            "invoices": self.invoices.list_for_user(user_id, per_page=10),
        }

    # -- metering (managed AI only) ----------------------------------------------
    def authorize_generation(
        self,
        user_id: str,
        project_id: str,
        operation_key: str,
        *,
        job_id: str = "",
    ) -> dict[str, Any] | None:
        """Reserve one managed-AI article generation for ``operation_key``.

        Returns None (no charge, no reservation) when billing is disabled or the
        project uses its own provider keys. Raises :class:`BillingError` when the
        managed-AI counter is exhausted.
        """
        if not self.enabled:
            return None
        if self.is_byo(project_id):
            return None
        if not operation_key:
            raise BillingError("Cannot bill this operation: it has no identifier.")
        if not user_id:
            raise BillingError("Cannot bill this operation: no user is associated with it.")

        with _user_lock(user_id):
            existing = self.usage.by_operation(operation_key)
            if existing and existing.get("status") != "released":
                return existing
            if self.credit_balance(user_id) < self.article_cost:
                raise BillingError(
                    "Your managed-AI allowance is used up. Add your own provider keys on "
                    "the Connections tab, or buy more managed-AI credits on the Billing page."
                )
            data = {
                "user": user_id,
                "project": project_id or "",
                "operationKey": operation_key,
                "source": _CREDIT,
                "quantity": self.article_cost,
                "unit": UNIT,
                "status": "reserved",
                "job": job_id,
                "metadata": {"metered": "managed_ai"},
            }
            if existing:  # previously released → re-reserve in place
                record = self.usage.update(existing["id"], {**data, "releasedAt": ""})
            else:
                record = self.usage.create(data)
            self.credits.append(
                user=user_id,
                kind="spend",
                quantity=-self.article_cost,
                reason="managed-AI article",
                usage=record["id"],
                idempotency_key=f"usage:{record['id']}",
            )
            logger.info("billing.reserved", user=user_id, operation=operation_key)
            return record

    def complete(self, operation_key: str) -> None:
        if not self.enabled or not operation_key:
            return
        usage = self.usage.by_operation(operation_key)
        if usage and usage.get("status") == "reserved":
            self.usage.update(usage["id"], {"status": "completed", "completedAt": pb_dt(now_utc())})

    def release(self, operation_key: str, reason: str = "") -> None:
        """Refund a reservation (failed/cancelled generation). Idempotent."""
        if not self.enabled or not operation_key:
            return
        usage = self.usage.by_operation(operation_key)
        if not usage or usage.get("status") == "released":
            return
        with _user_lock(str(usage.get("user") or "")):
            usage = self.usage.by_operation(operation_key)
            if not usage or usage.get("status") == "released":
                return
            self.usage.update(usage["id"], {"status": "released", "releasedAt": pb_dt(now_utc())})
            if usage.get("source") == _CREDIT:
                self.credits.append(
                    user=str(usage.get("user") or ""),
                    kind="refund",
                    quantity=int(usage.get("quantity") or self.article_cost),
                    reason=reason or "generation released",
                    usage=usage["id"],
                    idempotency_key=f"refund:{usage['id']}",
                )
            logger.info("billing.released", operation=operation_key, reason=reason)

    # -- checkout -----------------------------------------------------------------
    async def create_checkout(self, user: dict[str, Any], product_code: str) -> dict[str, Any]:
        if not self.enabled:
            raise BillingError("Billing is disabled.")
        user_id = str(user.get("id") or "")
        if not user_id:
            raise BillingError("You must be signed in to purchase.")

        product = self.products.by_code(product_code.strip())
        if not product or not product.get("active"):
            raise BillingError("That plan is not available for purchase.")

        price_cents = int(product.get("priceCents") or 0)
        if price_cents <= 0:
            raise BillingError("That plan is misconfigured (no price).")
        currency = str(product.get("currency") or settings.billing_currency)
        credits = int(product.get("credits") or 0)
        plan_days = int(product.get("planDays") or 0)

        # Reuse a just-created open invoice instead of stacking duplicates
        # (double-click / retry). Never trusts a client-supplied amount.
        cooldown = max(0, int(settings.billing_checkout_cooldown_seconds))
        reuse = self._recent_open_invoice(user_id, str(product.get("code") or ""), cooldown)
        if reuse and reuse.get("checkoutUrl"):
            return reuse

        invoice_number = self._new_invoice_number()
        provider = get_payment_provider(settings.billing_provider)
        invoice = self.invoices.create(
            {
                "user": user_id,
                "product": product["id"],
                "invoiceNumber": invoice_number,
                "productCode": product.get("code") or "",
                "productName": product.get("name") or "",
                "productType": product.get("type") or "",
                "priceCents": price_cents,
                "currency": currency,
                "credits": credits,
                "planDays": plan_days,
                "status": "pending",
                "provider": provider.name,
                "providerReference": "",
                "checkoutUrl": "",
                "expectedAmountUsd": price_cents / 100.0,
                "paidAmountUsd": 0,
                "overpaidAmountUsd": 0,
                "metadata": {},
            }
        )
        logger.info(
            "billing.checkout_initiated",
            invoice=invoice["id"],
            user=user_id,
            product=product.get("code"),
        )

        try:
            payment = await provider.create_payment(
                amount_usd=price_cents / 100.0,
                customer_email=str(user.get("email") or ""),
                customer_id=user_id,
                invoice_number=invoice_number,
                description=str(product.get("name") or ""),
            )
        except (PaymentProviderError, BillingConfigError) as exc:
            # Unknown outcome → keep the invoice for review, never a fake success.
            self.invoices.update(
                invoice["id"],
                {"status": "needs_review", "error": str(exc)[:2000]},
            )
            logger.warning("billing.checkout_failed", invoice=invoice["id"], error=str(exc))
            raise BillingError(
                "We could not start the payment. Please try again in a moment."
            ) from exc

        reference = payment.reference
        if reference and self.invoices.by_provider_reference(reference):
            raise BillingError("Duplicate payment reference from the provider.")
        updated = self.invoices.update(
            invoice["id"],
            {
                "providerReference": reference,
                "checkoutUrl": payment.checkout_url,
                "status": STATUS_AWAITING,
            },
        )
        logger.info("billing.checkout_created", invoice=invoice["id"], reference=reference)
        return updated

    def _recent_open_invoice(
        self, user_id: str, product_code: str, cooldown_seconds: int
    ) -> dict[str, Any] | None:
        if cooldown_seconds <= 0:
            return None
        since = pb_dt(now_utc() - dt.timedelta(seconds=cooldown_seconds))
        return self.invoices.first(
            filter=(
                f'user="{user_id}" && productCode="{product_code}"'
                f' && (status="pending" || status="awaiting_payment") && created >= "{since}"'
            ),
            sort="-created",
        )

    @staticmethod
    def _new_invoice_number() -> str:
        return f"INV-{int(now_utc().timestamp())}-{secrets.token_hex(3).upper()}"

    # -- webhooks & reconciliation -------------------------------------------------
    async def process_webhook(self, raw_body: bytes, headers: dict[str, str]) -> str:
        """Verify, dedupe and apply a provider notification.

        Returns a short outcome string. Raises :class:`WebhookVerificationError`
        on a bad signature and :class:`PaymentProviderError` when authoritative
        verification is temporarily unavailable (caller should retry).
        """
        provider = get_payment_provider(settings.billing_provider)
        if not provider.verify_webhook(raw_body, headers):
            raise WebhookVerificationError("invalid webhook signature")

        event = provider.parse_webhook(raw_body)
        record = self.events.by_key(event.event_key)
        if record and record.get("processed"):
            return "duplicate"
        if record is None:
            record = self.events.create(
                {
                    "provider": provider.name,
                    "eventKey": event.event_key,
                    "eventType": "payment",
                    "providerStatus": event.provider_status,
                    "processed": False,
                    "receivedAt": pb_dt(now_utc()),
                    "payload": event.raw,
                }
            )

        invoice = self.invoices.by_provider_reference(event.reference)
        if invoice is None:
            self.events.update(
                record["id"],
                {
                    "processed": True,
                    "processedAt": pb_dt(now_utc()),
                    "error": "unknown provider reference",
                },
            )
            logger.warning("billing.webhook_unknown_reference", reference=event.reference)
            return "unknown"

        self.events.update(record["id"], {"invoice": invoice["id"]})

        # Trust but verify: for a payment claim, re-read the authoritative state.
        if event.status in PAID_STATUSES:
            payment = await provider.get_payment(event.reference)  # may raise → retry
            if payment.status not in PAID_STATUSES:
                self._apply_status(invoice, payment.status, payment)
                self._mark_processed(record)
                return "pending"
            paid_usd = (
                payment.filled_usd
                or event.filled_usd
                or float(invoice.get("expectedAmountUsd") or 0)
            )
            self._fulfill(invoice, paid_usd=paid_usd)
        else:
            self._apply_status(invoice, event.status, event)

        self._mark_processed(record)
        return "ok"

    def _mark_processed(self, record: dict[str, Any]) -> None:
        self.events.update(record["id"], {"processed": True, "processedAt": pb_dt(now_utc())})

    def _apply_status(self, invoice: dict[str, Any], status: str, source: Any) -> None:
        """Monotonic status update — never downgrade a paid invoice."""
        if invoice.get("status") == STATUS_PAID:
            return
        filled = getattr(source, "filled_usd", 0) or 0
        patch: dict[str, Any] = {"status": status}
        if filled:
            patch["paidAmountUsd"] = filled
        if status == STATUS_PARTIALLY_PAID:
            patch["error"] = "partially paid — awaiting the remaining amount"
        self.invoices.update(invoice["id"], patch)

    def _fulfill(self, invoice: dict[str, Any], *, paid_usd: float) -> dict[str, Any]:
        """Grant the purchased credits/plan exactly once and mark the invoice paid."""
        user_id = str(invoice.get("user") or "")
        credits = int(invoice.get("credits") or 0)
        expected = float(invoice.get("expectedAmountUsd") or 0)
        overpaid = max(0.0, float(paid_usd or 0) - expected)

        if credits > 0:
            idem = f"invoice:{invoice['id']}:credits"
            if self.credits.by_idempotency(idem) is None:
                self.credits.append(
                    user=user_id,
                    kind="grant",
                    quantity=credits,
                    reason=f"managed AI · {invoice.get('productCode') or ''}".strip(),
                    invoice=invoice["id"],
                    idempotency_key=idem,
                    metadata={"paid_usd": paid_usd, "overpaid_usd": overpaid},
                )
                logger.info(
                    "billing.credits_granted",
                    invoice=invoice["id"],
                    user=user_id,
                    credits=credits,
                )

        self._grant_entitlement(invoice)

        updated = self.invoices.update(
            invoice["id"],
            {
                "status": STATUS_PAID,
                "paidAmountUsd": float(paid_usd or 0),
                "overpaidAmountUsd": overpaid,
                "paidAt": invoice.get("paidAt") or pb_dt(now_utc()),
                "fulfilledAt": invoice.get("fulfilledAt") or pb_dt(now_utc()),
                "error": "",
            },
        )
        return updated

    def _grant_entitlement(self, invoice: dict[str, Any]) -> dict[str, Any] | None:
        """Create the prepaid plan period exactly once; renewals stack."""
        plan_days = int(invoice.get("planDays") or 0)
        if plan_days <= 0:
            return None
        user_id = str(invoice.get("user") or "")
        existing = self.entitlements.by_invoice(invoice["id"])
        if existing:
            return existing

        now = now_utc()
        start = now
        current = self.entitlements.current(user_id)
        current_expires = _parse_pb_dt((current or {}).get("expiresAt"))
        if current_expires and current_expires > now:
            start = current_expires  # prepaid renewal extends the current period
        expires = start + dt.timedelta(days=plan_days)

        entitlement = self.entitlements.create(
            {
                "user": user_id,
                "product": invoice.get("product") or "",
                "planCode": invoice.get("productCode") or "",
                "planName": invoice.get("productName") or "",
                "status": "active",
                "startsAt": pb_dt(start),
                "expiresAt": pb_dt(expires),
                "invoice": invoice["id"],
                "metadata": {"plan_days": plan_days},
            }
        )
        logger.info(
            "billing.entitlement_granted",
            invoice=invoice["id"],
            user=user_id,
            expires_at=pb_dt(expires),
        )
        return entitlement

    async def reconcile_invoice(self, invoice: dict[str, Any]) -> dict[str, Any]:
        """Fetch authoritative provider state for an unsettled invoice."""
        reference = str(invoice.get("providerReference") or "")
        if not reference:
            return invoice
        if invoice.get("status") in ("paid", "refunded", "cancelled", "failed"):
            return invoice
        provider = get_payment_provider(settings.billing_provider)
        try:
            payment = await provider.get_payment(reference)
        except PaymentProviderError as exc:
            logger.info("billing.reconcile_unavailable", invoice=invoice["id"], error=str(exc))
            return invoice
        if payment.status in PAID_STATUSES:
            return self._fulfill(
                invoice,
                paid_usd=payment.filled_usd or float(invoice.get("expectedAmountUsd") or 0),
            )
        self._apply_status(invoice, payment.status, payment)
        return self.invoices.get(invoice["id"]) or invoice
