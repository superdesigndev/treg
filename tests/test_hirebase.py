from treg.domain.capacity.signatures import classify, is_exhausting


def test_a_spent_hirebase_meter_locks_but_a_short_allowance_and_a_rate_limit_do_not():
    spent = classify("hirebase", 429, {"x-billing-code": "limit_exceeded"},
                     b'{"detail":"You\'ve reached your plan\'s included usage of 3 for this period."}')
    short = classify("hirebase", 429, {"x-billing-code": "limit_exceeded"},
                     b'{"detail":"This request could use up to 100 jobs but only 2 of your plan\'s '
                     b'included 500 remain this period."}')
    burst = classify("hirebase", 429, {"retry-after": "39"},
                     b'{"detail":"Rate limit exceeded. Try again in 39s."}')
    assert spent.kind == "quota" and is_exhausting(spent)
    # A `limit` larger than what is left is refused, but a smaller request still runs: never lock.
    assert not is_exhausting(short)
    none_left = classify("hirebase", 429, {"x-billing-code": "limit_exceeded"},
                         b'{"detail":"This request could use up to 100 jobs but only 0 of your plan\'s '
                         b'included 500 remain this period."}')
    assert none_left.kind == "quota" and is_exhausting(none_left)
    assert burst.kind == "burst" and not is_exhausting(burst)


async def test_the_hirebase_jobs_meter_never_locks_the_whole_provider(monkeypatch):
    """Its meters are per operation; the jobs count alone cannot speak for companies or salary."""
    from treg.domain.capacity import collectors

    async def fake_get(c, url, headers=None):
        return {"meters": [{"event_name": "m_jobs_api_calls", "remaining": 0, "overage_mode": "block"}]}
    monkeypatch.setattr(collectors, "_get", fake_get)
    out = await collectors._hirebase(None, "k")
    assert out["value"] == 0 and out["informational"] is True


def _routing():
    from pathlib import Path

    import yaml

    from treg.domain.catalog.routing import contracts as C

    catalog = Path(__file__).parents[1] / "src" / "treg" / "catalog"
    contract = C.parse_contracts(yaml.safe_load((catalog / "contracts.yaml").read_text()))["companies.jobs.search"]
    adapter = C.parse_adapters(yaml.safe_load((catalog / "adapters.yaml").read_text()))["hirebase.jobs.search"]
    return contract, adapter


def test_a_free_text_place_reaches_hirebase_as_a_city_to_geocode():
    """Hirebase geocodes each geo_locations field, so "New York, NY" whole in `city` is the place the
    caller named; before, `location` had no field and a location search went to another provider."""
    from treg.domain.catalog.routing import paths as P
    from treg.domain.catalog.routing.plan import ignored_filters

    contract, adapter = _routing()
    expr = adapter.in_expr["body.geo_locations"]
    asked = {"title": "account executive", "location": "New York, NY"}
    assert ignored_filters(adapter, contract, asked) == ()
    assert P.evaluate(expr, {**asked, "country": None}) == [{"city": "New York, NY"}]
    assert P.evaluate(expr, {"location": "Texas", "country": "US"}) == [{"city": "Texas", "country": "United States"}]
    assert P.evaluate(expr, {"location": None, "country": "GB"}) == [{"country": "United Kingdom"}]


def test_the_hirebase_catalog_names_the_seniority_filter_and_where_the_match_threshold_goes():
    from pathlib import Path

    import yaml

    rows = {e["id"]: e for e in yaml.safe_load(
        (Path(__file__).parents[1] / "src" / "treg" / "catalog" / "hirebase.yaml").read_text())["endpoints"]}
    for eid in ("hirebase.jobs.search", "hirebase.jobs.salary.insights"):
        assert "experience_levels" in rows[eid]["input"]["body"]
    match = rows["hirebase.jobs.match"]["input"]["body"]
    # Hirebase answers 422 to a top-level score_threshold; it belongs inside `vector`.
    assert "score_threshold" not in match and "score_threshold" in match["vector"]["note"]
