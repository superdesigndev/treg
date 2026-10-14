"""Synthetic-only regression coverage for routed work-email identity checks."""

from copy import deepcopy

import pytest

from treg.domain.catalog.routing.email_identity import email_identity_mismatch


IDENTITY = {"first_name": "Avery", "last_name": "Example", "domain": "employer.test"}
OUTPUT = {"email": "avery@example.test", "first_name": "Avery", "last_name": "Example"}


def test_matching_identity_is_accepted_without_mutating_evidence():
    identity = deepcopy(IDENTITY)
    output = {**OUTPUT, "email": "avery@employer.test"}
    before = deepcopy((identity, output))
    assert email_identity_mismatch(identity, output) is None
    assert (identity, output) == before


@pytest.mark.parametrize("domain", ["elsewhere.test", "team.employer.test", "www.employer.test"])
def test_different_employer_and_subdomains_are_not_equivalent(domain):
    assert email_identity_mismatch(IDENTITY, {"email": f"avery@{domain}"}) == "email_domain_mismatch"


def test_parent_domain_does_not_match_requested_subdomain():
    assert email_identity_mismatch({**IDENTITY, "domain": "www.employer.test"},
                                   {"email": "avery@employer.test"}) == "email_domain_mismatch"


@pytest.mark.parametrize("email", [
    None, 12, True, [], {}, "", "avery", "avery@@employer.test", "@employer.test",
    "avery@", "Avery <avery@employer.test>", "avery @employer.test",
    "avery@employer.test\n", "avery@employer.test.", ".avery@employer.test",
    "avery.@employer.test", "avery..example@employer.test", "avery@employer",
    "avery@-employer.test", "avery@employer-.test", "avery@employer_.test",
    "avery@[127.0.0.1]", '"avery"@employer.test', "avéry@employer.test",
    "a" * 65 + "@employer.test", "avery@" + "a" * 64 + ".test",
])
def test_invalid_email_syntax_is_rejected(email):
    assert email_identity_mismatch(IDENTITY, {"email": email}) == "email_invalid"


@pytest.mark.parametrize("domain", ["EMPLOYER.TEST", "https://EMPLOYER.TEST/about", "employer.test/", "employer.test."])
def test_requested_website_host_and_case_normalize(domain):
    assert email_identity_mismatch({**IDENTITY, "domain": domain},
                                   {"email": "Avery@Employer.TEST"}) is None


def test_idna_domain_spellings_match_without_accent_folding_the_domain():
    assert email_identity_mismatch({**IDENTITY, "domain": "https://büro.test/about"},
                                   {"email": "avery@xn--bro-hoa.test"}) is None
    assert email_identity_mismatch({**IDENTITY, "domain": "büro.test"},
                                   {"email": "avery@buro.test"}) == "email_domain_mismatch"


def test_case_accents_and_name_punctuation_are_normalized():
    identity = {**IDENTITY, "first_name": "Ávery-Jane", "last_name": "D’Exámple"}
    output = {"email": "avery@employer.test", "first_name": "AVERY JANE", "last_name": "dexample"}
    assert email_identity_mismatch(identity, output) is None


@pytest.mark.parametrize("field,value", [
    ("first_name", "Taylor"), ("last_name", "Other"), ("first_name", "Ave"),
    ("first_name", True), ("last_name", 42), ("first_name", ["Avery"]),
    ("last_name", {"name": "Example"}), ("first_name", "---"), ("last_name", " "),
])
def test_changed_names_and_invalid_typed_echoes_are_rejected(field, value):
    output = {**OUTPUT, "email": "avery@employer.test", field: value}
    assert email_identity_mismatch(IDENTITY, output) == f"{field}_mismatch"


@pytest.mark.parametrize("names", [{}, {"first_name": None}, {"last_name": ""},
                                    {"first_name": None, "last_name": ""}])
def test_absent_optional_name_echoes_do_not_reject_a_result(names):
    assert email_identity_mismatch(IDENTITY, {"email": "avery@employer.test", **names}) is None


def test_linkedin_only_identity_does_not_impose_a_domain_or_name():
    assert email_identity_mismatch({"linkedin_url": "https://www.linkedin.com/in/synthetic-fixture"},
                                   OUTPUT) is None


def test_linkedin_identity_still_enforces_an_explicit_domain():
    identity = {"linkedin_url": "https://www.linkedin.com/in/synthetic-fixture", "domain": "employer.test"}
    assert email_identity_mismatch(identity, OUTPUT) == "email_domain_mismatch"


def test_diagnostic_contains_only_a_reason_code():
    reason = email_identity_mismatch(IDENTITY, OUTPUT)
    assert reason == "email_domain_mismatch"
    assert not any(value in reason for value in (*IDENTITY.values(), *OUTPUT.values()))


def test_only_explicit_name_fields_are_constraints_not_guessed_full_name_parts():
    assert email_identity_mismatch({'full_name': 'Avery Jordan Example', 'domain': 'employer.test'},
                                   {'email': 'avery@employer.test', 'first_name': 'Avery', 'last_name': 'Example'}) is None
    assert email_identity_mismatch({'full_name': 'Avery van der Example', 'domain': 'employer.test'},
                                   {'email': 'avery@employer.test', 'full_name': 'Avery van der Example'}) is None
    assert email_identity_mismatch({'full_name': 'Avery Example', 'domain': 'employer.test'},
                                   {'email': 'avery@employer.test', 'full_name': 'Someone Else'}) == 'full_name_mismatch'


def test_complete_echo_binds_a_split_name_request_without_guessing_boundaries():
    assert email_identity_mismatch(IDENTITY, {'email': 'avery@employer.test', 'full_name': 'Someone Else'}) == 'full_name_mismatch'
    assert email_identity_mismatch(IDENTITY, {'email': 'avery@employer.test', 'full_name': 'Avery Example'}) is None
