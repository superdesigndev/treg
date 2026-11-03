"""Public EVM targets must not trip the catalog credential detector."""
from scripts.catalog_validate import looks_like_secret


def test_evm_target_is_public_but_bearer_and_opaque_keys_are_rejected():
    address = "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9"
    assert not looks_like_secret(address)
    assert looks_like_secret("Bearer " + address)
    assert looks_like_secret("a5b6c7d8e9f0" * 4)
    assert looks_like_secret(address + "aa")
