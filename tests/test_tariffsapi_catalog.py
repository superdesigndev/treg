"""TariffsAPI listing: every endpoint is priced as one call that fx.yaml converts to USD. The two routes the
vendor's pricing page counts as calls (resolve and batch) are platform-eligible; the two it does not
mention (HTS lookup and schedule search) carry an inferred price and stay on a team's own key until the
vendor confirms how they meter. A batch of up to 200 lines is still one call."""
from treg import oauth_providers as P
from treg.domain.catalog import store as cs


def test_tariffsapi_is_a_registered_key_provider():
    p = P.REGISTRY["tariffsapi"]
    assert p.auth_kind == "key"
    assert p.token_header == "Authorization" and p.token_format == "Bearer {secret}"
    assert p.probe_path == "/api_keys"


def test_tariffsapi_platform_eligibility_follows_the_documented_meter():
    c = cs.load()
    eps = [ep for ep in c.by_id.values() if ep.get("provider") == "tariffsapi"]
    assert len(eps) == 4
    eligible = {"tariffsapi.tariffs.resolve", "tariffsapi.tariffs.resolve_batch"}
    for ep in eps:
        assert ep["platform"] == "trade", ep["id"]
        assert c.platform_eligible(ep) == (ep["id"] in eligible), ep["id"]


def test_a_batch_is_one_call_whatever_its_size():
    c = cs.load()
    batch = c.by_id["tariffsapi.tariffs.resolve_batch"]
    assert batch["method"] == "POST"
    assert batch["input"]["body"]["requests"]["required"] is True
    single = c.by_id["tariffsapi.tariffs.resolve"]
    assert batch["cost"]["value"] == single["cost"]["value"] == 1
    assert batch["cost"]["currency"] == single["cost"]["currency"] == "credit"
