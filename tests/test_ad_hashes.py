"""The ad-platform email hashes, against the platforms' own published examples."""

import pytest

from treg.domain.catalog.routing import ad_hashes


def test_meta_published_examples():
    # developers.facebook.com: Custom Audiences hashing table and Conversions API `em` example.
    assert ad_hashes.hashes("mary@example.com")["meta"] == \
        "f1904cf1a9d73a55fa5de0ac823c4403ded71afd4c3248d00bdcd0866552bb79"
    assert ad_hashes.hashes("John_Smith@gmail.com")["meta"] == \
        "62a14e44f765419d10fea99367361a727c12365e2520f32218d505ed9aa0f62f"


@pytest.mark.parametrize(("given", "google"), [
    # developers.google.com Data Manager formatting and Customer Match get-started examples.
    ("cloudy.sanfrancisco+shopping@gmail.com", "cloudysanfrancisco@gmail.com"),
    ("user.name+NYC@Example.com", "user.name+nyc@example.com"),
    ("Jane.Doe+Shopping@googlemail.com", "janedoe@googlemail.com"),
])
def test_google_published_normalization(given, google):
    assert ad_hashes.normalize(given, "google") == google


def test_google_hash_follows_its_normalization():
    h = ad_hashes.hashes("cloudy.sanfrancisco+shopping@gmail.com")
    assert h["google"] == "223ebda6f6889b1494551ba902d9d381daf2f642bae055888e96343d53e9f9c4"
    assert h["meta"] == h["linkedin"] == "8dd4a9bf850242873c14976125edabc35096192b784626a5641a7d2de56e3c1e"


def test_a_work_address_hashes_the_same_everywhere():
    h = ad_hashes.hashes("  Mary@Example.com ")
    assert set(h) == {"linkedin", "meta", "google"}
    assert len(set(h.values())) == 1 and h["meta"] == ad_hashes.hashes("mary@example.com")["meta"]


@pytest.mark.parametrize("bad", ["", "mary", "@example.com", "mary@", None, 42, "+@gmail.com", "..@gmail.com"])
def test_not_an_address_gives_no_hashes(bad):
    assert ad_hashes.hashes(bad) is None


def test_an_unknown_platform_is_refused():
    with pytest.raises(ValueError):
        ad_hashes.normalize("mary@example.com", "tiktok")
