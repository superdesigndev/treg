"""Bound Enrichlayer shared-key requests and settle per returned result."""

from __future__ import annotations

from collections.abc import Mapping


_PREMIUM_OPTIONS: dict[str, frozenset[str]] = {
    "enrichlayer.school.profile": frozenset({"live_fetch"}),
    "enrichlayer.company.profile": frozenset({
        "categories", "funding_data", "exit_data", "acquisitions", "extra", "live_fetch"}),
    "enrichlayer.person.profile": frozenset({
        "extra", "github_profile_id", "facebook_profile_id", "twitter_profile_id",
        "personal_contact_number", "personal_email", "inferred_salary", "live_fetch"}),
    "enrichlayer.company.employees.list": frozenset({
        "country", "enrich_profiles", "boolean_role_search", "role_search",
        "sort_by", "resolve_numeric_id"}),
    "enrichlayer.company.employees.count": frozenset({"at_date", "estimated_employee_count"}),
    "enrichlayer.person.lookup": frozenset({"similarity_checks", "enrich_profile"}),
    "enrichlayer.company.role.lookup": frozenset({"enrich_profile"}),
    "enrichlayer.company.lookup": frozenset({"enrich_profile"}),
    "enrichlayer.person.reverse-email": frozenset({"lookup_depth", "enrich_profile"}),
}


def _positive_page(query: Mapping[str, str], maximum: int) -> int | None:
    raw = query.get("page_size")
    if raw is None or len(raw) > 5 or not raw.isdecimal():
        return None
    page = int(raw)
    return page if 1 <= page <= maximum else None


def invalid_platform_parameter(endpoint_id: str, query: Mapping[str, str], cost: dict) -> str | None:
    """Curated shared-key options only; raw BYOK tools retain the complete upstream API."""
    if premium := _PREMIUM_OPTIONS.get(endpoint_id, frozenset()) & query.keys():
        return sorted(premium)[0]
    if endpoint_id in ("enrichlayer.company.profile", "enrichlayer.person.profile") \
            and query.get("use_cache") != "if-present":
        return "use_cache"
    if endpoint_id in _PREMIUM_OPTIONS and query.get("use_cache", "if-present") != "if-present":
        return "use_cache"
    rule = cost.get("enrichlayer")
    if not isinstance(rule, dict):
        return None
    maximum = int(rule["max_page"])
    page = _positive_page(query, maximum)
    if page is None:
        return "page_size"
    if endpoint_id in ("enrichlayer.people.search", "enrichlayer.companies.search"):
        if query.get("enrich_profiles", "skip") not in ("skip", "enrich"):
            return "enrich_profiles"
        if query.get("use_cache", "if-present") not in ("if-present", "if-recent"):
            return "use_cache"
        if (query.get("enrich_profiles") == "enrich" or query.get("use_cache") == "if-recent") \
                and page > 10:
            return "page_size"
    if endpoint_id == "enrichlayer.contacts.personal-email" \
            and query.get("email_validation", "none") not in ("none", "fast", "precise"):
        return "email_validation"
    return None


def rate_credits(query: Mapping[str, str], cost: dict) -> int:
    credits = int(cost["value"])
    for name, prices in cost["enrichlayer"].get("extra_per_result", {}).items():
        credits += prices.get(query.get(name), 0)
    return credits


def estimate_micro(endpoint_id: str, cost: dict, query: Mapping[str, str], credit_micro: int) -> tuple[int, int]:
    rule = cost["enrichlayer"]
    # An invalid request is rejected before relay; reserve its declared maximum meanwhile.
    page = _positive_page(query, int(rule["max_page"])) or int(rule["max_page"])
    unit = rate_credits(query, cost) * credit_micro
    return page * unit, unit


def observed_micro(rule: dict, unit_micro: int, document: object, ceiling: int) -> int | None:
    """A malformed or missing result list cannot be used as zero-charge evidence."""
    if not isinstance(document, dict):
        return None
    rows = document.get(rule["field"])
    if not isinstance(rows, list):
        return None
    count = len(rows)
    if rule["field"] == "emails":
        # Precise validation can place found but invalid addresses in this companion list.
        invalid = document.get("invalid_emails", [])
        if not isinstance(invalid, list):
            return None
        count += len(invalid)
    charge = count * unit_micro
    return charge if charge <= ceiling else None
