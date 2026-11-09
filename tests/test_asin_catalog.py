"""Asin: every paid row can run on treg's platform key, priced in USD through the credit rate in
fx.yaml (https://www.asin.dev/pricing, checked 2026-11-09); the balance collector and the out-of-credits answers."""
from __future__ import annotations

import re

import httpx
import pytest

from treg.domain.capacity import collectors
from treg.domain.capacity import signatures as S
from treg.domain.catalog import store as cs


def test_every_paid_asin_route_is_platform_priced():
    cat = cs.load()
    eps = [e for e in cat.endpoints if e["provider"] == "asin"]
    assert eps
    for ep in eps:
        if ep["cost"]["type"] == "free" or ep.get("scope") == "own_account":
            continue
        assert cat.platform_eligible(ep), ep["id"]
        assert cat.cost_view(ep["cost"], "asin")["usd"] > 0, ep["id"]


async def test_asin_balance_reads_the_remaining_credits():
    def usage(request):
        assert request.url.path == "/api/v1/account" and request.headers["authorization"] == "Bearer test"
        return httpx.Response(200, json={"plan": "basic", "plan_name": "Starter", "credits": 29880, "monthly_credits": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(usage)) as client:
        got = await collectors._asin(client, "test")
    assert got["value"] == 29880 and got["unit"] == "credits"


async def test_asin_balance_refuses_an_answer_without_a_balance():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as client:
        with pytest.raises(ValueError, match="remaining-credit"):
            await collectors._asin(client, "test")


@pytest.mark.parametrize("body", [
    '{"error":{"code":"insufficient_credits","message":"x"}}',
    '{"error":{"code":"email_not_verified","message":"x"}}',
])
def test_asin_out_of_credits_answers_are_recorded(body):
    sig = next(s for s in S._TABLE if s.provider == "asin")
    assert sig.status == 402 and sig.kind in S.EXHAUSTING_KINDS
    assert re.search(sig.body_regex, body, re.I)
