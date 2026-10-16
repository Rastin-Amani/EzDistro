"""Billing — provider, service accounting, webhook security and routes.

All provider calls are faked; the normal suite never touches a real PayRam
instance or performs a cryptocurrency payment.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
from types import SimpleNamespace

import pytest

from app.api import billing as billing_api
from app.config import settings
from app.providers.payments.base import (
    STATUS_AWAITING,
    STATUS_PAID,
    STATUS_PARTIALLY_PAID,
    ProviderEvent,
    ProviderPayment,
)
from app.providers.payments.payram import PayRamProvider
from app.repositories.billing import BillingProductRepo, CreditRepo, InvoiceRepo
from app.services.billing import BillingError, BillingService, clear_billing_locks
from tests.fakes import FakePocketBase, default_unique_fields
from tests.helpers import make_req, make_user

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _billing_enabled(monkeypatch):
    monkeypatch.setattr(settings, "billing_enabled", True)
    monkeypatch.setattr(settings, "billing_free_allowance", 2)
    monkeypatch.setattr(settings, "billing_article_cost", 1)
    monkeypatch.setattr(settings, "billing_free_reset", "none")
    monkeypatch.setattr(settings, "billing_provider", "fake")
    monkeypatch.setattr(settings, "billing_checkout_cooldown_seconds", 0)
    clear_billing_locks()
    yield
    clear_billing_locks()


def _pb() -> FakePocketBase:
    return FakePocketBase(default_unique_fields())


def _product(pb, code="starter", credits=25, cents=900, active=True):
    return BillingProductRepo(pb).create(
        {
            "code": code,
            "name": code.title(),
            "type": "credit_pack",
            "active": active,
            "priceCents": cents,
            "currency": "USD",
            "credits": credits,
            "sortOrder": 1,
        }
    )


class FakeProvider:
    """Minimal in-process stand-in for PayRam."""

    name = "fake"
    _NORM = {
        "OPEN": STATUS_AWAITING,
        "PARTIALLY_FILLED": STATUS_PARTIALLY_PAID,
        "FILLED": STATUS_PAID,
        "OVER_FILLED": STATUS_PAID,
        "CANCELLED": "cancelled",
    }

    def __init__(self, *, get_status=STATUS_PAID, filled_usd=0.0):
        self.get_status = get_status
        self.filled_usd = filled_usd
        self.created: list[dict] = []

    async def create_payment(
        self, *, amount_usd, customer_email, customer_id, invoice_number, description=""
    ):
        self.created.append({"amount": amount_usd, "invoice": invoice_number})
        return ProviderPayment(
            reference=f"ref-{invoice_number}",
            status=STATUS_AWAITING,
            provider_status="OPEN",
            checkout_url="https://pay.example.com/payments?reference_id=ref",
            amount_usd=amount_usd,
        )

    async def get_payment(self, reference):
        return ProviderPayment(
            reference=reference,
            status=self.get_status,
            provider_status="FILLED" if self.get_status == STATUS_PAID else "OPEN",
            filled_usd=self.filled_usd,
            amount_usd=self.filled_usd,
        )

    def verify_webhook(self, raw_body, headers):
        return True

    def parse_webhook(self, raw_body):
        data = json.loads(raw_body)
        raw_status = data.get("status", "FILLED")
        return ProviderEvent(
            reference=data["reference_id"],
            status=self._NORM.get(raw_status, "failed"),
            provider_status=raw_status,
            event_key=data.get("event_key", data["reference_id"]),
            filled_usd=data.get("filled", 0.0),
            amount_usd=data.get("amount", 0.0),
        )

    def normalize_status(self, provider_status):
        return self._NORM.get(provider_status, "failed")


# ---------------------------------------------------------------------------
# Provider: signature + status mapping
# ---------------------------------------------------------------------------


def test_payram_normalizes_all_documented_states():
    provider = PayRamProvider()
    assert provider.normalize_status("OPEN") == STATUS_AWAITING
    assert provider.normalize_status("FILLED") == STATUS_PAID
    assert provider.normalize_status("OVER_FILLED") == STATUS_PAID
    assert provider.normalize_status("PARTIALLY_FILLED") == STATUS_PARTIALLY_PAID
    assert provider.normalize_status("CANCELLED") == "cancelled"
    assert provider.normalize_status("garbage") == "failed"


def test_payram_webhook_signature(monkeypatch):
    monkeypatch.setattr(settings, "payram_api_key", "secret-key")
    monkeypatch.setattr(settings, "payram_webhook_secret", "")
    provider = PayRamProvider()
    body = b'{"reference_id":"r1","status":"FILLED"}'
    good = "sha256=" + hmac.new(b"secret-key", body, hashlib.sha256).hexdigest()

    assert provider.verify_webhook(body, {"X-Payram-Signature": good})
    assert not provider.verify_webhook(body, {"X-Payram-Signature": "sha256=deadbeef"})
    assert not provider.verify_webhook(body, {})
    # Legacy API-KEY header accepted verbatim.
    assert provider.verify_webhook(body, {"API-KEY": "secret-key"})
    assert not provider.verify_webhook(body, {"API-KEY": "wrong"})


def test_payram_webhook_signature_requires_configuration(monkeypatch):
    monkeypatch.setattr(settings, "payram_api_key", "")
    monkeypatch.setattr(settings, "payram_webhook_secret", "")
    provider = PayRamProvider()
    # No secret → never verifies (fails closed, never open).
    assert not provider.verify_webhook(b"{}", {"API-KEY": ""})


def test_payram_parse_webhook_event_key_includes_confirmation_progress():
    provider = PayRamProvider()
    early = provider.parse_webhook(
        b'{"reference_id":"r1","status":"OPEN","confirmation_current":1,"confirmation_required":12}'
    )
    later = provider.parse_webhook(
        b'{"reference_id":"r1","status":"OPEN","confirmation_current":2,"confirmation_required":12}'
    )
    assert early.event_key != later.event_key
    # Exact retry collapses.
    again = provider.parse_webhook(
        b'{"reference_id":"r1","status":"OPEN","confirmation_current":2,"confirmation_required":12}'
    )
    assert again.event_key == later.event_key


# ---------------------------------------------------------------------------
# Metering: free allowance → credits, idempotency, release
# ---------------------------------------------------------------------------


def test_free_allowance_then_credits_then_blocked():
    pb = _pb()
    svc = BillingService(pb)
    # Two free generations.
    svc.authorize_generation("u1", "p1", "op1")
    svc.authorize_generation("u1", "p1", "op2")
    assert svc.free_used("u1") == 2
    # Third needs credits — none yet.
    with pytest.raises(BillingError):
        svc.authorize_generation("u1", "p1", "op3")
    # Grant 1 credit, then it is spent.
    CreditRepo(pb).append(user="u1", kind="grant", quantity=1, idempotency_key="g1")
    svc.authorize_generation("u1", "p1", "op3")
    assert svc.credit_balance("u1") == 0
    usage = svc.usage.by_operation("op3")
    assert usage["source"] == "credit"


def test_reservation_is_idempotent_by_operation_key():
    pb = _pb()
    svc = BillingService(pb)
    first = svc.authorize_generation("u1", "p1", "op1")
    again = svc.authorize_generation("u1", "p1", "op1")
    assert first["id"] == again["id"]
    assert svc.free_used("u1") == 1


def test_release_refunds_a_credit_reservation():
    pb = _pb()
    svc = BillingService(pb)
    CreditRepo(pb).append(user="u1", kind="grant", quantity=2, idempotency_key="g1")
    # burn the free allowance first
    svc.usage.create(
        {
            "user": "u1",
            "project": "p1",
            "operationKey": "f1",
            "source": "free",
            "quantity": 1,
            "unit": "article",
            "status": "completed",
        }
    )
    svc.usage.create(
        {
            "user": "u1",
            "project": "p1",
            "operationKey": "f2",
            "source": "free",
            "quantity": 1,
            "unit": "article",
            "status": "completed",
        }
    )
    svc.authorize_generation("u1", "p1", "op1")
    assert svc.credit_balance("u1") == 1
    svc.release("op1", reason="failed")
    assert svc.credit_balance("u1") == 2
    # Released usage no longer counts as consumed.
    assert svc.free_used("u1") == 2
    # Idempotent release.
    svc.release("op1")
    assert svc.credit_balance("u1") == 2


def test_concurrent_reservations_cannot_overspend_last_credit():
    pb = _pb()
    svc = BillingService(pb)
    # Disable the free tier and seed exactly one credit.
    assert settings.billing_free_allowance == 2
    CreditRepo(pb).append(user="u1", kind="grant", quantity=1, idempotency_key="g1")
    # Two "free" ops already consumed.
    for key in ("f1", "f2"):
        svc.usage.create(
            {
                "user": "u1",
                "project": "p1",
                "operationKey": key,
                "source": "free",
                "quantity": 1,
                "unit": "article",
                "status": "completed",
            }
        )

    results: list[str] = []

    def attempt(i: int) -> None:
        try:
            svc.authorize_generation("u1", "p1", f"op{i}")
            results.append("ok")
        except BillingError:
            results.append("blocked")

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["blocked", "ok"]
    assert svc.credit_balance("u1") == 0


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------


def test_checkout_creates_invoice_and_returns_checkout_url(monkeypatch):
    pb = _pb()
    _product(pb, code="starter", credits=25, cents=900)
    provider = FakeProvider()
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: provider)
    svc = BillingService(pb)
    invoice = asyncio.run(svc.create_checkout(make_user("u1"), "starter"))
    assert invoice["status"] == STATUS_AWAITING
    assert invoice["checkoutUrl"].startswith("https://")
    assert invoice["priceCents"] == 900
    assert invoice["providerReference"].startswith("ref-INV-")
    assert provider.created[0]["amount"] == 9.0


def test_checkout_rejects_inactive_or_unknown_product():
    pb = _pb()
    _product(pb, code="off", active=False)
    svc = BillingService(pb)
    with pytest.raises(BillingError):
        asyncio.run(svc.create_checkout(make_user("u1"), "off"))
    with pytest.raises(BillingError):
        asyncio.run(svc.create_checkout(make_user("u1"), "nope"))


# ---------------------------------------------------------------------------
# Webhook fulfilment & idempotency
# ---------------------------------------------------------------------------


def _seed_invoice(pb, user_id="u1", credits=25, cents=900):
    _product(pb, credits=credits, cents=cents)
    return InvoiceRepo(pb).create(
        {
            "user": user_id,
            "invoiceNumber": "INV-TEST-1",
            "productCode": "starter",
            "productName": "Starter",
            "priceCents": cents,
            "currency": "USD",
            "credits": credits,
            "status": STATUS_AWAITING,
            "provider": "fake",
            "providerReference": "ref-1",
            "expectedAmountUsd": cents / 100.0,
        }
    )


def test_webhook_fulfills_credits_exactly_once(monkeypatch):
    pb = _pb()
    invoice = _seed_invoice(pb)
    provider = FakeProvider(get_status=STATUS_PAID, filled_usd=9.0)
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: provider)
    svc = BillingService(pb)
    body = json.dumps({"reference_id": "ref-1", "status": "FILLED", "event_key": "evt-1"}).encode()

    assert asyncio.run(svc.process_webhook(body, {})) == "ok"
    assert CreditRepo(pb).balance("u1") == 25
    assert InvoiceRepo(pb).get(invoice["id"])["status"] == STATUS_PAID

    # Same event again → duplicate, no double grant.
    assert asyncio.run(svc.process_webhook(body, {})) == "duplicate"
    assert CreditRepo(pb).balance("u1") == 25


def test_webhook_partial_payment_grants_nothing(monkeypatch):
    pb = _pb()
    invoice = _seed_invoice(pb)
    provider = FakeProvider(get_status=STATUS_PARTIALLY_PAID, filled_usd=3.0)
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: provider)
    svc = BillingService(pb)
    body = json.dumps(
        {"reference_id": "ref-1", "status": "PARTIALLY_FILLED", "event_key": "evt-p"}
    ).encode()
    assert asyncio.run(svc.process_webhook(body, {})) == "ok"
    assert CreditRepo(pb).balance("u1") == 0
    assert InvoiceRepo(pb).get(invoice["id"])["status"] == STATUS_PARTIALLY_PAID


def test_webhook_overpayment_grants_only_invoice_credits(monkeypatch):
    pb = _pb()
    invoice = _seed_invoice(pb, credits=25, cents=900)
    provider = FakeProvider(get_status=STATUS_PAID, filled_usd=50.0)
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: provider)
    svc = BillingService(pb)
    body = json.dumps(
        {"reference_id": "ref-1", "status": "OVER_FILLED", "event_key": "evt-o"}
    ).encode()
    assert asyncio.run(svc.process_webhook(body, {})) == "ok"
    assert CreditRepo(pb).balance("u1") == 25
    stored = InvoiceRepo(pb).get(invoice["id"])
    assert stored["overpaidAmountUsd"] == pytest.approx(41.0)


def test_webhook_unknown_reference_is_acknowledged_without_grant(monkeypatch):
    pb = _pb()
    provider = FakeProvider()
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: provider)
    svc = BillingService(pb)
    body = json.dumps({"reference_id": "nope", "status": "FILLED"}).encode()
    assert asyncio.run(svc.process_webhook(body, {})) == "unknown"
    assert CreditRepo(pb).balance("u1") == 0


def test_reconcile_recovers_after_missed_webhook(monkeypatch):
    pb = _pb()
    invoice = _seed_invoice(pb)
    provider = FakeProvider(get_status=STATUS_PAID, filled_usd=9.0)
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: provider)
    svc = BillingService(pb)
    reconciled = asyncio.run(svc.reconcile_invoice(invoice))
    assert reconciled["status"] == STATUS_PAID
    assert CreditRepo(pb).balance("u1") == 25
    # Reconciling again does not re-grant.
    asyncio.run(svc.reconcile_invoice(reconciled))
    assert CreditRepo(pb).balance("u1") == 25


# ---------------------------------------------------------------------------
# Routes & webhook security
# ---------------------------------------------------------------------------


def test_overview_route_renders_real_balances():
    pb = _pb()
    _product(pb)
    CreditRepo(pb).append(user="u1", kind="grant", quantity=5, idempotency_key="g1")
    req = make_req(pb, make_user("u1"), "p1")
    resp = billing_api.billing_overview(req)
    body = resp.body.decode()
    assert resp.status_code == 200
    assert "Billing" in body
    assert "Starter" in body


def test_checkout_route_redirects_to_provider(monkeypatch):
    pb = _pb()
    _product(pb, code="starter")
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: FakeProvider())
    req = make_req(pb, make_user("u1"), "p1")
    resp = asyncio.run(billing_api.billing_checkout(req, product_code="starter"))
    assert resp.headers.get("HX-Redirect", "").startswith("https://")


def test_checkout_route_rejects_unknown_product(monkeypatch):
    pb = _pb()
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: FakeProvider())
    req = make_req(pb, make_user("u1"), "p1")
    resp = asyncio.run(billing_api.billing_checkout(req, product_code="ghost"))
    events = json.loads(resp.headers["HX-Trigger"])
    assert events["show-toast"]["type"] == "error"


def test_invoice_detail_is_owner_scoped():
    pb = _pb()
    owner = make_user("u1")
    other = make_user("u2")
    invoice = _seed_invoice(pb, user_id="u1")
    # Owner sees it.
    req = make_req(pb, owner, "p1")
    assert billing_api.invoice_detail(req, invoice["id"]).status_code == 200
    # Another user gets a "not found" render (invoice is None).
    req2 = make_req(pb, other, "p1")
    body = billing_api.invoice_detail(req2, invoice["id"]).body.decode()
    assert "Invoice not found" in body


def test_webhook_route_rejects_bad_signature(monkeypatch):
    pb = _pb()
    monkeypatch.setattr(settings, "billing_provider", "payram")
    monkeypatch.setattr(settings, "payram_api_key", "k")
    monkeypatch.setattr(settings, "payram_webhook_secret", "")
    raw = b'{"reference_id":"r1","status":"FILLED"}'

    class _Req:
        state = SimpleNamespace(pb=pb)
        headers: dict = {"X-Payram-Signature": "sha256=bad"}

        async def body(self):
            return raw

    resp = asyncio.run(billing_api.payram_webhook(_Req()))
    assert resp.status_code == 401


def test_webhook_route_accepts_valid_signature_and_fulfills(monkeypatch):
    pb = _pb()
    _seed_invoice(pb)
    monkeypatch.setattr(settings, "billing_provider", "payram")
    monkeypatch.setattr(settings, "payram_api_key", "k")
    monkeypatch.setattr(settings, "payram_webhook_secret", "")

    async def _fake_get(self, reference):
        return ProviderPayment(
            reference=reference,
            status=STATUS_PAID,
            provider_status="FILLED",
            filled_usd=9.0,
            amount_usd=9.0,
        )

    monkeypatch.setattr(PayRamProvider, "get_payment", _fake_get)
    raw = json.dumps({"reference_id": "ref-1", "status": "FILLED"}).encode()
    sig = "sha256=" + hmac.new(b"k", raw, hashlib.sha256).hexdigest()

    class _Req:
        state = SimpleNamespace(pb=pb)
        headers = {"X-Payram-Signature": sig}

        async def body(self):
            return raw

    resp = asyncio.run(billing_api.payram_webhook(_Req()))
    assert resp.status_code == 200
    assert CreditRepo(pb).balance("u1") == 25


def test_checkout_return_does_not_grant_from_query_params(monkeypatch):
    pb = _pb()
    invoice = _seed_invoice(pb)
    # Provider still says the payment is open → nothing is granted.
    provider = FakeProvider(get_status=STATUS_AWAITING, filled_usd=0.0)
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: provider)
    req = make_req(pb, make_user("u1"), "p1")
    resp = asyncio.run(billing_api.billing_return(req, invoice=invoice["invoiceNumber"]))
    assert resp.status_code == 200
    assert CreditRepo(pb).balance("u1") == 0
    assert "Waiting for confirmation" in resp.body.decode()


# ---------------------------------------------------------------------------
# Enforcement through the write routes (billing enabled)
# ---------------------------------------------------------------------------


def _project_with_owner(pb, user_id="u1"):
    from tests.helpers import make_member, make_project

    project = make_project(pb, slug="proj-a", name="A")
    make_member(pb, project["id"], user_id, role="owner")
    return project


def test_write_topic_reserves_and_annotates_job():
    from app.api import projects as P
    from tests.helpers import make_topic

    pb = _pb()
    project = _project_with_owner(pb)
    topic = make_topic(pb, project["id"])
    req = make_req(pb, make_user("u1"), project["id"])
    resp = P.write_topic(req, project["id"], topic["id"])

    assert "started" in json.loads(resp.headers["HX-Trigger"])["show-toast"]["message"]
    job = pb.collection("jobs").get_full_list()[0]
    assert job["payload"]["billingKey"] == f"write:article:{topic['id']}"
    assert job["payload"]["billingUserId"] == "u1"
    usage = BillingService(pb).usage.by_operation(f"write:article:{topic['id']}")
    assert usage and usage["status"] == "reserved"


def test_write_topic_paywall_when_out_of_allowance(monkeypatch):
    from app.api import projects as P
    from tests.helpers import make_topic

    monkeypatch.setattr(settings, "billing_free_allowance", 0)
    pb = _pb()
    project = _project_with_owner(pb)
    topic = make_topic(pb, project["id"])
    req = make_req(pb, make_user("u1"), project["id"])
    resp = P.write_topic(req, project["id"], topic["id"])

    events = json.loads(resp.headers["HX-Trigger"])
    assert events["show-toast"]["type"] == "error"
    # No job was queued.
    assert pb.collection("jobs").get_full_list() == []


def test_cancel_topic_releases_the_reservation():
    from app.api import projects as P
    from tests.helpers import make_topic

    pb = _pb()
    project = _project_with_owner(pb)
    topic = make_topic(pb, project["id"])
    req = make_req(pb, make_user("u1"), project["id"])
    P.write_topic(req, project["id"], topic["id"])
    svc = BillingService(pb)
    assert svc.free_used("u1") == 1

    P.cancel_topic(req, project["id"], topic["id"])
    usage = svc.usage.by_operation(f"write:article:{topic['id']}")
    assert usage["status"] == "released"
    assert svc.free_used("u1") == 0


def test_overview_renders_disabled_state(monkeypatch):
    monkeypatch.setattr(settings, "billing_enabled", False)
    pb = _pb()
    req = make_req(pb, make_user("u1"), "p1")
    resp = billing_api.billing_overview(req)
    body = resp.body.decode()
    assert resp.status_code == 200
    assert "Billing is turned off" in body


def test_return_page_renders_when_invoice_unknown():
    pb = _pb()
    req = make_req(pb, make_user("u1"), "p1")
    resp = asyncio.run(billing_api.billing_return(req, invoice="INV-NOPE"))
    body = resp.body.decode()
    assert resp.status_code == 200
    assert "could not find that payment" in body
