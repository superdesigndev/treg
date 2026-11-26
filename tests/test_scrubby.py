from __future__ import annotations

import json

import pytest

from treg.application.call import settle as call_settle
from treg.application.call.resolve import MarketplaceCall


def _mk(*, endpoint_id="scrubby.people.email.verify", unit_micro=8_000):
    return MarketplaceCall(
        tool=None, upstream="https://api.scrubby.io/validate_email", consumed=set(),
        provider="scrubby", endpoint_id=endpoint_id, tier="platform",
        estimate_micro=unit_micro, cost_type="per_success", unit_micro=unit_micro,
        reported_charge_unit_micro=8_000, request_data={},
    )


@pytest.mark.parametrize("credits,expected", [(0, 0), (3, 24_000), (-1, None), (True, None), (1.5, None)])
def test_scrubby_settles_from_reported_credits(credits, expected):
    body = json.dumps({"result": "Valid", "credits_used": credits}).encode()
    assert call_settle._observed_cost_micro(_mk(), body) == expected


