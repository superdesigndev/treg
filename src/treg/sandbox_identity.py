"""Shared, deterministic identities for the landing sandbox and its public feed."""

import hashlib


# Keep these lists in sync with LIVE_ADJ/LIVE_ANIMAL in web/landing.html.
ADJECTIVES = (
    "swift", "brave", "calm", "clever", "cosmic", "daring", "eager", "fuzzy",
    "gentle", "golden", "happy", "jolly", "lucky", "mellow", "mighty", "neon",
    "nifty", "plucky", "proud", "quick", "shiny", "snappy", "solar", "sunny",
)
ANIMALS = (
    "otter", "fox", "lynx", "panda", "koala", "falcon", "heron", "badger",
    "dolphin", "gecko", "ibis", "jaguar", "kiwi", "lemur", "marmot", "narwhal",
    "ocelot", "puffin", "quokka", "raven", "seal", "tapir", "walrus", "wombat",
)


def visitor_name(seed: str) -> str:
    """Derive one stable adjective-animal-number identity from an opaque seed."""
    h = int(hashlib.sha256((seed or "").encode()).hexdigest(), 16)
    return f"{ADJECTIVES[h % len(ADJECTIVES)]}-{ANIMALS[(h // 100) % len(ANIMALS)]}-{h % 1000}"
