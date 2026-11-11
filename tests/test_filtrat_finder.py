"""Filtrat's finder is its own provider: its own key, price and limits, apart from verification."""
from treg.config import platform_setting_name
from treg.domain.capacity import collectors
from treg.domain.capacity.view import view as capacity_view
from treg.domain.catalog import store as catalog_store
from treg.oauth_providers import FILTRAT_FINDER

FIND = "filtrat-finder.people.email.find"


def test_the_finder_has_its_own_key_and_registry_entry():
    assert platform_setting_name("filtrat-finder") == "platform_key_filtrat_finder"
    assert FILTRAT_FINDER.token_format == "Bearer {secret}" and FILTRAT_FINDER.probe_path == "/credits/balance"
    assert collectors.BALANCE_ROUTES["filtrat_finder"] is collectors._filtrat


def test_the_finder_row_is_live_priced_on_its_own_credit_rate():
    catalog = catalog_store.load()
    row = catalog.by_id[FIND]
    assert row["provider"] == "filtrat-finder" and not row.get("status")
    assert row["platform_request"] == {"body.resolve_catchall": True}
    assert row["cost"]["settle"] == "usage" and row["cost"]["fallback"]["value"] == 25


def test_the_old_finder_rows_point_to_the_new_one():
    by_id = catalog_store.load().by_id
    for old in ("filtrat.people.email.find", "filtrat.people.email.find.resolve_catchall"):
        assert by_id[old]["status"] == "retired" and by_id[old]["superseded_by"] == FIND


def test_the_finder_limits_are_its_own():
    assert capacity_view.concurrency_pool("filtrat-finder", FIND) == ("filtrat-finder", 20)
    assert capacity_view.rate_bucket("filtrat-finder", FIND) == ("filtrat-finder", 110, 60.0)
    assert capacity_view.concurrency_pool("filtrat", "filtrat.people.email.verify") == ("filtrat", 10)


COMPANY = "filtrat-finder.companies.enrich"
COMPANY_MD = "filtrat-finder.linkedin.company.profile"


def test_company_pages_are_priced_per_page_found_and_fixed_to_their_mode():
    by_id = catalog_store.load().by_id
    for ep, mode in ((COMPANY, "json"), (COMPANY_MD, "markdown")):
        row = by_id[ep]
        assert row["provider"] == "filtrat-finder" and not row.get("status")
        assert row["platform_request"] == {"body.mode": mode}
        assert row["cost"]["type"] == "per_success" and row["cost"]["value"] == 12
        assert row["cost"]["settle"] == "usage"


def test_company_pages_have_their_own_limits_apart_from_finder_searches():
    assert capacity_view.rate_bucket("filtrat-finder", COMPANY) == (COMPANY, 300, 60.0)
    assert capacity_view.concurrency_pool("filtrat-finder", COMPANY)[1] == 20
    assert capacity_view.concurrency_pool("filtrat-finder", COMPANY) != ("filtrat-finder", 20)
    assert capacity_view.concurrency_pool("filtrat-finder", FIND) == ("filtrat-finder", 20)
