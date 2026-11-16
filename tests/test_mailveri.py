"""MailVeri verification, v2 settlement from credits_used, routing verdicts, and capacity."""
from __future__ import annotations

import json

import httpx
import pytest

from treg.config import platform_setting_name
from treg.domain.capacity import collectors, signatures
from treg.domain.capacity.view import view as capacity_view
from treg.domain.catalog import outcome, results
from treg.domain.catalog import store as catalog_store
from treg.domain.catalog.schema.v2 import is_v2
from treg.oauth_providers import MAILVERI, REGISTRY

VERIFY = "mailveri.people.email.verify"
USAGE = "mailveri.account.usage"


def test_mailveri_registry_and_capacity_wiring():
    assert REGISTRY["mailveri"] is MAILVERI
    assert platform_setting_name("mailveri") == "platform_key_mailveri"
    assert MAILVERI.token_header == "Authorization" and MAILVERI.token_format == "Bearer {secret}"
    assert MAILVERI.probe_path == "/v1/get-balance/" and MAILVERI.token_reject_field == "error"
    assert collectors.BALANCE_ROUTES["mailveri"] is collectors._mailveri
    assert capacity_view.concurrency_pool("mailveri", VERIFY) == ("mailveri", 5)
    assert capacity_view.rate_bucket("mailveri", VERIFY) == ("mailveri", 10, 1.0)


def test_mailveri_v2_rows_and_pricing():
    cat = catalog_store.load()
    verify = cat.by_id[VERIFY]
    usage = cat.by_id[USAGE]

    assert is_v2(verify) and is_v2(usage)
    assert cat.platform_eligible(verify)
    assert not cat.platform_eligible(usage)  # own_account scope stays off shared platform keys
    assert verify["settle"] == {"reported": {"path": "response.credits_used", "unit": "credit"}}
    assert verify["cost"]["reported_charge"] == {"path": "credits_used", "unit": "credit"}
    assert cat.credit_rates["mailveri"] == pytest.approx(0.0009)


@pytest.mark.parametrize("raw_status,expected_valid,expected_word,is_miss", [
    ("VALID", True, "valid", False),
    ("ROLE_ACCOUNT", True, "valid", False),
    ("INVALID", False, "invalid", False),
    ("CATCH_ALL", False, "catch_all", False),
    ("DISPOSABLE", False, "risky", False),
    ("FULL_INBOX", False, "risky", False),
    ("UNKNOWN", False, "unknown", True),
])
def test_mailveri_adapter_maps_all_statuses_and_marks_unknown_miss(
    raw_status, expected_valid, expected_word, is_miss,
):
    cat = catalog_store.load()
    adapter = cat.adapters[VERIFY]
    endpoint = cat.by_id[VERIFY]
    assert adapter.verified

    doc = {
        "error": 0,
        "status": raw_status,
        "email": "founder@acme-saas.io",
        "quality_score": 98 if expected_valid else 50,
        "credits_used": 0 if raw_status == "UNKNOWN" else 1,
    }
    mapped = adapter.from_upstream(doc)
    assert mapped == {
        "valid": expected_valid,
        "status": raw_status,
        "score": 98 if expected_valid else 50,
    }
    assert adapter.is_miss(doc, endpoint) is is_miss
    assert results.verdict(VERIFY, 200, json.dumps(doc).encode()) == expected_word


def test_mailveri_error_envelope_fails_expect_and_signals_empty_balance():
    cat = catalog_store.load()
    endpoint = cat.by_id[VERIFY]
    adapter = cat.adapters[VERIFY]
    empty_doc = {
        "error": 2,
        "message": "Insufficient credits to verify founder@acme-saas.io",
        "credits_used": 0,
    }
    out = outcome.judge(endpoint, 200, empty_doc)
    assert out.state == "error" and out.cause == "expect"
    assert adapter.from_upstream(empty_doc)["valid"] is None
    assert results.verdict(VERIFY, 200, json.dumps(empty_doc).encode()) is None

    sig = signatures.classify("mailveri", 200, {}, json.dumps(empty_doc).encode())
    assert sig is not None and sig.kind == "balance"

    burst = signatures.classify("mailveri", 429, {}, b'{"detail":"Too many checks running at once."}')
    assert burst is not None and burst.kind == "burst"


async def test_mailveri_balance_collector_reads_credits_and_rejects_error_envelope():
    def ok_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer mv-key"
        return httpx.Response(200, json={
            "error": 0,
            "message": "You have 12500 credits",
            "balance": 12500,
            "daily_limit": 5000,
            "daily_remaining": 4800,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(ok_handler)) as c:
        row = await collectors._mailveri(c, "mv-key")
    assert row["value"] == 12500 and row["unit"] == "credits"
    assert "4800/5000" in row["note"]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"error": 3, "message": "Invalid key"})),
    ) as c:
        with pytest.raises(ValueError, match="MailVeri error 3"):
            await collectors._mailveri(c, "bad-key")
