"""Billing service — the single place that decides what a user may generate,
records usage, grants credits on confirmed payment, and reconciles provider
state.

Monetized operation: one article generation (the ``write_article`` job). A user
spends their free allowance first, then purchased credits.

Money and credits stay separate: invoices carry integer minor-unit prices;
credits are integer units; the ledger is append-only.
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
    InvoiceRepo,
    PaymentEventRepo,
    UsageRepo,
)
from app.repositories.jobs import now_utc, pb_dt
from app.repositories.members import MemberRepo

logger = get_logger(__name__)

UNIT = "article"
_FREE = "free"
_CREDIT = "credit"


class BillingError(ValueError):
    """A user-facing billing problem (quota exhausted, bad product, …)."""


class WebhookVerificationError(Exception):
    """The provider signature could not be verified — the request must be rejected."""


# Process-local per-user lock serializing reserve/release. All user-initiated
# reservations happen in the web process; the worker only ever adds credits
# back (release/grant), which cannot overspend. Cross-process concurrent
# spend of the same last credit is therefore not serialized by this lock — the
# ledger's unique idempotency keys still prevent duplicate charges.
# ponytail: process-local lock, move to a DB counter if multi-web replicas.
_locks_guard = threading.Lock()
_user_locks: dict[str, threading.Lock] = {}


def _user_lock(user_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _user_locks.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _user_locks[user_id] = lock
        return lock


def clear_billing_locks() -> None:
    """Test/rotation helper — drop cached per-user locks."""
    with _locks_guard:
        _user_locks.clear()


def _period_start() -> str:
    """PB timestamp marking the start of the current free-allowance window.

    ``none`` → lifetime allowance (returns ""). ``monthly`` → midnight UTC on the
    first of the current month. Derived, never stored, so it cannot double-grant.
    """
    if str(settings.billing_free_reset).lower() != "monthly":
        return ""
    now = now_utc()
    return pb_dt(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))


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

    # -- configuration -----------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(settings.billing_enabled)

    @property
    def free_allowance(self) -> int:
        return max(0, int(settings.billing_free_allowance))

    @property
    def article_cost(self) -> int:
        return max(1, int(settings.billing_article_cost))

    # -- read model --------------------------------------------------------------
    def free_used(self, user_id: str) -> int:
        return self.usage.count_active_for_user(user_id, source=_FREE, since=_period_start())

    def credit_balance(self, user_id: str) -> int:
        return self.credits.balance(user_id)

    def overview(self, user_id: str) -> dict[str, Any]:
        free_used = self.free_used(user_id)
        allowance = self.free_allowance
        balance = self.credit_balance(user_id)
        cost = self.article_cost
        free_remaining = max(0, allowance - free_used)
        return {
            "billing_enabled": self.enabled,
            "configured": settings.payram_configured,
            "currency": settings.billing_currency,
            "article_cost": cost,
            "free_allowance": allowance,
            "free_used": free_used,
            "free_remaining": free_remaining,
            "free_reset": settings.billing_free_reset,
            "credit_balance": balance,
            "generations_remaining": free_remaining + (balance // cost),
            "products": self.products.list_active(),
            "invoices": self.invoices.list_for_user(user_id, per_page=10),
        }

    def can_generate(self, user_id: str) -> bool:
        if not self.enabled:
            return True
        if self.free_used(user_id) < self.free_allowance:
            return True
        return self.credit_balance(user_id) >= self.article_cost

    # -- metering -----------------------------------------------------------------
    def authorize_generation(
        self,
        user_id: str,
        project_id: str,
        operation_key: str,
        *,
        job_id: str = "",
    ) -> dict[str, Any] | None:
        """Reserve one article generation for ``operation_key`` (idempotent).

        Returns the usage record, or None when billing is disabled. Raises
        :class:`BillingError` when the user has neither free allowance nor credits.
        """
        if not self.enabled:
            return None
        if not operation_key:
            raise BillingError("Cannot bill this operation: it has no identifier.")
        if not user_id:
            raise BillingError("Cannot bill this operation: no user is associated with it.")
        with _user_lock(user_id):
            existing = self.usage.by_operation(operation_key)
            if existing and existing.get("status") != "released":
                return existing
            source = _FREE
            if self.free_used(user_id) >= self.free_allowance:
                if self.credit_balance(user_id) < self.article_cost:
                    raise BillingError(
                        "You have used your free article generations and have no credits "
                        "left. Buy more to keep generating."
                    )
                source = _CREDIT
            data = {
                "user": user_id,
                "project": project_id or "",
                "operationKey": operation_key,
                "source": source,
                "quantity": self.article_cost,
                "unit": UNIT,
                "status": "reserved",
                "job": job_id,
                "metadata": {},
            }
            if existing:  # previously released → re-reserve in place
                record = self.usage.update(existing["id"], {**data, "releasedAt": ""})
            else:
                record = self.usage.create(data)
            if source == _CREDIT:
                self.credits.append(
                    user=user_id,
                    kind="spend",
                    quantity=-self.article_cost,
                    reason="article generation",
                    usage=record["id"],
                    idempotency_key=f"usage:{record['id']}",
                )
            logger.info(
                "billing.reserved",
                user=user_id,
                source=source,
                operation=operation_key,
            )
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
                "priceCents": price_cents,
                "currency": currency,
                "credits": credits,
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
        """Grant the purchased credits exactly once and mark the invoice paid."""
        user_id = str(invoice.get("user") or "")
        credits = int(invoice.get("credits") or 0)
        idem = f"invoice:{invoice['id']}:grant"
        expected = float(invoice.get("expectedAmountUsd") or 0)
        overpaid = max(0.0, float(paid_usd or 0) - expected)

        if self.credits.by_idempotency(idem) is None and credits > 0:
            self.credits.append(
                user=user_id,
                kind="grant",
                quantity=credits,
                reason=f"purchase {invoice.get('productCode') or ''}".strip(),
                invoice=invoice["id"],
                idempotency_key=idem,
                metadata={"paid_usd": paid_usd, "overpaid_usd": overpaid},
            )
            logger.info("billing.fulfilled", invoice=invoice["id"], user=user_id, credits=credits)
        elif overpaid > 0:
            logger.info("billing.overpayment", invoice=invoice["id"], overpaid=overpaid)

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
