"""The catalog's size on the front door is generated, never typed (`catalog_store.headline_counts`)."""
from treg.domain.catalog import store as catalog_store


def test_headline_counts_round_down_and_skip_routed():
    cat = catalog_store.Catalog()
    for i in range(3218):
        cat.by_id[f"p{i % 69}.e{i}"] = {"id": f"p{i % 69}.e{i}", "provider": f"p{i % 69}"}
    for i in range(75):
        cat.by_id[f"treg.r{i}"] = {"id": f"treg.r{i}", "provider": "treg", "kind": "routed"}
    assert catalog_store.headline_counts(cat) == ("3,200+", 69)


async def test_served_front_door_fills_the_counts(clients):
    endpoints, providers = catalog_store.headline_counts(catalog_store.load())
    for path in ("/skill.md", "/llms.txt"):
        text = (await clients.get(path)).text
        assert "{ENDPOINTS}" not in text and "{PROVIDERS}" not in text, path
        assert f"{endpoints} " in text and f"across {providers} providers" in text, path
    listing = (await clients.get("/.well-known/skill.md")).text
    assert "{ENDPOINTS}" not in listing
