"""Self-hosted checkout: order creation, three-field status, deposit options,
the owner-scoped page, webhook replay, and the in-browser QR encoder.

No test touches a real PayRam instance: the provider is faked. The QR test runs
the shipped JavaScript with node and validates the matrix structurally (BCH
format bits, Reed–Solomon syndromes, payload round-trip).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import billing as billing_api
from app.api import checkout as checkout_api
from app.api.checkout import CheckoutRequest
from app.config import settings
from app.providers.payments.base import (
    STATUS_AWAITING,
    STATUS_PAID,
    STATUS_PARTIALLY_PAID,
    DepositOption,
    PaymentProviderError,
    ProviderPayment,
)
from app.repositories.billing import CreditRepo, InvoiceRepo, PaymentEventRepo
from app.services.billing import BillingError, BillingService, CheckoutNotFound, clear_billing_locks
from tests.fakes import FakePocketBase, default_unique_fields
from tests.helpers import make_req, make_req_plain, make_user

# ---------------------------------------------------------------------------
# Fixtures & fakes
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _billing_on(monkeypatch):
    monkeypatch.setattr(settings, "billing_enabled", True)
    monkeypatch.setattr(settings, "billing_provider", "fake")
    monkeypatch.setattr(settings, "billing_checkout_cooldown_seconds", 0)
    clear_billing_locks()
    yield
    clear_billing_locks()


class FakeProvider:
    name = "fake"

    def __init__(self, *, status=STATUS_AWAITING, options=None):
        self.status = status
        self.options = options or []
        self.created: list[dict] = []
        self.assigns: list[str] = []

    async def create_payment(
        self,
        *,
        amount_usd,
        customer_email,
        customer_id,
        invoice_number,
        description="",
        currency="",
        network="",
    ):
        self.created.append(
            {
                "amount": amount_usd,
                "invoice": invoice_number,
                "currency": currency,
                "network": network,
            }
        )
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
            status=self.status,
            provider_status="FILLED" if self.status == STATUS_PAID else "OPEN",
            amount_usd=1.0,
            filled_usd=1.0 if self.status == STATUS_PAID else 0.0,
        )

    async def get_deposit_options(self, reference):
        return self.options

    async def assign_deposit_address(self, reference, blockchain_code):
        self.assigns.append(blockchain_code)
        return f"addr-{blockchain_code.lower()}"

    def verify_webhook(self, raw_body, headers):
        return True

    def parse_webhook(self, raw_body):
        data = json.loads(raw_body)
        return SimpleNamespace(
            reference=data["reference_id"],
            status=STATUS_PAID,
            provider_status="FILLED",
            event_key=data.get("event_key", data["reference_id"]),
            amount_usd=1.0,
            filled_usd=1.0,
            currency="USD",
            raw={},
        )


def _pb() -> FakePocketBase:
    return FakePocketBase(default_unique_fields())


def _invoice(pb, *, user_id="u1", reference="ref-1", cents=100, credits=0, expires_at=""):
    return InvoiceRepo(pb).create(
        {
            "user": user_id,
            "invoiceNumber": f"INV-{reference}",
            "productCode": "",
            "productName": "Payment",
            "productType": "service",
            "priceCents": cents,
            "currency": "USD",
            "credits": credits,
            "planDays": 0,
            "status": STATUS_AWAITING,
            "provider": "fake",
            "providerReference": reference,
            "expectedAmountUsd": cents / 100.0,
            "expiresAt": expires_at,
        }
    )


# ---------------------------------------------------------------------------
# Order creation
# ---------------------------------------------------------------------------


def test_create_order_posts_the_amount_and_stores_the_reference(monkeypatch):
    pb = _pb()
    provider = FakeProvider()
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: provider)
    invoice = asyncio.run(BillingService(pb).create_order(make_user("u1"), amount_usd=1.0))
    assert provider.created[0]["amount"] == 1.0
    assert provider.created[0]["invoice"] == invoice["invoiceNumber"]
    assert invoice["providerReference"] == f"ref-{invoice['invoiceNumber']}"
    assert invoice["expectedAmountUsd"] == 1.0
    assert invoice["status"] == STATUS_AWAITING
    assert invoice["expiresAt"]


def test_create_order_rejects_amounts_outside_the_allowed_range():
    pb = _pb()
    with pytest.raises(BillingError):
        asyncio.run(BillingService(pb).create_order(make_user("u1"), amount_usd=0.5))
    with pytest.raises(BillingError):
        asyncio.run(BillingService(pb).create_order(make_user("u1"), amount_usd=10**9))


def test_create_order_is_idempotent_on_invoice_number(monkeypatch):
    pb = _pb()
    provider = FakeProvider()
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: provider)
    svc = BillingService(pb)
    first = asyncio.run(svc.create_order(make_user("u1"), amount_usd=1.0, invoice_number="INV-1"))
    again = asyncio.run(svc.create_order(make_user("u1"), amount_usd=1.0, invoice_number="INV-1"))
    assert first["id"] == again["id"]
    assert len(provider.created) == 1


def test_create_order_sends_a_restriction_and_stores_it(monkeypatch):
    pb = _pb()
    provider = FakeProvider()
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: provider)
    invoice = asyncio.run(
        BillingService(pb).create_order(
            make_user("u1"), amount_usd=1.0, currency="usdc", network="base"
        )
    )
    assert provider.created[0]["currency"] == "USDC"
    assert provider.created[0]["network"] == "BASE"
    assert invoice["metadata"]["currency"] == "USDC"
    assert invoice["metadata"]["network"] == "BASE"
    assert BillingService(pb).card_onramp_available(invoice) is False


def test_create_order_rejects_half_a_restriction():
    pb = _pb()
    with pytest.raises(BillingError):
        asyncio.run(BillingService(pb).create_order(make_user("u1"), amount_usd=1, currency="USDC"))
    with pytest.raises(BillingError):
        asyncio.run(BillingService(pb).create_order(make_user("u1"), amount_usd=1, network="BASE"))


def test_card_onramp_flag_and_restriction_gate(monkeypatch):
    pb = _pb()
    svc = BillingService(pb)
    assert svc.card_onramp_available(_invoice(pb, reference="open")) is True
    restricted = {
        **_invoice(pb, reference="pinned"),
        "metadata": {"currency": "USDC", "network": "BASE"},
    }
    assert svc.card_onramp_available(restricted) is False
    monkeypatch.setattr(settings, "payram_card_onramp_enabled", False)
    assert svc.card_onramp_available(_invoice(pb, reference="off")) is False


def test_create_route_returns_reference_and_our_checkout_url(monkeypatch):
    pb = _pb()
    provider = FakeProvider()
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: provider)
    req = make_req(pb, make_user("u1"), "p1")
    resp = asyncio.run(checkout_api.create_checkout(req, CheckoutRequest(amountInUSD=1)))
    body = json.loads(resp.body)
    assert resp.status_code == 200
    assert body["referenceID"].startswith("ref-")
    assert body["url"].endswith(f"/checkout/{body['referenceID']}")
    assert "PAYRAM" not in resp.body.decode().upper()


# ---------------------------------------------------------------------------
# Status: three fields, fulfilment, expiry
# ---------------------------------------------------------------------------


def test_status_returns_only_the_three_public_fields(monkeypatch):
    pb = _pb()
    _invoice(pb)
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: FakeProvider())
    req = make_req(pb, make_user("u1"), "p1")
    resp = asyncio.run(checkout_api.checkout_status(req, "ref-1"))
    assert resp.status_code == 200
    assert set(json.loads(resp.body)) == {"amountInUSD", "paymentState", "expiresAt"}


def test_status_fulfils_a_paid_order_exactly_once(monkeypatch):
    pb = _pb()
    invoice = _invoice(pb, credits=10)
    provider = FakeProvider(status=STATUS_PAID)
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: provider)
    svc = BillingService(pb)

    first = asyncio.run(svc.checkout_status("ref-1"))
    second = asyncio.run(svc.checkout_status("ref-1"))

    assert first["paymentState"] == "FILLED"
    assert second["paymentState"] == "FILLED"
    assert CreditRepo(pb).balance("u1") == 10
    assert InvoiceRepo(pb).get(invoice["id"])["status"] == STATUS_PAID


def test_a_partial_order_is_rechecked_and_can_still_become_paid(monkeypatch):
    pb = _pb()
    invoice = _invoice(pb, credits=3)
    InvoiceRepo(pb).update(invoice["id"], {"status": STATUS_PARTIALLY_PAID})
    monkeypatch.setattr(
        "app.services.billing.get_payment_provider", lambda name: FakeProvider(status=STATUS_PAID)
    )
    status = asyncio.run(BillingService(pb).checkout_status("ref-1"))
    assert status["paymentState"] == "FILLED"
    assert CreditRepo(pb).balance("u1") == 3


def test_reporting_the_same_open_order_keeps_the_same_expiry(monkeypatch):
    pb = _pb()
    _invoice(pb)  # no stored expiry: derive it from `created`, never "now + 24h" again
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: FakeProvider())
    svc = BillingService(pb)
    first = asyncio.run(svc.checkout_status("ref-1"))
    second = asyncio.run(svc.checkout_status("ref-1"))
    assert first["expiresAt"] == second["expiresAt"]


def test_expired_link_reports_cancelled_and_marks_the_order_expired(monkeypatch):
    pb = _pb()
    invoice = _invoice(pb, expires_at="2020-01-01 00:00:00.000Z")
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: FakeProvider())
    status = asyncio.run(BillingService(pb).checkout_status("ref-1"))
    assert status["paymentState"] == "CANCELLED"
    assert InvoiceRepo(pb).get(invoice["id"])["status"] == "expired"


def test_status_404_for_an_unknown_reference():
    pb = _pb()
    req = make_req(pb, make_user("u1"), "p1")
    resp = asyncio.run(checkout_api.checkout_status(req, "nope"))
    assert resp.status_code == 404


def test_status_is_owner_scoped(monkeypatch):
    pb = _pb()
    _invoice(pb, user_id="u2")
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: FakeProvider())
    req = make_req(pb, make_user("u1"), "p1")
    assert asyncio.run(checkout_api.checkout_status(req, "ref-1")).status_code == 404


# ---------------------------------------------------------------------------
# Deposit options
# ---------------------------------------------------------------------------


def test_options_assign_one_address_per_family(monkeypatch):
    pb = _pb()
    _invoice(pb)
    provider = FakeProvider(
        options=[
            DepositOption(
                id="1",
                network="Base",
                currency="USDC",
                family="ETH_Family",
                blockchain_code="BASE",
                recommended=True,
            ),
            DepositOption(
                id="2",
                network="Ethereum",
                currency="ETH",
                family="ETH_Family",
                blockchain_code="ETH",
            ),
            DepositOption(
                id="3",
                network="Bitcoin",
                currency="BTC",
                family="BTC_Family",
                blockchain_code="BTC",
                address="bc1qexisting",
                most_used=True,
            ),
        ]
    )
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: provider)
    options = asyncio.run(BillingService(pb).deposit_options("ref-1"))

    assert provider.assigns == ["BASE"]  # shared family, never assigned twice
    assert [o["network"] for o in options] == ["Base", "Ethereum", "Bitcoin"]
    assert options[0]["address"] == options[1]["address"] == "addr-base"
    assert options[2]["address"] == "bc1qexisting"
    assert options[0]["recommended"] is True
    assert options[2]["mostUsed"] is True


def test_options_hide_a_family_that_cannot_be_assigned(monkeypatch):
    pb = _pb()
    _invoice(pb)

    class Failing(FakeProvider):
        async def assign_deposit_address(self, reference, blockchain_code):
            if blockchain_code == "BTC":
                raise PaymentProviderError("no xpub for this family")
            return await super().assign_deposit_address(reference, blockchain_code)

    provider = Failing(
        options=[
            DepositOption(
                id="1", network="Base", currency="USDC", family="ETH_Family", blockchain_code="BASE"
            ),
            DepositOption(
                id="2",
                network="Bitcoin",
                currency="BTC",
                family="BTC_Family",
                blockchain_code="BTC",
            ),
        ]
    )
    monkeypatch.setattr("app.services.billing.get_payment_provider", lambda name: provider)
    options = asyncio.run(BillingService(pb).deposit_options("ref-1"))
    assert [o["network"] for o in options] == ["Base"]
    assert options[0]["address"] == "addr-base"


def test_options_404_for_an_unknown_reference():
    pb = _pb()
    req = make_req(pb, make_user("u1"), "p1")
    assert asyncio.run(checkout_api.checkout_options(req, "nope")).status_code == 404


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def test_checkout_page_exposes_the_documented_card_hand_off(monkeypatch):
    pb = _pb()
    _invoice(pb)
    monkeypatch.setattr(settings, "payram_base_url", "https://pay.example.com")
    monkeypatch.setattr(settings, "payram_card_onramp_enabled", True)
    body = checkout_api.checkout_page(make_req(pb, make_user("u1"), "p1"), "ref-1").body.decode()
    assert 'data-card-available="true"' in body
    assert "https://pay.example.com/payments?" in body
    assert "reference_id=ref-1" in body
    assert "host=https%3A%2F%2Fpay.example.com" in body


def test_checkout_page_hides_the_card_for_a_restricted_order(monkeypatch):
    pb = _pb()
    invoice = _invoice(pb)
    InvoiceRepo(pb).update(invoice["id"], {"metadata": {"currency": "USDC", "network": "BASE"}})
    monkeypatch.setattr(settings, "payram_card_onramp_enabled", True)
    body = checkout_api.checkout_page(make_req(pb, make_user("u1"), "p1"), "ref-1").body.decode()
    assert 'data-card-available="false"' in body
    assert 'data-card-url=""' in body


def test_checkout_page_hides_the_card_when_the_flag_is_off(monkeypatch):
    pb = _pb()
    _invoice(pb)
    monkeypatch.setattr(settings, "payram_card_onramp_enabled", False)
    body = checkout_api.checkout_page(make_req(pb, make_user("u1"), "p1"), "ref-1").body.decode()
    assert 'data-card-available="false"' in body


def test_return_page_renders_in_return_mode():
    pb = _pb()
    _invoice(pb)
    resp = checkout_api.checkout_return(make_req(pb, make_user("u1"), "p1"), "ref-1")
    body = resp.body.decode()
    assert resp.status_code == 200
    assert 'data-mode="return"' in body


def test_return_page_is_a_clean_404_for_a_garbage_reference():
    pb = _pb()
    resp = checkout_api.checkout_return(make_req(pb, make_user("u1"), "p1"), "garbage")
    assert resp.status_code == 404
    assert "Checkout not found" in resp.body.decode()


def test_page_renders_the_card_for_the_owner():
    pb = _pb()
    _invoice(pb)
    req = make_req(pb, make_user("u1"), "p1")
    resp = checkout_api.checkout_page(req, "ref-1")
    body = resp.body.decode()
    assert resp.status_code == 200
    assert 'data-reference="ref-1"' in body
    assert "Powered by" in body
    assert "payram" not in body.lower()


def test_page_renders_the_full_shell_and_script():
    pb = _pb()
    _invoice(pb)
    req = make_req_plain(pb, make_user("u1"))
    body = checkout_api.checkout_page(req, "ref-1").body.decode()
    assert "<!doctype html>" in body.lower()
    assert "checkout.js" in body
    assert "min-h-[19rem]" in body


def test_page_is_a_clean_404_for_a_garbage_reference():
    pb = _pb()
    req = make_req(pb, make_user("u1"), "p1")
    resp = checkout_api.checkout_page(req, "garbage")
    assert resp.status_code == 404
    assert "Checkout not found" in resp.body.decode()
    assert "Traceback" not in resp.body.decode()


def test_page_is_owner_scoped():
    pb = _pb()
    _invoice(pb, user_id="u2")
    req = make_req(pb, make_user("u1"), "p1")
    assert checkout_api.checkout_page(req, "ref-1").status_code == 404


def test_service_raises_not_found_for_unknown_reference():
    with pytest.raises(CheckoutNotFound):
        BillingService(_pb()).invoice_for_reference("missing")


def test_api_key_never_reaches_the_client_bundle():
    key = settings.payram_api_key
    if not key:
        pytest.skip("PAYRAM_API_KEY is not configured in this environment")
    static = Path(__file__).resolve().parents[1] / "app" / "static"
    leaks = [
        str(path)
        for path in static.rglob("*")
        if path.suffix in {".js", ".css", ".html"} and key in path.read_text(errors="ignore")
    ]
    assert leaks == []


# ---------------------------------------------------------------------------
# Webhook replay on the documented /api/payram/webhook path
# ---------------------------------------------------------------------------


def test_webhook_replay_marks_the_order_paid_once(monkeypatch):
    pb = _pb()
    invoice = _invoice(pb, credits=5)
    monkeypatch.setattr(settings, "billing_provider", "payram")
    monkeypatch.setattr(settings, "payram_api_key", "k")
    monkeypatch.setattr(settings, "payram_webhook_secret", "")
    monkeypatch.setattr(
        "app.services.billing.get_payment_provider", lambda name: FakeProvider(status=STATUS_PAID)
    )

    raw = b'{"reference_id":"ref-1","status":"FILLED","event_key":"evt-1"}'
    signature = "sha256=" + hmac.new(b"k", raw, hashlib.sha256).hexdigest()

    class _Req:
        state = SimpleNamespace(pb=pb)
        headers = {"X-Payram-Signature": signature}

        async def body(self):
            return raw

    first = asyncio.run(billing_api.payram_webhook(_Req()))
    second = asyncio.run(billing_api.payram_webhook(_Req()))

    assert first.status_code == second.status_code == 200
    assert b"duplicate" in second.body
    assert CreditRepo(pb).balance("u1") == 5
    assert InvoiceRepo(pb).get(invoice["id"])["status"] == STATUS_PAID
    assert len(PaymentEventRepo(pb).list_records(per_page=10)) == 1


# ---------------------------------------------------------------------------
# QR encoder (the shipped JS, run with node)
# ---------------------------------------------------------------------------

_QR_DATA = [16, 28, 44, 64, 86, 108]  # level M payload codewords, versions 1–6
_QR_ECC = [10, 16, 26, 18, 24, 16]
_QR_BLOCKS = [1, 1, 1, 2, 2, 4]


def _qr_matrix(text: str) -> list[list[int]]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    source = Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "qrcode.js"
    with tempfile.TemporaryDirectory() as folder:
        module = Path(folder) / "qrcode.mjs"
        shutil.copy(source, module)
        runner = Path(folder) / "run.mjs"
        runner.write_text(
            f"import {{ qrMatrix }} from {json.dumps(module.as_uri())};\n"
            f"console.log(JSON.stringify(qrMatrix({json.dumps(text)}).matrix));\n",
            encoding="utf-8",
        )
        raw = subprocess.check_output([node, str(runner)], text=True)
    return json.loads(raw)


def _gf_tables():
    exp = [0] * 512
    log = [0] * 256
    value = 1
    for i in range(255):
        exp[i] = value
        log[value] = i
        value <<= 1
        if value & 0x100:
            value ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


def _fixed_cells(size: int, version: int) -> list[list[bool]]:
    fixed = [[False] * size for _ in range(size)]

    def mark(r, c):
        if 0 <= r < size and 0 <= c < size:
            fixed[r][c] = True

    for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                mark(top + dr, left + dc)
    for i in range(8, size - 8):
        mark(6, i)
        mark(i, 6)
    centers = [None, [], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34]][version]
    if centers:
        last = centers[-1]
        for r in centers:
            for c in centers:
                if (r, c) in ((6, 6), (6, last), (last, 6)):
                    continue
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        mark(r + dr, c + dc)
    for i in range(9):
        mark(i, 8)
        mark(8, i)
    for i in range(8):
        mark(8, size - 1 - i)
        mark(size - 1 - i, 8)
    mark(size - 8, 8)
    return fixed


def _decode_qr(matrix: list[list[int]]) -> tuple[str, int, int]:
    """Return (payload, version, mask); also asserts RS validity and format BCH."""
    size = len(matrix)
    version = (size - 17) // 4
    assert 1 <= version <= 6

    bits = 0
    for i in range(6):
        bits |= matrix[i][8] << i
    bits |= matrix[7][8] << 6
    bits |= matrix[8][8] << 7
    bits |= matrix[8][7] << 8
    for i in range(9, 15):
        bits |= matrix[8][14 - i] << i
    value = bits ^ 0x5412
    assert (value >> 13) & 0b11 == 0b00, "format info must say error-correction level M"
    mask = (value >> 10) & 0b111

    masks = [
        lambda r, c: (r + c) % 2 == 0,
        lambda r, c: r % 2 == 0,
        lambda r, c: c % 3 == 0,
        lambda r, c: (r + c) % 3 == 0,
        lambda r, c: (r // 2 + c // 3) % 2 == 0,
        lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
        lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
        lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
    ]
    fixed = _fixed_cells(size, version)
    stream: list[int] = []
    row = size - 1
    direction = -1
    col = size - 1
    while col >= 1:
        if col == 6:
            col = 5
        for _ in range(size):
            for k in range(2):
                c = col - k
                if not fixed[row][c]:
                    stream.append(matrix[row][c] ^ (1 if masks[mask](row, c) else 0))
            row += direction
            if row < 0 or row >= size:
                row -= direction
                direction = -direction
                break
        col -= 2

    words = []
    for i in range(0, len(stream) - 7, 8):
        word = 0
        for bit in stream[i : i + 8]:
            word = (word << 1) | bit
        words.append(word)

    per_block = _QR_DATA[version - 1] // _QR_BLOCKS[version - 1]
    blocks = _QR_BLOCKS[version - 1]
    data = [[] for _ in range(blocks)]
    index = 0
    for _ in range(per_block):
        for b in range(blocks):
            data[b].append(words[index])
            index += 1
    ecc = [[] for _ in range(blocks)]
    for _ in range(_QR_ECC[version - 1]):
        for b in range(blocks):
            ecc[b].append(words[index])
            index += 1

    exp, log = _gf_tables()

    def mul(a, b):
        return 0 if not a or not b else exp[log[a] + log[b]]

    for b in range(blocks):
        codeword = data[b] + ecc[b]
        for power in range(_QR_ECC[version - 1]):
            syndrome = 0
            for offset, coefficient in enumerate(codeword):
                if coefficient:
                    exponent = (power * (len(codeword) - 1 - offset)) % 255
                    syndrome ^= mul(coefficient, exp[exponent])
            assert syndrome == 0, f"Reed-Solomon syndrome {power} is non-zero"

    payload = [word for block in data for word in block]
    bits = "".join(f"{word:08b}" for word in payload)
    assert bits[:4] == "0100", "byte mode"
    length = int(bits[4:12], 2)
    text = bytes(int(bits[12 + 8 * i : 20 + 8 * i], 2) for i in range(length)).decode()
    return text, version, mask


@pytest.mark.parametrize(
    "text",
    [
        "hello",
        "0xCb12499d865271D1FfFf16308E523e0BB624a779",
        "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq",
        "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE",
        "x" * 106,
    ],
)
def test_qr_encoder_produces_a_scannable_matrix(text):
    payload, version, _ = _decode_qr(_qr_matrix(text))
    assert payload == text
    assert 1 <= version <= 6
