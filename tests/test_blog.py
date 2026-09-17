"""Blog post tests: receipt posts under /blog/ with their content, canonical URLs, and measured data.

Each post is a thin announcement with measured numbers that must match the source data exactly.
"""

from __future__ import annotations

import re

import pytest
from httpx import AsyncClient

from treg.config import get_settings


def _base() -> str:
    return get_settings().public_url.rstrip("/")


async def test_blog_index_returns_200_and_lists_posts(clients: AsyncClient):
    r = await clients.get("/blog")
    assert r.status_code == 200
    assert '<h1>Launches and Notes</h1>' in r.text
    assert 'href="/blog/work-email-finding-bench"' in r.text
    assert 'href="/blog/people-search-bench"' in r.text


async def test_blog_index_canonical(clients: AsyncClient):
    r = await clients.get("/blog")
    assert f'<link rel="canonical" href="{_base()}/blog"/>' in r.text


# ---- Work Email Finding Bench ----

async def test_work_email_finding_bench_returns_200(clients: AsyncClient):
    r = await clients.get("/blog/work-email-finding-bench")
    assert r.status_code == 200


async def test_work_email_finding_bench_canonical(clients: AsyncClient):
    r = await clients.get("/blog/work-email-finding-bench")
    assert f'<link rel="canonical" href="{_base()}/blog/work-email-finding-bench"/>' in r.text


async def test_work_email_finding_bench_h1(clients: AsyncClient):
    r = await clients.get("/blog/work-email-finding-bench")
    assert '<h1>Work Email Finding: a 292 Person Receipt</h1>' in r.text


async def test_work_email_finding_bench_measured_numbers(clients: AsyncClient):
    """The measured numbers from the 2026-09-16 run must appear verbatim."""
    r = await clients.get("/blog/work-email-finding-bench")
    body = r.text
    assert '90.4%' in body
    assert '89.7%' in body
    assert '$0.0056' in body
    assert '$0.0395' in body
    assert '264' in body
    assert '262' in body
    assert '$1.49' in body
    assert '$10.34' in body
    assert '0.44s' in body


async def test_work_email_finding_bench_links_related_pages(clients: AsyncClient):
    r = await clients.get("/blog/work-email-finding-bench")
    assert 'href="/pricing"' in r.text
    assert 'href="/people-search"' in r.text
    assert 'href="/workflows/find-and-verify-a-lead-list"' in r.text
    assert 'href="/use-cases/lead-enrichment-for-ai-agents"' in r.text


async def test_work_email_finding_bench_discloses_list_bias(clients: AsyncClient):
    r = await clients.get("/blog/work-email-finding-bench")
    assert 'published team-page email' in r.text
    assert 'find rates are inflated' in r.text


async def test_work_email_finding_bench_discloses_millionverifier(clients: AsyncClient):
    r = await clients.get("/blog/work-email-finding-bench")
    assert 'MillionVerifier' in r.text
    assert 'MV served 0' in r.text


async def test_work_email_finding_bench_discloses_502_bug(clients: AsyncClient):
    r = await clients.get("/blog/work-email-finding-bench")
    assert '502' in r.text
    assert 'route_failed' in r.text
    assert '3 rows' in r.text


async def test_work_email_finding_bench_breadcrumbs(clients: AsyncClient):
    r = await clients.get("/blog/work-email-finding-bench")
    assert 'BreadcrumbList' in r.text
    assert '"name": "treg.to"' in r.text or '"name":"treg.to"' in r.text


async def test_work_email_finding_bench_fair_clay_framing(clients: AsyncClient):
    """The post must include when to choose Clay, not just trash it."""
    r = await clients.get("/blog/work-email-finding-bench")
    assert 'Choose Clay' in r.text or 'choose Clay' in r.text
    assert 'visual table' in r.text or 'GTM' in r.text


async def test_work_email_finding_bench_no_em_dashes(clients: AsyncClient):
    """Product constraint: no em-dashes in page copy."""
    r = await clients.get("/blog/work-email-finding-bench")
    assert '\u2014' not in r.text


# ---- People Search Bench ----

async def test_people_search_bench_returns_200(clients: AsyncClient):
    r = await clients.get("/blog/people-search-bench")
    assert r.status_code == 200


async def test_people_search_bench_canonical(clients: AsyncClient):
    r = await clients.get("/blog/people-search-bench")
    assert f'<link rel="canonical" href="{_base()}/blog/people-search-bench"/>' in r.text
