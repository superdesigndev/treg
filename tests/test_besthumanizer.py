"""BestHumanizer: scoring is free, a rewrite draws pasted words from the monthly balance and settles
at the reported words_debited, one credit per word (fx.yaml, Pro plan). Observed live 2026-11-19;
see src/treg/catalog/besthumanizer.yaml."""
from __future__ import annotations

from treg.domain.catalog import store as cs


def _rows():
    cat = cs.load()
    return cat, [e for e in cat.endpoints if e["provider"] == "besthumanizer"]


def test_every_besthumanizer_route_but_the_account_is_platform_eligible():
    cat, eps = _rows()
    assert eps
    for ep in eps:
        if ep.get("scope") == "own_account":
            assert not cat.platform_eligible(ep), ep["id"]
            continue
        assert cat.platform_eligible(ep), ep["id"]


def test_a_rewrite_is_priced_per_word_and_settles_at_words_debited():
    cat, _ = _rows()
    view = cat.cost_view(cat.by_id["besthumanizer.text.humanize"]["cost"], "besthumanizer")
    assert view is not None and view["usd"] > 0
    assert view["reported_charge"]["path"] == "words_debited"
