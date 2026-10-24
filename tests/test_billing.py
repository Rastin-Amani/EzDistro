"""Billing — plans, BYO-unmetered managed-AI metering, webhooks and routes.

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
from tests.helpers import make_integration, make_member, make_project, make_req, make_user

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _billing_on(monkeypatch):
    monkeypatch.setattr(settings, "billing_enabled", True)
    monkeypatch.setattr(settings, "billing_article_cost", 1)
    monkeypatch.setattr(settings, "billing_provider", "fake")
    monkeypatch.setattr(settings, "billing_checkout_cooldown_seconds", 0)
    clear_billing_locks()
    yield
    clear_billing_locks()


def _pb() -> FakePocketBase:
    return FakePocketBase(default_unique_fields())


def _product(
    pb,
    code="cloud",
    name="Cloud",
    ptype="plan",
    credits=0,
    days=30,
    cents=4900,
    active=True,
):
    return BillingProductRepo(pb).create(
        {
            "code": code,
            "name": name,
            "type": ptype,
            "active": active,
            "priceCents": cents,
            "currency": "USD",
            "credits": credits,
            "planDays": days,
            "features": [],
            "sortOrder": 1,
        }
    )


def _project(pb, *, byo: bool, user_id: str = "u1"):
    """A project owned by ``user_id``. ``byo`` adds an enabled LLM integration."""
    project = make_project(pb, slug="proj-a", name="A")
    make_member(pb, project["id"], user_id, role="owner")
    if byo:
        make_integration(pb, project["id"])  # category=llm, enabled=true
    return project


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


def _seed_invoice(
    pb,
    *,
    user_id="u1",
    number="INV-TEST-1",
    reference="ref-1",
    credits=30,
    days=30,
    cents=9900,
    code="cloud_ai",
    name="Cloud + Managed AI",
):
    if BillingProductRepo(pb).by_code(code) is None:
        _product(pb, code=code, name=name, credits=credits, days=days, cents=cents)
    return InvoiceRepo(pb).create(
        {
            "user": user_id,
            "invoiceNumber": number,
            "productCode": code,
            "productName": name,
            "productType": "plan",
            "priceCents": cents,
            "currency": "USD",
            "credits": credits,
            "planDays": days,
            "status": STATUS_AWAITING,
            "provider": "fake",
            "providerReference": reference,
            "expectedAmountUsd": cents / 100.0,
        }
    )


def _webhook(body: dict):
    return json.dumps(body).encode()


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
    assert provider.verify_webhook(body, {"API-KEY": "secret-key"})
    assert not provider.verify_webhook(body, {"API-KEY": "wrong"})


def test_payram_webhook_signature_requires_configuration(monkeypatch):
    monkeypatch.setattr(settings, "payram_api_key", "")
    monkeypatch.setattr(settings, "payram_webhook_secret", "")
    assert not PayRamProvider().verify_webhook(b"{}", {"API-KEY": ""})


def test_payram_parse_webhook_event_key_includes_confirmation_progress():
    provider = PayRamProvider()
    early = provider.parse_webhook(
        b'{"reference_id":"r1","status":"OPEN","confirmation_current":1,"confirmation_required":12}'
    )
    later = provider.parse_webhook(
        b'{"reference_id":"r1","status":"OPEN","confirmation_current":2,"confirmation_required":12}'
    )
    assert early.event_key != later.event_key
    again = provider.parse_webhook(
        b'{"reference_id":"r1","status":"OPEN","confirmation_current":2,"confirmation_required":12}'
    )
    assert again.event_key == later.event_key


# ---------------------------------------------------------------------------
# Metering: BYO unmetered, managed-AI only
# ---------------------------------------------------------------------------


def test_byo_project_is_unmetered():
    pb = _pb()
    project = _project(pb, byo=True)
    svc = BillingService(pb)
    assert svc.is_byo(project["id"]) is True
    assert svc.authorize_generation("u1", project["id"], "op1") is None
    assert svc.usage.by_operation("op1") is None
    assert svc.credit_balance("u1") == 0


def test_managed_project_consumes_credits_and_blocks_at_zero():
    pb = _pb()
    project = _project(pb, byo=False)
    svc = BillingService(pb)
    assert svc.is_byo(project["id"]) is False
    with pytest.raises(BillingError):
        svc.authorize_generation("u1", project["id"], "op0")

    CreditRepo(pb).append(user="u1", kind="grant", quantity=2, idempotency_key="g1")
    usage = svc.authorize_generation("u1", project["id"], "op1")
    assert usage["source"] == "credit"
    assert svc.credit_balance("u1") == 1
    svc.authorize_generation("u1", project["id"], "op2")
    assert svc.credit_balance("u1") == 0
    with pytest.raises(BillingError):
        svc.authorize_generation("u1", project["id"], "op3")


def test_reservation_is_idempotent_by_operation_key():
    pb = _pb()
    project = _project(pb, byo=False)
    CreditRepo(pb).append(user="u1", kind="grant", quantity=5, idempotency_key="g1")
    svc = BillingService(pb)
    first = svc.authorize_generation("u1", project["id"], "op1")
    again = svc.authorize_generation("u1", project["id"], "op1")
    assert first["id"] == again["id"]
    assert svc.credit_balance("u1") == 4


def test_release_refunds_a_managed_reservation():
    pb = _pb()
    project = _project(pb, byo=False)
    CreditRepo(pb).append(user="u1", kind="grant", quantity=2, idempotency_key="g1")
    svc = BillingService(pb)
    svc.authorize_generation("u1", project["id"], "op1")
    assert svc.credit_balance("u1") == 1
    svc.release("op1", reason="failed")
    assert svc.credit_balance("u1") == 2
    svc.release("op1")  # idempotent
    assert svc.credit_balance("u1") == 2


def test_concurrent_reservations_cannot_overspend_last_credit():
    pb = _pb()
    project = _project(pb, byo=False)
    CreditRepo(pb).append(user="u1", kind="grant", quantity=1, idempotency_key="g1")
    svc = BillingService(pb)
    results: list[str] = []

    def attempt(i: int) -> None:
        try:
            svc.authorize_generation("u1", project["id"], f"op{i}")
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


def test_checkout_creates_invoice_with_plan_snapshot(monkeypatch):
    pb = _pb()
    _product(pb, code="cloud_ai", name="Cloud + Managed AI", credits=30, days=30, cents=9900)
    provider = FakeProvider()
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: provider)
    svc = BillingService(pb)
    invoice = asyncio.run(svc.create_checkout(make_user("u1"), "cloud_ai"))
    assert invoice["status"] == STATUS_AWAITING
    assert invoice["checkoutUrl"].startswith("https://")
    assert invoice["priceCents"] == 9900
    assert invoice["planDays"] == 30
    assert invoice["credits"] == 30
    assert invoice["productType"] == "plan"
    assert provider.created[0]["amount"] == 99.0


def test_checkout_rejects_inactive_or_unknown_product():
    pb = _pb()
    _product(pb, code="off", active=False)
    svc = BillingService(pb)
    with pytest.raises(BillingError):
        asyncio.run(svc.create_checkout(make_user("u1"), "off"))
    with pytest.raises(BillingError):
        asyncio.run(svc.create_checkout(make_user("u1"), "nope"))


# ---------------------------------------------------------------------------
# Webhook fulfilment: plan entitlement + managed-AI credits
# ---------------------------------------------------------------------------


def test_webhook_activates_plan_and_grants_credits_exactly_once(monkeypatch):
    pb = _pb()
    invoice = _seed_invoice(pb)
    provider = FakeProvider(get_status=STATUS_PAID, filled_usd=99.0)
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: provider)
    svc = BillingService(pb)
    body = _webhook({"reference_id": "ref-1", "status": "FILLED", "event_key": "evt-1"})

    assert asyncio.run(svc.process_webhook(body, {})) == "ok"
    assert CreditRepo(pb).balance("u1") == 30
    plan = svc.current_plan("u1")
    assert plan and plan["planName"] == "Cloud + Managed AI"
    assert InvoiceRepo(pb).get(invoice["id"])["status"] == STATUS_PAID

    # Same event again → duplicate, no double grant.
    assert asyncio.run(svc.process_webhook(body, {})) == "duplicate"
    assert CreditRepo(pb).balance("u1") == 30
    assert len(pb.collection("billing_entitlements").get_full_list()) == 1


def test_renewal_extends_from_current_expiry(monkeypatch):
    pb = _pb()
    _seed_invoice(pb, number="INV-1", reference="ref-1")
    _seed_invoice(pb, number="INV-2", reference="ref-2")
    provider = FakeProvider(get_status=STATUS_PAID, filled_usd=99.0)
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: provider)
    svc = BillingService(pb)

    asyncio.run(
        svc.process_webhook(
            _webhook({"reference_id": "ref-1", "status": "FILLED", "event_key": "e1"}), {}
        )
    )
    first_expiry = svc.current_plan("u1")["expiresAt"]
    asyncio.run(
        svc.process_webhook(
            _webhook({"reference_id": "ref-2", "status": "FILLED", "event_key": "e2"}), {}
        )
    )
    second_expiry = svc.current_plan("u1")["expiresAt"]
    assert second_expiry > first_expiry
    assert len(pb.collection("billing_entitlements").get_full_list()) == 2


def test_webhook_partial_payment_grants_nothing(monkeypatch):
    pb = _pb()
    invoice = _seed_invoice(pb)
    provider = FakeProvider(get_status=STATUS_PARTIALLY_PAID, filled_usd=30.0)
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: provider)
    svc = BillingService(pb)
    body = _webhook({"reference_id": "ref-1", "status": "PARTIALLY_FILLED", "event_key": "p1"})
    assert asyncio.run(svc.process_webhook(body, {})) == "ok"
    assert CreditRepo(pb).balance("u1") == 0
    assert svc.current_plan("u1") is None
    assert InvoiceRepo(pb).get(invoice["id"])["status"] == STATUS_PARTIALLY_PAID


def test_webhook_overpayment_grants_only_invoice_credits(monkeypatch):
    pb = _pb()
    invoice = _seed_invoice(pb, credits=30, cents=9900)
    provider = FakeProvider(get_status=STATUS_PAID, filled_usd=150.0)
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: provider)
    svc = BillingService(pb)
    body = _webhook({"reference_id": "ref-1", "status": "OVER_FILLED", "event_key": "o1"})
    assert asyncio.run(svc.process_webhook(body, {})) == "ok"
    assert CreditRepo(pb).balance("u1") == 30
    stored = InvoiceRepo(pb).get(invoice["id"])
    assert stored["overpaidAmountUsd"] == pytest.approx(51.0)


def test_webhook_unknown_reference_is_acknowledged_without_grant(monkeypatch):
    pb = _pb()
    provider = FakeProvider()
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: provider)
    svc = BillingService(pb)
    body = _webhook({"reference_id": "nope", "status": "FILLED"})
    assert asyncio.run(svc.process_webhook(body, {})) == "unknown"
    assert CreditRepo(pb).balance("u1") == 0


def test_reconcile_recovers_after_missed_webhook(monkeypatch):
    pb = _pb()
    invoice = _seed_invoice(pb)
    provider = FakeProvider(get_status=STATUS_PAID, filled_usd=99.0)
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: provider)
    svc = BillingService(pb)
    reconciled = asyncio.run(svc.reconcile_invoice(invoice))
    assert reconciled["status"] == STATUS_PAID
    assert CreditRepo(pb).balance("u1") == 30
    assert svc.current_plan("u1") is not None
    asyncio.run(svc.reconcile_invoice(reconciled))
    assert CreditRepo(pb).balance("u1") == 30
    assert len(pb.collection("billing_entitlements").get_full_list()) == 1


# ---------------------------------------------------------------------------
# Routes & webhook security
# ---------------------------------------------------------------------------


def test_overview_route_renders_plan_catalog():
    pb = _pb()
    _product(pb, code="cloud", name="Cloud")
    _product(pb, code="cloud_ai", name="Cloud + Managed AI", credits=30, cents=9900)
    req = make_req(pb, make_user("u1"), "p1")
    resp = billing_api.billing_overview(req)
    body = resp.body.decode()
    assert resp.status_code == 200
    assert "Cloud" in body
    assert "No active plan" in body


def test_checkout_route_redirects_to_provider(monkeypatch):
    pb = _pb()
    _product(pb, code="cloud")
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: FakeProvider())
    req = make_req(pb, make_user("u1"), "p1")
    resp = asyncio.run(billing_api.billing_checkout(req, product_code="cloud"))
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
    invoice = _seed_invoice(pb, user_id="u1")
    req = make_req(pb, make_user("u1"), "p1")
    assert billing_api.invoice_detail(req, invoice["id"]).status_code == 200
    req2 = make_req(pb, make_user("u2"), "p1")
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

    assert asyncio.run(billing_api.payram_webhook(_Req())).status_code == 401


def test_webhook_route_accepts_valid_signature_and_activates_plan(monkeypatch):
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
            filled_usd=99.0,
            amount_usd=99.0,
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
    assert CreditRepo(pb).balance("u1") == 30
    assert BillingService(pb).current_plan("u1") is not None


def test_checkout_return_does_not_grant_from_query_params(monkeypatch):
    pb = _pb()
    invoice = _seed_invoice(pb)
    provider = FakeProvider(get_status=STATUS_AWAITING, filled_usd=0.0)
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: provider)
    req = make_req(pb, make_user("u1"), "p1")
    resp = asyncio.run(billing_api.billing_return(req, invoice=invoice["invoiceNumber"]))
    assert resp.status_code == 200
    assert CreditRepo(pb).balance("u1") == 0
    assert BillingService(pb).current_plan("u1") is None
    assert "Waiting for confirmation" in resp.body.decode()


# ---------------------------------------------------------------------------
# Enforcement through the write routes
# ---------------------------------------------------------------------------


def test_byo_write_is_unmetered_and_has_no_billing_payload():
    from app.api import projects as P
    from tests.helpers import make_topic

    pb = _pb()
    project = _project(pb, byo=True)
    topic = make_topic(pb, project["id"])
    req = make_req(pb, make_user("u1"), project["id"])
    P.write_topic(req, project["id"], topic["id"])

    job = pb.collection("jobs").get_full_list()[0]
    assert "billingKey" not in job["payload"]
    assert BillingService(pb).usage.by_operation(f"write:article:{topic['id']}") is None


def test_managed_write_reserves_and_paywalls_at_zero(monkeypatch):
    from app.api import projects as P
    from tests.helpers import make_topic

    pb = _pb()
    project = _project(pb, byo=False)
    topic = make_topic(pb, project["id"])
    req = make_req(pb, make_user("u1"), project["id"])

    # No managed-AI credits → paywall, no job.
    resp = P.write_topic(req, project["id"], topic["id"])
    assert json.loads(resp.headers["HX-Trigger"])["show-toast"]["type"] == "error"
    assert pb.collection("jobs").get_full_list() == []

    # Grant credit → reserved and annotated.
    CreditRepo(pb).append(user="u1", kind="grant", quantity=1, idempotency_key="g1")
    P.write_topic(req, project["id"], topic["id"])
    job = pb.collection("jobs").get_full_list()[0]
    assert job["payload"]["billingKey"] == f"write:article:{topic['id']}"
    assert BillingService(pb).credit_balance("u1") == 0


def test_cancel_topic_releases_the_reservation():
    from app.api import projects as P
    from tests.helpers import make_topic

    pb = _pb()
    project = _project(pb, byo=False)
    CreditRepo(pb).append(user="u1", kind="grant", quantity=1, idempotency_key="g1")
    topic = make_topic(pb, project["id"])
    req = make_req(pb, make_user("u1"), project["id"])
    P.write_topic(req, project["id"], topic["id"])
    assert BillingService(pb).credit_balance("u1") == 0

    P.cancel_topic(req, project["id"], topic["id"])
    assert BillingService(pb).credit_balance("u1") == 1


def test_overview_renders_disabled_state(monkeypatch):
    monkeypatch.setattr(settings, "billing_enabled", False)
    pb = _pb()
    req = make_req(pb, make_user("u1"), "p1")
    body = billing_api.billing_overview(req).body.decode()
    assert "Billing is turned off" in body


def test_return_page_renders_when_invoice_unknown():
    pb = _pb()
    req = make_req(pb, make_user("u1"), "p1")
    body = asyncio.run(billing_api.billing_return(req, invoice="INV-NOPE")).body.decode()
    assert "could not find that payment" in body
