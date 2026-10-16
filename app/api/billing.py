"""Billing — overview, credit-pack purchase, invoices, and the PayRam webhook.

Mutations are HTMX POSTs (see app/utils.py helpers). The webhook is a plain
public POST (no HX-Request), verified by signature.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from structlog import get_logger

from app.api.deps import require_hx, require_user
from app.api.errors import hx_error, page_guard
from app.providers.payments.base import BillingConfigError, PaymentProviderError
from app.services.billing import BillingError, BillingService, WebhookVerificationError
from app.templates import templates

router = APIRouter()
logger = get_logger("app.api.billing")

# Webhook bodies are small JSON documents; anything larger is rejected.
_MAX_WEBHOOK_BYTES = 64 * 1024


def _billing(request: Request) -> BillingService:
    return BillingService(request.state.pb)


def _invoice_for(request: Request, invoice_id: str) -> dict[str, Any] | None:
    """Invoice the current user owns (admins may view any)."""
    user = require_user(request)
    invoice = _billing(request).invoices.get(invoice_id)
    if not invoice:
        return None
    if str(invoice.get("user") or "") != str(user.get("id") or "") and user.get("role") != "admin":
        return None
    return invoice


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------
@router.get("/billing", response_class=HTMLResponse)
@page_guard("Something went wrong loading billing — please try again.")
def billing_overview(request: Request):
    user = require_user(request)
    service = _billing(request)
    overview = service.overview(str(user.get("id") or ""))
    return templates.TemplateResponse(
        request,
        "pages/billing/overview.html",
        {
            "title": ("Billing"),
            "overview": overview,
            "products": overview["products"],
            "invoices": overview["invoices"],
            "configured": overview["configured"],
        },
    )


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------
@router.post("/billing/checkout")
@hx_error("Could not start the payment")
async def billing_checkout(request: Request, product_code: str = Form("")):
    require_hx(request)
    user = require_user(request)
    service = _billing(request)
    try:
        invoice = await service.create_checkout(user, product_code)
    except BillingError as exc:
        # Expected, user-facing — 200 + toast so HTMX renders it cleanly.
        from app.utils import error_response

        return error_response(str(exc))

    checkout_url = str(invoice.get("checkoutUrl") or "")
    if not checkout_url.startswith(("https://", "http://")):
        from app.utils import error_response

        return error_response("The payment page could not be opened. Please try again.")
    logger.info("billing.redirecting", invoice=invoice.get("id"))
    # HX-Redirect navigates the whole browser to the trusted provider URL.
    return Response(status_code=200, headers={"HX-Redirect": checkout_url})


# ---------------------------------------------------------------------------
# Checkout return + invoice status
# ---------------------------------------------------------------------------
@router.get("/billing/checkout/return", response_class=HTMLResponse)
@page_guard("Something went wrong confirming your payment — please try again.")
async def billing_return(request: Request, invoice: str = ""):
    """Landing page after checkout. Never trusts query params for entitlement —
    the invoice is reconciled against the provider and its stored state shown."""
    user = require_user(request)
    service = _billing(request)
    record = service.invoices.by_number(invoice) if invoice else None
    if record and (
        str(record.get("user") or "") == str(user.get("id") or "") or user.get("role") == "admin"
    ):
        if record.get("status") in ("pending", "awaiting_payment", "partially_paid"):
            record = await service.reconcile_invoice(record)
    else:
        record = None
    return templates.TemplateResponse(
        request,
        "pages/billing/return.html",
        {"title": ("Payment status"), "invoice": record},
    )


@router.get("/billing/invoices/{invoice_id}", response_class=HTMLResponse)
@page_guard("Something went wrong loading the invoice — please try again.")
def invoice_detail(request: Request, invoice_id: str):
    invoice = _invoice_for(request, invoice_id)
    return templates.TemplateResponse(
        request,
        "pages/billing/invoice.html",
        {"title": ("Invoice"), "invoice": invoice},
    )


@router.post("/billing/invoices/{invoice_id}/check")
@hx_error("Could not check the payment status")
async def invoice_check(request: Request, invoice_id: str):
    require_hx(request)
    invoice = _invoice_for(request, invoice_id)
    if not invoice:
        from app.utils import error_response

        return error_response("Invoice not found")
    service = _billing(request)
    if invoice.get("status") in ("pending", "awaiting_payment", "partially_paid"):
        invoice = await service.reconcile_invoice(invoice)
    return templates.TemplateResponse(
        request, "pages/billing/_invoice_card.html", {"invoice": invoice}
    )


# ---------------------------------------------------------------------------
# Webhook (public — added to PUBLIC_PATHS in app/middleware.py)
# ---------------------------------------------------------------------------
@router.post("/webhooks/payram")
async def payram_webhook(request: Request):
    raw = await request.body()
    if len(raw) > _MAX_WEBHOOK_BYTES:
        return JSONResponse({"status": "error", "detail": "payload too large"}, status_code=413)

    headers = {k: v for k, v in request.headers.items()}
    service = BillingService(request.state.pb)
    try:
        outcome = await service.process_webhook(raw, headers)
    except WebhookVerificationError:
        logger.warning("billing.webhook_signature_failed")
        return JSONResponse({"status": "error", "detail": "invalid signature"}, status_code=401)
    except PaymentProviderError as exc:
        # Authoritative verification unavailable → ask the provider to retry.
        logger.warning("billing.webhook_retry", error=str(exc))
        return JSONResponse({"status": "retry"}, status_code=503)
    except BillingConfigError:
        logger.error("billing.webhook_unconfigured")
        return JSONResponse({"status": "error", "detail": "unconfigured"}, status_code=500)
    return JSONResponse({"status": "ok", "outcome": outcome}, status_code=200)


__all__ = ["router"]
