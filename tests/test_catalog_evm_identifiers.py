"""Public EVM targets must not trip the catalog credential detector."""
from scripts.catalog_validate import looks_like_secret


def test_evm_target_is_public_but_bearer_and_opaque_keys_are_rejected():
    address = "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9"
    assert not looks_like_secret(address)
    assert looks_like_secret("Bearer " + address)
    assert looks_like_secret("a5b6c7d8e9f0" * 4)
    assert looks_like_secret(address + "aa")


async def test_nansen_balance_reads_only_the_credit_balance():
    import httpx
    from treg.domain.capacity.collectors import _nansen

    def account(request):
        assert str(request.url) == "https://api.nansen.ai/api/v1/account"
        assert request.headers["apikey"] == "test-key"
        return httpx.Response(200, json={"credits_remaining": 2100, "plan": "pro"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(account)) as client:
        assert await _nansen(client, "test-key") == {"value": 2100, "unit": "credits", "note": ""}
