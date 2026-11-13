"""Piloterr: the balance collector and the out-of-credits and rate-limit answers."""
from __future__ import annotations

import re

import httpx
import pytest

from treg.domain.capacity import collectors
from treg.domain.capacity import signatures as S


async def test_piloterr_balance_reads_the_remaining_credits():
    def usage(request):
        assert request.url.path == "/v2/usage" and request.headers["x-api-key"] == "test"
        return httpx.Response(200, json={"remaining": 17880, "total_used": 120, "renewal_date": "2026-11-24",
                                          "credits": {"given": 0, "consumed": 0, "remaining": 0}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(usage)) as client:
        got = await collectors._piloterr(client, "test")
    assert got["value"] == 17880 and got["unit"] == "credits"


async def test_piloterr_balance_refuses_an_answer_without_a_balance():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as client:
        with pytest.raises(ValueError, match="remaining-credit"):
            await collectors._piloterr(client, "test")


@pytest.mark.parametrize("body", [
    '{"error":"Payment required"}',
    '{"error":"Insufficient credits: 3 required, 1 available"}',
    '{"error":"You have open invoices: Invoice #1"}',
])
def test_piloterr_out_of_credits_answers_are_recorded(body):
    sig = next(s for s in S._TABLE if s.provider == "piloterr")
    assert sig.status == 402 and sig.kind in S.EXHAUSTING_KINDS
    assert re.search(sig.body_regex, body, re.I)


@pytest.mark.parametrize("body,kind", [
    ('{"error":"Rate limit exceeded: 2 requests per second"}', "burst"),
    ('{"error":"Rate limit exceeded for the API key: quota daily"}', "quota"),
])
def test_piloterr_rate_limits_answer_401_and_are_not_a_dead_key(body, kind):
    signal = S.classify("piloterr", 401, None, body)
    assert signal is not None and signal.kind == kind


def test_piloterr_an_invalid_key_stays_an_auth_failure():
    assert S.classify("piloterr", 401, None, '{"error":"Invalid API Key"}') is None
