"""The routed job search must filter remote the same way whichever provider serves it.

`companies.jobs.search` routes among Hirebase, JobsPipe, TheirStack and MobiusEngine. A caller
can ask for remote work two ways: `remote=true`, or the place `location="Remote"`. Hirebase
already normalised the place to its remote filter (its geocoder cannot place "Remote"); the
other three did not, so the *same* routed query filtered remote on Hirebase but sent "Remote"
as a literal place (JobsPipe, MobiusEngine) or dropped it (TheirStack) depending on which
provider answered. These tests lock the normalised behaviour across all four: `location="Remote"`
and `remote=true` produce the provider's remote filter, "Remote" is never sent as a place, and a
real place or country still filters normally.
"""
from __future__ import annotations

import pytest

from treg.domain.catalog import store as catalog_store

# endpoint -> (where the remote filter lands, its value, the place field that must stay absent)
REMOTE_FILTER = {
    "hirebase.jobs.search": ("body", "location_types", ["Remote"], "geo_locations"),
    "jobspipe.jobs.search": ("body", "remote", True, "job_location_or"),
    "theirstack.jobs.search": ("body", "workplace_types_or", ["remote"], None),
    "mobiusengine-jobs.jobs.search": ("query", "work_mode", "Remote Solely", "locations"),
}


def _upstream(ep: str, ident: dict):
    ad = catalog_store.load().adapters[ep]
    query, body = ad.to_upstream(ident, ("title",))
    return query, body


@pytest.mark.parametrize("ep", list(REMOTE_FILTER))
@pytest.mark.parametrize("remote_ask", [{"remote": True}, {"location": "Remote"}, {"location": "remote"}])
def test_remote_is_filtered_the_same_way_on_every_provider(ep, remote_ask):
    where, field, value, place_field = REMOTE_FILTER[ep]
    query, body = _upstream(ep, {"title": "support engineer", "limit": 10, **remote_ask})
    got = body if where == "body" else query
    assert got.get(field) == value, f"{ep}: {remote_ask} must set {field}={value!r}, got {got!r}"
    if place_field:
        assert place_field not in got, f"{ep}: 'Remote' must not be sent as a literal place ({place_field})"


@pytest.mark.parametrize("ep", list(REMOTE_FILTER))
def test_a_real_place_is_not_turned_into_a_remote_filter(ep):
    query, body = _upstream(ep, {"title": "support engineer", "location": "Berlin", "limit": 10})
    where, field, _, place_field = REMOTE_FILTER[ep]
    got = body if where == "body" else query
    assert field not in got, f"{ep}: a real place must not enable the remote filter"
    # Providers that take a city place keep it; TheirStack maps country only (no city field).
    if ep == "jobspipe.jobs.search":
        assert body["job_location_or"] == ["Berlin"]
    elif ep == "mobiusengine-jobs.jobs.search":
        assert query["locations"] == "Berlin"
    elif ep == "hirebase.jobs.search":
        assert body["geo_locations"] == [{"city": "Berlin"}]


@pytest.mark.parametrize("ep", list(REMOTE_FILTER))
def test_a_country_still_filters(ep):
    query, body = _upstream(ep, {"title": "support engineer", "country": "RO", "limit": 10})
    if ep == "hirebase.jobs.search":
        assert body["geo_locations"] == [{"country": "Romania"}]
    elif ep == "jobspipe.jobs.search":
        assert body["job_country_code_or"] == ["RO"]
    elif ep == "theirstack.jobs.search":
        assert body["job_country_code_or"] == ["RO"]
    elif ep == "mobiusengine-jobs.jobs.search":
        assert query["countries"] == "RO"
