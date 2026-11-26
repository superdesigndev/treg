"""Lusha's plugins (catalog schema v2, section 5)."""

from __future__ import annotations

from . import Request

# Lusha's rate card (GET /v3/account/usage `pricing`): an email reveal costs 1 credit, a phone
# reveal 5, a matched contact on search-and-enrich 1, a buying-group contact 1, a company 1, each
# premium company field 1 per company, and a company search 1 per started block of 25 companies.
_REVEAL = {"emails": 1, "phones": 5}
_MATCH = 1
_BUYING_GROUP_DEFAULT_LIMIT = 60
# Lusha's buying-group `pagination.size` bounds the contacts one call returns: 10 to 100, default 100.
_BUYING_GROUP_PAGE_MAX = 100
_COMPANY_SEARCH_BLOCK = 25
_PREMIUM = frozenset({
    "employeesByDepartment", "employeesByLocation", "employeesBySeniority", "competitors",
    "estimatedAnnualItSpend", "monthlyWebsiteTraffic", "openJobsTotal", "openJobsByDepartment",
    "openJobsByLocation", "openJobsBySeniority", "directParent", "ultimateParent",
})


def _body(request: Request) -> dict:
    return request.body if isinstance(request.body, dict) else {}


def _items(body: dict, key: str) -> int:
    items = body.get(key)
    return max(len(items), 1) if isinstance(items, list) else 1


def _reveal_credits(body: dict) -> int:
    """Credits one contact's reveal can cost: what `reveal` asks for, or both datapoints, the most
    it can cost, when it names none Lusha knows."""
    asked = body.get("reveal")
    names = {name for name in asked if name in _REVEAL} if isinstance(asked, list) else set()
    return sum(_REVEAL[name] for name in names or _REVEAL)


def contact_reveal_credits(request: Request) -> int:
    """contacts/enrich: each id costs the datapoints it reveals."""
    body = _body(request)
    return _items(body, "ids") * _reveal_credits(body)


def contact_enrich_units(request: Request) -> float:
    """contacts/search-and-enrich: each contact costs the match plus the datapoints it reveals, in
    units of the row's 2-credit price (a matched contact with one email)."""
    body = _body(request)
    return _items(body, "contacts") * (_MATCH + _reveal_credits(body)) / 2


def _premium_fields(body: dict) -> int:
    asked = body.get("reveal")
    return len({name for name in asked if name in _PREMIUM}) if isinstance(asked, list) else 0


def company_enrich_units(request: Request) -> float:
    """companies/search-and-enrich: one company search per started block of 25 companies, then each
    company and each premium field it reveals, in units of the row's 2-credit price (one company)."""
    body = _body(request)
    companies = _items(body, "companies")
    blocks = -(-companies // _COMPANY_SEARCH_BLOCK)
    return (blocks + companies * (1 + _premium_fields(body))) / 2


def company_reveal_credits(request: Request) -> int:
    """companies/enrich: each id costs the company and each premium field it reveals."""
    body = _body(request)
    return _items(body, "ids") * (1 + _premium_fields(body))


def buying_group_contacts(request: Request) -> int:
    """contacts/buying-group: up to `contactsLimit` contacts per company (Lusha's default 60), and
    no more than one page of `pagination.size` contacts (default and most 100)."""
    body = _body(request)
    limit = body.get("contactsLimit")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        limit = _BUYING_GROUP_DEFAULT_LIMIT
    pagination = body.get("pagination")
    size = pagination.get("size") if isinstance(pagination, dict) else None
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= _BUYING_GROUP_PAGE_MAX:
        size = _BUYING_GROUP_PAGE_MAX
    return min(_items(body, "companies") * limit, size)
