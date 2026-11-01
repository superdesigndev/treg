"""Only quota-admitted upstream attempts enter the provider rate observation."""

import pytest

from treg.application.call import service as call_service

from test_capacity_rate_observe import _counts, emitted, observing  # noqa: F401
from test_capacity_smoothing import _publish_rate, _relay_script
from test_marketplace_call import EP, EP_MICRO, _balance, platform_on  # noqa: F401
from test_org_endpoint_quota_call import _assert_no_money, _enable


@pytest.mark.parametrize("limit,unavailable,status", [(0, False, 429), (1, False, 200), (0, True, 200)])
async def test_rate_observation_follows_quota_admission(
    clients, monkeypatch, observing, emitted, limit, unavailable, status,
):
    verdicts, asked = observing
    _enable(monkeypatch, {EP: limit}, unavailable=unavailable)
    await _publish_rate("tikhub", 20, 60.0)
    verdicts.append("over")
    sent = []
    monkeypatch.setattr(call_service, "relay", _relay_script([(200, (), b'{"ok":true}')], sent))
    before = await _balance(clients)
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == status, response.text
    if status == 429:
        assert response.json()["detail"]["error"] == "org_endpoint_quota_exceeded"
        assert asked == emitted == sent == []
        assert await _balance(clients) == before
        await _assert_no_money()
    else:
        assert len(sent) == 1
        assert asked == [("tikhub", 20, 60.0)]
        assert len(emitted) == 1 and _counts(emitted[0]) == {"over_2xx": 1}
        assert before - await _balance(clients) == EP_MICRO
