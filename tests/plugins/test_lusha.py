import pytest

from treg.domain.plugins import Request, lusha


def _req(body):
    return Request(method="POST", query={}, body=body)


@pytest.mark.parametrize(("body", "credits"), [
    ({"ids": ["a"], "reveal": ["emails"]}, 1),
    ({"ids": ["a"], "reveal": ["phones"]}, 5),
    ({"ids": ["a", "b"], "reveal": ["emails", "phones"]}, 12),
    ({"ids": ["a", "b", "c"]}, 18),
    ({"ids": ["a"], "reveal": []}, 6),
    ({"ids": ["a"], "reveal": ["fax"]}, 6),
    ({"ids": ["a"], "reveal": ["emails", "emails"]}, 1),
    ({"ids": [], "reveal": ["emails"]}, 1),
    ({"reveal": ["phones"]}, 5),
    (None, 6),
])
def test_contact_reveal_credits_holds_each_id_at_its_reveal(body, credits):
    assert lusha.contact_reveal_credits(_req(body)) == credits


@pytest.mark.parametrize(("body", "units"), [
    ({"contacts": [{}], "reveal": ["emails"]}, 1),
    ({"contacts": [{}], "reveal": ["phones"]}, 3),
    ({"contacts": [{}, {}], "reveal": ["emails", "phones"]}, 7),
    ({"contacts": [{}]}, 3.5),
    ("not json", 3.5),
])
def test_contact_enrich_units_adds_the_match_in_two_credit_units(body, units):
    assert lusha.contact_enrich_units(_req(body)) == units


@pytest.mark.parametrize(("body", "contacts"), [
    ({"companies": [{"domain": "a"}], "contactsLimit": 2}, 2),
    ({"companies": [{"domain": "a"}] * 25, "contactsLimit": 3}, 75),
    ({"companies": [{"domain": "a"}]}, 60),
    ({"companies": [{"domain": "a"}] * 2}, 100),
    ({"companies": [{"domain": "a"}] * 25}, 100),
    ({"companies": [{"domain": "a"}], "contactsLimit": 20, "pagination": {"page": 0, "size": 10}}, 10),
    ({"companies": [{"domain": "a"}] * 25, "contactsLimit": 60, "pagination": {"size": 40}}, 40),
    ({"companies": [{"domain": "a"}] * 25, "pagination": {"size": 500}}, 100),
    ({"companies": [{"domain": "a"}] * 25, "pagination": {"size": True}}, 100),
    ({"companies": [{"domain": "a"}] * 2, "contactsLimit": 0}, 100),
    ({"companies": [{"domain": "a"}], "contactsLimit": True}, 60),
    ({"companies": [{"domain": "a"}], "contactsLimit": "5"}, 60),
    ({"contactsLimit": 4}, 4),
    (None, 60),
])
def test_buying_group_contacts_holds_companies_times_limit_up_to_one_page(body, contacts):
    assert lusha.buying_group_contacts(_req(body)) == contacts


@pytest.mark.parametrize(("body", "units"), [
    ({"companies": [{"domain": "a"}]}, 1),
    ({"companies": [{"domain": "a"}] * 2}, 1.5),
    ({"companies": [{"domain": "a"}], "reveal": ["competitors"]}, 1.5),
    ({"companies": [{"domain": "a"}], "reveal": ["competitors", "competitors", "fax"]}, 1.5),
    ({"companies": [{"domain": "a"}] * 26}, 14),
    ({"companies": [{"domain": "a"}] * 100, "reveal": sorted(lusha._PREMIUM)}, 652),
    ({"companies": []}, 1),
    ("not json", 1),
])
def test_company_enrich_units_adds_a_search_block_per_25_and_each_premium_field(body, units):
    assert lusha.company_enrich_units(_req(body)) == units


@pytest.mark.parametrize(("body", "credits"), [
    ({"ids": ["a"]}, 1),
    ({"ids": ["a", "b"], "reveal": ["competitors", "directParent"]}, 6),
    ({"ids": ["a"] * 100, "reveal": sorted(lusha._PREMIUM)}, 1300),
    ({"ids": ["a"], "reveal": "competitors"}, 1),
    (None, 1),
])
def test_company_reveal_credits_holds_each_id_and_its_premium_fields(body, credits):
    assert lusha.company_reveal_credits(_req(body)) == credits
