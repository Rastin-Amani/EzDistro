"""Payment-provider abstraction.

Billing business logic (credits, entitlements, invoices) depends ONLY on this
interface; the concrete PayRam adapter lives in ``payram.py``. Adding Stripe or
any other provider means implementing ``PaymentProvider`` and registering it in
``get_payment_provider`` — never touching the ledger, usage metering, or UI.

Normalized payment states map onto the app's invoice statuses; provider-native
statuses are preserved for observability and debugging.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


class PaymentProviderError(Exception):
    """An external payment-provider failure.

    ``retryable`` tells the caller whether repeating the same call is safe.
    """

    def __init__(
        self, message: str, *, retryable: bool = False, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.details = details or {}


class BillingConfigError(RuntimeError):
    """Billing/provider configuration is missing or invalid (operator action)."""


# Normalized payment states used across the app.
STATUS_AWAITING = "awaiting_payment"
STATUS_PAID = "paid"
STATUS_PARTIALLY_PAID = "partially_paid"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"

TERMINAL_STATUSES = frozenset({STATUS_PAID, STATUS_PARTIALLY_PAID, STATUS_CANCELLED, STATUS_FAILED})
PAID_STATUSES = frozenset({STATUS_PAID})


@dataclass
class ProviderPayment:
    """A checkout creation or a fetched payment, normalized."""

    reference: str
    status: str
    provider_status: str
    checkout_url: str = ""
    amount_usd: float = 0.0
    filled_usd: float = 0.0
    currency: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderEvent:
    """A verified webhook notification, normalized."""

    reference: str
    status: str
    provider_status: str
    event_key: str
    amount_usd: float = 0.0
    filled_usd: float = 0.0
    currency: str = ""
    occurred_at: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class PaymentProvider(Protocol):
    """Everything the billing service may ask of a provider."""

    name: str

    async def create_payment(
        self,
        *,
        amount_usd: float,
        customer_email: str,
        customer_id: str,
        invoice_number: str,
        description: str = "",
    ) -> ProviderPayment: ...

    async def get_payment(self, reference: str) -> ProviderPayment: ...

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> bool: ...

    def parse_webhook(self, raw_body: bytes) -> ProviderEvent: ...

    def normalize_status(self, provider_status: str) -> str: ...


def get_payment_provider(name: str) -> PaymentProvider:
    """Resolve a provider adapter by name. Only ``payram`` ships today."""
    normalized = (name or "").strip().lower()
    if normalized in ("", "payram"):
        from app.providers.payments.payram import PayRamProvider

        return PayRamProvider()
    raise BillingConfigError(f"Unknown payment provider: {name!r}")
