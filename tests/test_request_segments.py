"""Request segments: the categories a people search is recorded and routed by, never its text."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from treg.application import call_events as ev
from treg.application.call import route_rules as reader_app
from treg.domain.catalog import store as catalog_store
from treg.domain.catalog.routing import contracts as C
from treg.domain.catalog.routing import rules as R
from treg.domain.catalog.routing import segments as S


@pytest.mark.parametrize("title, cls, fn, sen, lang", [
    ("", "none", "none", "none", "none"),
    (None, "none", "none", "none", "none"),
    ("   ", "none", "none", "none", "none"),
    ("CEO", "leader", "executive", "c_level_founder", "en"),
    ("Co-Founder", "leader", "executive", "c_level_founder", "en"),
    ("Managing Partner", "leader", "executive", "c_level_founder", "en"),
    ("Geschäftsführer", "leader", "executive", "c_level_founder", "non_en"),
    ("Directeur Général", "leader", "executive", "c_level_founder", "non_en"),
    ("Amministratore Delegato", "leader", "executive", "c_level_founder", "non_en"),
    ("Director General", "leader", "executive", "c_level_founder", "en"),
    ("VP Sales", "function", "sales", "vp", "en"),
    ("Head of Marketing", "function", "marketing", "head_director", "en"),
    ("Senior Software Engineer", "function", "engineering", "individual", "en"),
    ("Chief Financial Officer", "function", "finance", "c_level_founder", "en"),
    ("CTO", "function", "engineering", "c_level_founder", "en"),
    ("Responsabile Vendite", "function", "sales", "manager_lead", "non_en"),
    ("Leiter Vertrieb", "function", "sales", "head_director", "non_en"),
    ("Jefe de Ventas", "function", "sales", "individual", "non_en"),
    ("Head of Tax", "function", "finance", "head_director", "en"),
    ("HR Business Partner", "function", "hr_talent", "c_level_founder", "en"),
    ("Partnerships Manager", "other", "other", "manager_lead", "en"),
    ("Astronaut", "other", "other", "individual", "en"),
    ("高级产品经理", "other", "other", "individual", "non_en"),
    ("CEO, Founder", "multi", "executive", "c_level_founder", "en"),
    ("CTO / VP Engineering", "multi", "engineering", "c_level_founder", "en"),
    ("CMO or Head of Growth", "multi", "marketing", "c_level_founder", "en"),
    ("Sales | Marketing", "multi", "sales", "individual", "en"),
    (["VP Sales", "Head of Sales"], "multi", "sales", "vp", "en"),
])
def test_a_title_falls_in_one_class_function_seniority_and_language(title, cls, fn, sen, lang):
    assert S.title_class(title) == cls
    assert S.title_function(title) == fn
    assert S.title_seniority(title) == sen
    assert S.lang(title) == lang
    assert {cls} <= S.TITLE_CLASSES and fn in S.FUNCTIONS and sen in S.SENIORITIES


@pytest.mark.parametrize("country, iso2, case", [
    ("us", "US", "lower"), ("US", "US", "upper"), ("United States", "US", "name"),
    ("Germany", "DE", "name"), ("uk", "GB", "lower"), ("USA", "US", "upper"), ("Us", "US", "other"),
    ("Atlantis", None, "name"), ("12", None, "other"), (None, None, "none"), ("", None, "none"),
])
def test_a_country_is_recorded_as_its_code_and_how_it_was_spelled(country, iso2, case):
    assert S.iso2(country) == iso2
    assert S.country_case(country) == case


@pytest.mark.parametrize("location, kind", [
    ("Germany", "country_name"), ("United States", "country_name"), ("USA", "country_name"),
    ("Oregon, United States", "region"), ("Greater Phoenix", "region"), ("EMEA", "region"),
    ("Bay Area", "region"), ("Paris", "city"), ("London, United Kingdom", "city"),
    ("Austin, TX", "city"), ("Springfield", "unknown"), ("", "none"), (None, "none"),
])
def test_a_location_is_recorded_as_its_kind(location, kind):
    assert S.location_kind(location) == kind


def test_limit_and_q_buckets():
    assert [S.limit_bucket(n) for n in (1, 5, 6, 10, 11, 25, 26, 100)] == \
        ["<=5", "<=5", "6-10", "6-10", "11-25", "11-25", ">25", ">25"]
    assert S.limit_bucket(None) == S.limit_bucket(True) == S.limit_bucket("many") == "none"
    q = S.people_search
    assert q({"q": "fintech founders"})["q_words"] == "1-3"
    assert q({"q": "B2B sales coaches with a mostly US audience"})["q_words"] == "4-8"
    assert q({"q": "people who run growth at seed stage developer tools companies in Europe"})["q_words"] == "9+"
    assert q({"q": "fintech founders in Berlin"})["q_has_place"] == "true"
    assert q({"q": "specialty coffee roasters, New York"})["q_has_place"] == "true"
    assert q({"q": "fintech founders"})["q_has_place"] == "false"
    # a country that is also a given name is no place
    assert q({"q": "Jordan Smith designer"})["q_has_place"] == "false"
    assert q({"q": "fondateurs de startups à Lyon"})["q_lang"] == "non_en"
    assert q({})["q_words"] == q({})["q_lang"] == q({})["q_has_place"] == "none"


def _contract():
    return catalog_store.load().contracts["people.search"]


def test_segment_records_every_category_and_no_raw_value():
    given = {"title": "Geschäftsführer", "company_domain": "acme.example.de", "country": "de",
             "location": "Bavaria", "limit": 30, "keywords": ["saas"]}
    _, meta, _ = ev.segment(_contract(), given)
    assert meta == {
        "domain_class": "company", "tld": "de", "country": "de", "country_iso2": "DE",
        "country_raw_case": "lower", "has_country": "true", "title_class": "leader",
        "title_function": "executive", "title_seniority": "c_level_founder", "title_lang": "non_en",
        "title_words": "1-2", "has_location": "true", "location_kind": "region",
        "limit_bucket": ">25", "q_words": "none", "q_lang": "none", "q_has_place": "none",
        "name_given": "false", "has_keywords": "true", "segments_version": S.PEOPLE_SEARCH.version,
    }
    named = ev.segment(_contract(), {"full_name": "Ann Lee", "q": "ann lee at acme in Paris"})[1]
    for raw in ("geschäftsführer", "bavaria", "acme", "ann", "paris", "saas"):
        assert raw not in json.dumps([meta, named]).lower()
    # every category recorded is one its vocabulary allows, so a rule can match it
    checks = ev.segment_checks(_contract())
    for m in (meta, named):
        assert all(checks[k](v) for k, v in m.items() if k != "segments_version")
    assert "segments_version" not in checks
    assert named["name_given"] == "true" and named["limit_bucket"] == "6-10"  # the contract default


def test_a_contract_without_a_segment_set_records_none():
    _, meta, _ = ev.segment(catalog_store.load().contracts["people.email.find"],
                            {"full_name": "Ann Lee", "domain": "acme.example"})
    assert set(meta) == {"domain_class", "tld"}


def test_a_contract_naming_an_unknown_segment_set_does_not_load():
    contract = {"identity": [{"q": "str"}], "output": {"rows": {"type": "list"}}, "miss": "rows == []",
                "segments": "nope"}
    with pytest.raises(ValueError, match="segments names one of"):
        C.parse_contracts({"contracts": {"x.search": contract}})


def _envelope(*rules):
    return json.dumps({"version": 1, "config": {"schema_version": 1, "enabled": True, "rules": list(rules)}})


def _rule(match, rid="r"):
    return {"id": rid, "capability": "people.search", "match": match, "order": ["crawl4ai"]}


def test_rules_match_the_new_keys_and_refuse_words_outside_the_vocabulary():
    contracts = catalog_store.load().contracts

    def check(*rules):
        R.check_against(R.parse(_envelope(*rules)), contracts, reader_app._servers_of, ev.segment_checks)

    check(_rule({"title_class": "leader"}), _rule({"title_function": "marketing", "title_seniority": "vp"}, "b"),
          _rule({"has_country": "true", "country_iso2": "US", "country_raw_case": "lower"}, "c"),
          _rule({"title_lang": "non_en", "limit_bucket": ">25"}, "d"))
    for match, why in (({"title_class": "ceo"}, "never records title_class=ceo"),
                       ({"title_function": "Marketing"}, "never records title_function"),
                       ({"has_country": "yes"}, "never records has_country"),
                       ({"country_iso2": "us"}, "never records country_iso2"),
                       ({"country_iso2": "ZZ"}, "never records country_iso2"),
                       ({"title_mood": "happy"}, "records no title_mood")):
        with pytest.raises(ValueError, match=why):
            check(_rule(match))
    # a capability with no segment set records none of these keys
    with pytest.raises(ValueError, match="records no title_class"):
        check({"id": "f", "capability": "people.email.find", "match": {"title_class": "leader"},
               "order": ["tomba"]})


def test_the_most_specific_title_rule_wins_on_the_recorded_segment():
    parsed = R.parse(_envelope(_rule({"title_class": "leader"}, "leader"),
                               _rule({"title_class": "function", "title_function": "marketing"}, "mkt"),
                               _rule({"has_country": "true"}, "country")))
    seg = lambda given: {**(s := ev.segment(_contract(), given))[1], "input_type": s[0]}  # noqa: E731
    assert R.match(parsed, "people.search", seg({"title": "Founder", "company_domain": "a.example"})).id == "leader"
    assert R.match(parsed, "people.search", seg({"title": "Head of Marketing", "company_domain": "a.example",
                                                 "country": "us"})).id == "mkt"
    assert R.match(parsed, "people.search", seg({"company_domain": "a.example", "country": "US"})).id == "country"
    assert R.match(parsed, "people.search", seg({"company_domain": "a.example"})) is None


# ---- answer side -------------------------------------------------------------------------------

_ROWS = [
    {"name": "Ann Lee", "title": "VP Sales", "company": "Acme", "company_domain": "acme.example",
     "location": "Austin, Texas, United States", "profile_url": "https://www.linkedin.com/in/ann-lee"},
    # the same person again, by profile
    {"name": "Ann Lee", "title": "VP Sales", "company": "Acme Inc",
     "profile_url": "https://linkedin.com/in/ann-lee/"},
    {"name": "Bo Chen", "title": "Head of Sales EMEA", "company": "Acme, Inc.",
     "location": "Berlin, Germany", "profile_url": "linkedin.com/in/bo-chen"},
    {"name": "Cy Diaz", "title": "Marketing Manager", "company": "Other Co", "company_domain": "other.example",
     "location": "San Francisco, CA"},
    # the same person again, by name and company
    {"name": "cy  diaz", "title": "Marketing Manager", "company": "Other Co"},
]


def test_answer_counts_judge_rows_against_the_question():
    asked = {"company_domain": "acme.example", "title": "Head of Sales", "country": "us"}
    assert S.people_search_answer(asked, {"people": _ROWS}) == {
        "rows": "5", "rows_with_profile_url": "2", "rows_dup": "2", "rows_company_match": "3",
        "rows_title_match": "3", "rows_country_match": "1"}
    # a leader asked: a row matches when it is a leader too
    assert S.people_search_answer({"title": "Founder"}, {"people": [
        {"title": "Co-founder & CEO"}, {"title": "CTO"}, {"title": "Geschäftsführer"}]})["rows_title_match"] == "2"
    # checks the question gives nothing to judge by are absent, not zero
    assert set(S.people_search_answer({"q": "fintech founders"}, {"people": []})) == \
        {"rows", "rows_with_profile_url", "rows_dup"}
    assert set(S.people_search_answer({"title": "Astronaut"}, {"people": _ROWS})) == \
        {"rows", "rows_with_profile_url", "rows_dup"}
    assert S.people_search_answer(asked, {"error": "x"}) == {}


@pytest.mark.parametrize("location, code", [
    ("Austin, Texas, United States", "US"), ("Austin, Texas", "US"), ("Berlin, Germany", "DE"),
    ("San Francisco, CA", None), ("London, UK", "GB"), ("Paris", None), ("", None), (None, None),
])
def test_a_row_location_names_its_country_by_name_never_by_a_two_letter_part(location, code):
    assert S.location_country(location) == code


def test_answer_columns_record_counts_with_the_version_and_no_row_value():
    asked = {"company_domain": "acme.example", "title": "Head of Sales", "country": "US"}
    values = ev.answer_columns(_contract(), {"people": _ROWS}, {"company_domain": "acme.example"},
                               asked)["answer_values"]
    assert values["rows"] == "5" and values["rows_company_match"] == "3"
    assert values["segments_version"] == S.PEOPLE_SEARCH.version
    dumped = json.dumps(values).lower()
    for raw in ("ann", "acme", "berlin", "linkedin", "sales"):
        assert raw not in dumped
    # without the question there is nothing to count against
    assert ev.answer_columns(_contract(), {"people": _ROWS})["answer_values"] is None
    # a contract with no segment set counts nothing
    assert ev.answer_columns(catalog_store.load().contracts["people.email.find"], {"email": None},
                             None, asked)["answer_values"] is None


def test_a_direct_answer_counts_every_mapped_row_not_only_the_arenas_first_few():
    rows = [{"name": f"P{i}", "title": "Engineer", "profile_url": f"https://www.linkedin.com/in/p{i}"}
            for i in range(40)]
    adapter = SimpleNamespace(verified=True, from_upstream=lambda doc: {"people": doc["rows"]})
    core = ev.adapter_output(adapter, "people.search", 200, json.dumps({"rows": rows}).encode())
    assert len(core["people"]) == 40 and core["people"][0]["profile_url"]
    counts = ev.answer_columns(_contract(), core, None, {"title": "Software Engineer"})["answer_values"]
    assert counts["rows"] == counts["rows_with_profile_url"] == counts["rows_title_match"] == "40"
