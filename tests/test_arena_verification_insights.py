"""Synthetic aggregates only; production samples are never fixtures."""
import copy
import json

import pytest
from pydantic import ValidationError

from treg.application import arena_insights
from treg.application.arena_verification_insights import build_snapshot, publish_snapshot
from treg.infra.db import session_maker


def aggregate():
    return dict(version=1, run_id="synthetic-audit", baseline_since="2026-01-01T00:00:00Z",
                baseline_until="2026-01-03T00:00:00Z", sample_since="2026-01-01T00:00:00Z",
                sample_until="2026-01-04T00:00:00Z", checked_at="2026-01-05T00:00:00Z",
                rows=[dict(task="people.email.find", endpoint="hunter.people.email.find", input="name_domain",
                           baseline_rate=40, baseline_n=200, sample_n=40, passed_n=30,
                           method="email_verifier_consensus", verifiers=["leadmagic", "millionverifier"])])


def test_projection_thresholds_and_phone_does_not_imply_ownership():
    data = aggregate()
    row = build_snapshot(data)["rows"][0]
    assert row["rate"] == 30 and row["estimate"] and row["small_sample"]
    data["rows"][0]["passed_n"] = 0
    assert build_snapshot(data)["rows"][0]["rate"] == 0
    data["rows"][0]["sample_n"] = 19
    assert build_snapshot(data)["rows"][0]["rate"] is None
    data["rows"][0].update(sample_n=40, baseline_n=19)
    assert build_snapshot(data)["rows"][0]["rate"] is None
    data["rows"][0].update(task="people.phone.find", endpoint="tomba.people.phone.find", input="email",
                           baseline_n=200, passed_n=40, method="phone_format", verifiers=["tomba"])
    assert build_snapshot(data)["rows"][0]["rate"] is None


@pytest.mark.parametrize("change", [
    lambda d: d.update(raw_response={"email": "private@example.test"}),
    lambda d: d["rows"][0].update(contact="private@example.test"),
    lambda d: d["rows"][0].update(passed_n=41),
    lambda d: d["rows"][0].update(unresolved_n=11),
    lambda d: d["rows"][0].update(baseline_rate=float("nan")),
    lambda d: d["rows"][0].update(endpoint="tomba.people.phone.find"),
    lambda d: d["rows"][0].update(verifiers=["leadmagic", "leadmagic"]),
    lambda d: d["rows"][0].update(method="phone_format"),
    lambda d: d.update(sample_until="2026-01-06T00:00:00Z"),
    lambda d: d["rows"].append(copy.deepcopy(d["rows"][0])),
])
def test_reject_private_content_and_invalid_aggregates(change):
    data = aggregate()
    change(data)
    with pytest.raises(ValidationError):
        build_snapshot(data)


async def test_published_snapshot_survives_observation_refresh_and_reimport(clients):
    data = aggregate()
    assert await publish_snapshot(data, session_maker)
    assert not await publish_snapshot(data, session_maker)
    before = (await arena_insights.public_snapshot(session_maker))["verification"]
    assert before["rows"][0]["rate"] == 30
    await arena_insights.collect_batch(session_maker)
    after = (await arena_insights.public_snapshot(session_maker))["verification"]
    assert before == after
    assert "private@example.test" not in json.dumps(after)
    data["rows"][0]["passed_n"] = 20
    with pytest.raises(ValueError, match="different contents"):
        await publish_snapshot(data, session_maker)


async def test_empty_database_has_no_verification_rates(clients):
    assert (await arena_insights.public_snapshot(session_maker))["verification"] is None


def test_validity_uses_completed_returned_emails_not_lookup_baseline():
    data = aggregate()
    row = build_snapshot(data)["rows"][0]
    assert row["validity_rate"] == 75 and row["checked_n"] == 40
    data["rows"][0].update(baseline_rate=None, baseline_n=0)
    assert build_snapshot(data)["rows"][0]["validity_rate"] == 75
    data["rows"][0].update(unresolved_n=10)
    assert build_snapshot(data)["rows"][0]["validity_rate"] == 100
    data["rows"][0].update(passed_n=0)
    assert build_snapshot(data)["rows"][0]["validity_rate"] == 0
    data["rows"][0].update(unresolved_n=21)
    assert build_snapshot(data)["rows"][0]["validity_rate"] is None
    data["rows"][0].update(task="people.phone.find", endpoint="tomba.people.phone.find",
                           method="phone_format", verifiers=["tomba"], unresolved_n=0)
    assert build_snapshot(data)["rows"][0]["validity_rate"] is None


def test_phone_format_validity_has_a_separate_completed_check_denominator():
    data = aggregate()
    data["rows"][0].update(task="people.phone.find", endpoint="tomba.people.phone.find",
                           method="phone_format", verifiers=["tomba"], unresolved_n=5)
    row = build_snapshot(data)["rows"][0]
    assert row["format_validity_rate"] == 85.71
    assert row["validity_rate"] is None and row["rate"] is None
    data["rows"][0].update(passed_n=0, unresolved_n=21)
    assert build_snapshot(data)["rows"][0]["format_validity_rate"] is None
    data["rows"][0]["unresolved_n"] = 40
    assert build_snapshot(data)["rows"][0]["format_validity_rate"] is None
    data["rows"][0]["unresolved_n"] = 0
    assert build_snapshot(data)["rows"][0]["format_validity_rate"] == 0
    assert "format_validity_rate" not in build_snapshot(aggregate())["rows"][0]
