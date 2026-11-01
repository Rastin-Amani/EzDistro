"""Self-hosted checkout — our page, PayRam behind our server.

The browser only ever talks to these routes. ``PAYRAM_API_KEY`` stays in
``app/providers/payments/payram.py`` and is never rendered, returned or logged.
The PayRam payload carries internal ids, so the status route deliberately
passes through only ``amountInUSD``, ``paymentState`` and ``expiresAt``.

Access is owner-scoped: a reference is unguessable, but a stranger must not be
able to read someone's order even if it leaks.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from structlog import get_logger

from app.api.deps import current_user, require_user
from app.config import settings
from app.providers.payments.base import BillingConfigError, PaymentProviderError
from app.services.billing import BillingError, BillingService, CheckoutNotFound
from app.templates import templates

router = APIRouter()
logger = get_logger("app.api.checkout")


class CheckoutRequest(BaseModel):
    """Body of ``POST /api/checkout`` (camelCase, as PayRam names them).

    ``customerEmail``/``customerID`` are accepted for API compatibility but the
    signed-in user is authoritative; a client cannot create an order for
    somebody else.
    """

    amountInUSD: float = 0
    customerEmail: str = ""
    customerID: str = ""
    invoiceID: str = ""
    productCode: str = ""
    # Optional restriction. A pinned pair means no card on-ramp (it settles on a
    # chain of the provider's choosing).
    currency: str = ""
    network: str = ""


def _service(request: Request) -> BillingService:
    return BillingService(request.state.pb)


def _owned_invoice(request: Request, reference: str) -> dict[str, Any]:
    """The caller's order for a reference — 404 for anything else."""
    user = require_user(request)
    invoice = _service(request).invoice_for_reference(reference)
    if str(invoice.get("user") or "") != str(user.get("id") or "") and user.get("role") != "admin":
        raise CheckoutNotFound(reference)
    return invoice


def _provider_failure(exc: PaymentProviderError) -> JSONResponse:
    if exc.details.get("status") == 404:
        return JSONResponse({"error": "Payment not found"}, status_code=404)
    logger.warning("checkout.provider_error", error=str(exc))
    return JSONResponse({"error": "Could not reach the payment provider"}, status_code=502)


def _checkout_url(reference: str) -> str:
    return f"{settings.public_app_url.rstrip('/')}/checkout/{reference}"


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
@router.post("/api/checkout")
async def create_checkout(request: Request, payload: CheckoutRequest) -> JSONResponse:
    user = current_user(request)
    if user is None:
        return JSONResponse({"error": "Sign in to start a payment"}, status_code=401)
    service = _service(request)
    try:
        if payload.productCode.strip():
            # Catalog purchases never trust a client-supplied price.
            invoice = await service.create_checkout(user, payload.productCode)
        else:
            invoice = await service.create_order(
                user,
                amount_usd=payload.amountInUSD,
                invoice_number=payload.invoiceID,
                currency=payload.currency,
                network=payload.network,
            )
    except BillingError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except BillingConfigError:
        return JSONResponse({"error": "Payments are not configured"}, status_code=503)

    reference = str(invoice.get("providerReference") or "")
    if not reference:
        return JSONResponse({"error": "The payment could not be created"}, status_code=502)
    logger.info("checkout.created", invoice=invoice.get("id"), reference=reference)
    return JSONResponse(
        {
            "referenceID": reference,
            "invoiceID": invoice.get("invoiceNumber") or "",
            "url": _checkout_url(reference),
        }
    )


# ---------------------------------------------------------------------------
# Read (three fields only)
# ---------------------------------------------------------------------------
@router.get("/api/checkout/{reference}")
async def checkout_status(request: Request, reference: str) -> JSONResponse:
    try:
        _owned_invoice(request, reference)
        status = await _service(request).checkout_status(reference)
    except CheckoutNotFound:
        return JSONResponse({"error": "Payment not found"}, status_code=404)
    except PaymentProviderError as exc:
        return _provider_failure(exc)
    except BillingConfigError:
        return JSONResponse({"error": "Payments are not configured"}, status_code=503)
    return JSONResponse(status)


@router.get("/api/checkout/{reference}/options")
async def checkout_options(request: Request, reference: str) -> JSONResponse:
    try:
        _owned_invoice(request, reference)
        options = await _service(request).deposit_options(reference)
    except CheckoutNotFound:
        return JSONResponse({"error": "Payment not found"}, status_code=404)
    except PaymentProviderError as exc:
        return _provider_failure(exc)
    except BillingConfigError:
        return JSONResponse({"error": "Payments are not configured"}, status_code=503)
    return JSONResponse(options)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def _render_checkout(request: Request, reference: str, *, mode: str):
    """Owner-scoped page. ``mode`` is ``checkout`` or the card ``return`` path."""
    try:
        invoice = _owned_invoice(request, reference)
    except CheckoutNotFound:
        return templates.TemplateResponse(
            request,
            "pages/checkout/not_found.html",
            {"title": ("Checkout not found")},
            status_code=404,
        )
    service = _service(request)
    card_available = mode == "checkout" and service.card_onramp_available(invoice)
    return templates.TemplateResponse(
        request,
        "pages/checkout/checkout.html",
        {
            "title": ("Payment status" if mode == "return" else "Checkout"),
            "mode": mode,
            "reference": reference,
            "invoice": invoice,
            "amount_usd": float(invoice.get("expectedAmountUsd") or 0),
            "expires_at": str(invoice.get("expiresAt") or ""),
            "initial_state": str(invoice.get("status") or ""),
            "card_available": card_available,
            "card_url": service.hosted_checkout_url(invoice) if card_available else "",
            "support_url": settings.support_url,
        },
    )


@router.get("/checkout/{reference}", response_class=HTMLResponse)
def checkout_page(request: Request, reference: str):
    return _render_checkout(request, reference, mode="checkout")


@router.get("/checkout/{reference}/return", response_class=HTMLResponse)
def checkout_return(request: Request, reference: str):
    """Landing page after PayRam's hosted card flow.

    The query string is ignored: it only says the browser came back. The page
    reads authoritative payment status on mount; the webhook still owns
    fulfilment, and visiting a URL never grants anything.
    """
    return _render_checkout(request, reference, mode="return")


__all__ = ["router"]
