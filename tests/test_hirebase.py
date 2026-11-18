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
