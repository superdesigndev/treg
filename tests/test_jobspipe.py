"""JobsPipe bring-your-own-key catalog: a free bearer probe and credit-priced job/company rows."""

from __future__ import annotations

from treg.domain.catalog.store import load
from treg import oauth_providers as P


def test_jobspipe_connects_with_a_free_bearer_probe():
    provider = P.REGISTRY["jobspipe"]
    assert provider.auth_kind == "key"
    assert provider.uses_pasted_secret is True
    assert P.is_configured(provider) is True
    assert provider.base_url == "https://api.jobspipe.dev"
    assert provider.token_header == "Authorization"
    assert provider.token_format == "Bearer {secret}"
    assert provider.probe_path == "/v1/account"
    assert provider.probe_method == "GET"
    assert provider.probe_cost_micro == 0


def test_jobspipe_rows_are_priced_from_the_builder_package():
    catalog = load()
    per_record = {
        "jobspipe.jobs.search", "jobspipe.jobs.agentic_search", "jobspipe.companies.jobs",
        "jobspipe.jobs.detail", "jobspipe.companies.search.technology",
    }
    per_call = {
        "jobspipe.companies.enrich", "jobspipe.companies.technologies",
        "jobspipe.technologies.list", "jobspipe.technologies.detail", "jobspipe.web.stack.scan",
    }
    for endpoint_id in per_record | per_call:
        row = catalog.by_id[endpoint_id]
        assert catalog.platform_eligible(row), endpoint_id
        view = catalog.cost_view(row["cost"], "jobspipe")
        assert view["usd"] == 0.00196, endpoint_id
        assert row["cost"]["confidence"] in {"verified", "documented"}
    for endpoint_id in per_record:
        cost = catalog.by_id[endpoint_id]["cost"]
        # already-paid records are free, so the charge settles from the response, not the estimate
        assert cost["settle"] == "usage", endpoint_id
        assert cost["usage"] == {"path": "metadata.credits_charged", "unit": "credit"}, endpoint_id
