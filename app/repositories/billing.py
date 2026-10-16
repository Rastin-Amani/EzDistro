"""Billing repositories — PocketBase access for products, invoices, usage and the
append-only credit ledger. No business rules here (see app/services/billing.py).
"""

from __future__ import annotations

from typing import Any

from app.repositories.base import BaseRepo


class BillingProductRepo(BaseRepo):
    collection = "billing_products"

    def list_active(self) -> list[dict[str, Any]]:
        return self.list_records(filter="active=true", sort="sortOrder,priceCents", per_page=100)

    def by_code(self, code: str) -> dict[str, Any] | None:
        return self.first(filter=f'code="{code}"')


class InvoiceRepo(BaseRepo):
    collection = "billing_invoices"

    def by_number(self, invoice_number: str) -> dict[str, Any] | None:
        return self.first(filter=f'invoiceNumber="{invoice_number}"')

    def by_provider_reference(self, reference: str) -> dict[str, Any] | None:
        return self.first(filter=f'providerReference="{reference}"')

    def list_for_user(
        self, user_id: str, *, page: int = 1, per_page: int = 20
    ) -> list[dict[str, Any]]:
        return self.list_records(
            filter=f'user="{user_id}"', sort="-created", page=page, per_page=per_page
        )

    def count_for_user(self, user_id: str) -> int:
        return self.count(filter=f'user="{user_id}"')

    def has_open_for_user(self, user_id: str) -> bool:
        return (
            self.count(
                filter=(f'user="{user_id}" && (status="pending" || status="awaiting_payment")')
            )
            > 0
        )


class PaymentEventRepo(BaseRepo):
    collection = "billing_payment_events"

    def by_key(self, event_key: str) -> dict[str, Any] | None:
        return self.first(filter=f'eventKey="{event_key}"')


class UsageRepo(BaseRepo):
    collection = "billing_usage"

    def by_operation(self, operation_key: str) -> dict[str, Any] | None:
        return self.first(filter=f'operationKey="{operation_key}"')

    def count_active_for_user(self, user_id: str, *, source: str, since: str = "") -> int:
        f = f'user="{user_id}" && source="{source}" && status!="released"'
        if since:
            f += f' && created >= "{since}"'
        return self.count(filter=f)


class CreditRepo(BaseRepo):
    collection = "credit_transactions"

    def by_idempotency(self, idempotency_key: str) -> dict[str, Any] | None:
        return self.first(filter=f'idempotencyKey="{idempotency_key}"')

    def balance(self, user_id: str) -> int:
        """Sum the ledger for one user.

        ponytail: O(n) over the user's own transactions (projected to `quantity`
        only). A cached balance column is the upgrade path if a user ever
        accumulates enough transactions for this to matter.
        """
        rows = self.list_all(filter=f'user="{user_id}"', fields="quantity")
        return sum(int(r.get("quantity") or 0) for r in rows)

    def append(
        self,
        *,
        user: str,
        kind: str,
        quantity: int,
        idempotency_key: str,
        reason: str = "",
        invoice: str = "",
        usage: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing = self.by_idempotency(idempotency_key)
        if existing:
            return existing
        return self.create(
            {
                "user": user,
                "kind": kind,
                "quantity": quantity,
                "reason": reason,
                "invoice": invoice,
                "usage": usage,
                "idempotencyKey": idempotency_key,
                "metadata": metadata or {},
            }
        )
