"""PayRam payment adapter (REST over httpx — no new dependency).

Verified against https://docs.payram.com/api-integration/payments-api:
  * create:  POST {BASE_URL}/api/v1/payment          header API-Key
  * status:  GET  {BASE_URL}/api/v1/payment/reference/{reference_id}
  * webhook: POST to our endpoint, header X-Payram-Signature =
             ``sha256=hex(HMAC_SHA256(raw_body, api_key))`` (legacy: API-KEY).

PayRam states: OPEN | PARTIALLY_FILLED | FILLED | OVER_FILLED | CANCELLED.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

import httpx

from app.config import settings
from app.providers.payments.base import (
    STATUS_AWAITING,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_PAID,
    STATUS_PARTIALLY_PAID,
    BillingConfigError,
    PaymentProviderError,
    ProviderEvent,
    ProviderPayment,
)

# PayRam-native → normalized.
_STATUS_MAP = {
    "OPEN": STATUS_AWAITING,
    "PARTIALLY_FILLED": STATUS_PARTIALLY_PAID,
    "FILLED": STATUS_PAID,
    "OVER_FILLED": STATUS_PAID,
    "CANCELLED": STATUS_CANCELLED,
}


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class PayRamProvider:
    name = "payram"

    def __init__(self) -> None:
        self._base_url = (settings.payram_base_url or "").rstrip("/")
        self._api_key = settings.payram_api_key or ""
        self._webhook_secret = settings.billing_webhook_secret or ""
        self._timeout = float(settings.payram_timeout_seconds or 20.0)

    # -- configuration -----------------------------------------------------------
    def _require_config(self) -> None:
        if not self._base_url or not self._api_key:
            raise BillingConfigError(
                "PayRam is not configured. Set PAYRAM_BASE_URL and PAYRAM_API_KEY."
            )

    @property
    def _headers(self) -> dict[str, str]:
        return {"API-Key": self._api_key, "Content-Type": "application/json"}

    # -- checkout -----------------------------------------------------------------
    async def create_payment(
        self,
        *,
        amount_usd: float,
        customer_email: str,
        customer_id: str,
        invoice_number: str,
        description: str = "",
    ) -> ProviderPayment:
        self._require_config()
        body = {
            "customerEmail": customer_email,
            "customerID": customer_id,
            "amountInUSD": amount_usd,
            "invoiceID": invoice_number,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/api/v1/payment", headers=self._headers, json=body
                )
        except httpx.HTTPError as exc:
            # The request may or may not have reached PayRam — do NOT retry blindly.
            raise PaymentProviderError(f"could not reach PayRam: {exc}", retryable=False) from exc
        if response.status_code >= 400:
            raise PaymentProviderError(
                f"PayRam rejected the checkout (HTTP {response.status_code})",
                retryable=response.status_code >= 500,
                details={"status": response.status_code},
            )
        data = self._json(response)
        reference = str(data.get("reference_id") or data.get("referenceID") or "")
        url = str(data.get("url") or "")
        if not reference or not url:
            raise PaymentProviderError(
                "PayRam returned no reference or checkout URL", details={"keys": list(data)}
            )
        return ProviderPayment(
            reference=reference,
            status=STATUS_AWAITING,
            provider_status="OPEN",
            checkout_url=url,
            amount_usd=amount_usd,
            currency="USD",
            raw={"host": data.get("host")},
        )

    # -- status -------------------------------------------------------------------
    async def get_payment(self, reference: str) -> ProviderPayment:
        self._require_config()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    f"{self._base_url}/api/v1/payment/reference/{reference}",
                    headers=self._headers,
                )
        except httpx.HTTPError as exc:
            raise PaymentProviderError(f"could not reach PayRam: {exc}", retryable=True) from exc
        if response.status_code == 404:
            raise PaymentProviderError(
                "payment reference not found at PayRam",
                retryable=False,
                details={"status": 404},
            )
        if response.status_code >= 400:
            raise PaymentProviderError(
                f"PayRam status lookup failed (HTTP {response.status_code})",
                retryable=response.status_code >= 500,
            )
        data = self._json(response)
        provider_status = str(data.get("paymentState") or data.get("status") or "")
        return ProviderPayment(
            reference=reference,
            status=self.normalize_status(provider_status),
            provider_status=provider_status,
            amount_usd=_as_float(data.get("amountInUSD") or data.get("amount")),
            filled_usd=_as_float(data.get("filledAmountInUsd") or data.get("filled_amount_in_usd")),
            currency=str(data.get("currency") or "USD"),
            raw={},
        )

    # -- webhook ------------------------------------------------------------------
    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        """Constant-time verification of the PayRam signature.

        Preferred: ``X-Payram-Signature: sha256=<hex>`` where the HMAC-SHA256 is
        computed over the EXACT raw request body keyed with the project API key
        (or a dedicated webhook secret). Legacy fallback: ``API-KEY`` sent
        verbatim. Never disabled.
        """
        secret = self._webhook_secret
        if not secret:
            return False
        lowered = {str(k).lower(): v for k, v in headers.items()}
        signature = lowered.get("x-payram-signature")
        if signature:
            expected = (
                "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
            )
            return hmac.compare_digest(str(signature), expected)
        provided = lowered.get("api-key")
        if provided:
            return hmac.compare_digest(str(provided), secret)
        return False

    def parse_webhook(self, raw_body: bytes) -> ProviderEvent:
        try:
            data = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise PaymentProviderError("malformed webhook payload") from exc
        if not isinstance(data, dict):
            raise PaymentProviderError("webhook payload is not an object")
        reference = str(data.get("reference_id") or "")
        provider_status = str(data.get("status") or "")
        if not reference or not provider_status:
            raise PaymentProviderError("webhook is missing reference_id or status")
        # Idempotency key: reference + status. Confirmation-progress spam carries
        # additional counters, so fold those in too to keep every distinct event
        # unique while collapsing exact retries.
        event_key = "|".join(
            str(part)
            for part in (
                reference,
                provider_status,
                data.get("confirmation_current", ""),
                data.get("confirmation_required", ""),
            )
        )
        return ProviderEvent(
            reference=reference,
            status=self.normalize_status(provider_status),
            provider_status=provider_status,
            event_key=event_key,
            amount_usd=_as_float(data.get("amount")),
            filled_usd=_as_float(data.get("filled_amount_in_usd")),
            currency=str(data.get("currency") or ""),
            occurred_at=int(data.get("timestamp") or 0),
            raw={
                "invoice_id": data.get("invoice_id"),
                "customer_id": data.get("customer_id"),
            },
        )

    def normalize_status(self, provider_status: str) -> str:
        return _STATUS_MAP.get(str(provider_status or "").upper(), STATUS_FAILED)

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise PaymentProviderError("PayRam returned a non-JSON response") from exc
        return data if isinstance(data, dict) else {}
