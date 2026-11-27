"""Candidate-side filters on the routed job search: written-in language and visa sponsorship.

A candidate looking for English-language roles in Denmark needs both: JobsPipe takes
`language_or` and `visa_sponsorship_or`, but the routed `companies.jobs.search` contract
never exposed them, so an agent using treg's job index could not ask either question.
"""
from __future__ import annotations

from treg.domain.catalog import store as catalog_store
from treg.domain.catalog.routing.contracts import adapter_accepts, canonical_identity
from treg.domain.catalog.routing.plan import ignored_filters


def _jobs_identity(**given):
    cat = catalog_store.load()
    contract = cat.contracts["companies.jobs.search"]
    ident, _ = canonical_identity(contract, {"title": "backend engineer", "country": "DK", **given})
    return cat, contract, ident


def test_routed_job_search_maps_a_language_filter_to_jobspipe():
    cat, contract, ident = _jobs_identity(language="en")
    assert "language" in contract.filters, "the routed contract must accept a language filter"
    ad = cat.adapters["jobspipe.jobs.search"]
    variant = adapter_accepts(ad, ident)
    assert variant == ("title",)
    _, body = ad.to_upstream(ident, variant)
    assert body["language_or"] == ["en"]
    assert body["job_country_code_or"] == ["DK"]


def test_routed_job_search_maps_visa_sponsorship_to_jobspipe():
    cat, contract, ident = _jobs_identity(visa_sponsorship="offers")
    assert "visa_sponsorship" in contract.filters
    ad = cat.adapters["jobspipe.jobs.search"]
    _, body = ad.to_upstream(ident, adapter_accepts(ad, ident))
    assert body["visa_sponsorship_or"] == ["offers"]


def test_candidate_filters_are_omitted_from_a_request_that_did_not_ask_for_them():
    cat, _, ident = _jobs_identity()
    ad = cat.adapters["jobspipe.jobs.search"]
    _, body = ad.to_upstream(ident, adapter_accepts(ad, ident))
    assert "language_or" not in body
    assert "visa_sponsorship_or" not in body


def test_only_the_provider_that_takes_a_filter_claims_it():
    """A provider with no place for a filter answers a looser question; ranking must know it."""
    cat, contract, ident = _jobs_identity(language="en", visa_sponsorship="offers")
    given = frozenset({"language", "visa_sponsorship"})
    expected = {
        "jobspipe.jobs.search": (),
        "hirebase.jobs.search": ("language", "visa_sponsorship"),
        "theirstack.jobs.search": ("language", "visa_sponsorship"),
        "mobiusengine-jobs.jobs.search": ("language", "visa_sponsorship"),
    }
    for endpoint_id, want in expected.items():
        ad = cat.adapters[endpoint_id]
        assert ignored_filters(ad, contract, ident, given) == want, endpoint_id
