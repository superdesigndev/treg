"""ContactOut's Starter rate: request-sized holds and contact hits per returned profile.

Pure billing interpretation of the unmodified request/response. Prices live in catalog YAML;
no balance polling or database access belongs in a call's settlement.
"""

from __future__ import annotations


def _true(value):
    return value is True or isinstance(value, str) and value.lower() in ("true", "1")


def _selected(job, request):
    if job == "contact":
        selected = str(request.get("email_type", "personal,work")).split(",")
        return (
            "work" in selected,
            "personal" in selected,
            _true(request.get("include_phone")),
        )
    if job == "person":
        included = request.get("include") or []
        if not isinstance(included, (list, str)):
            return (True,) * 3  # invalid input cannot reduce the maximum hold
        return tuple(k in included for k in ("work_email", "personal_email", "phone"))
    if job in ("search", "decision"):
        return (_true(request.get("reveal_info")),) * 3
    if job == "linkedin":
        return (not _true(request.get("profile_only")),) * 3
    if job == "email":
        return (request.get("include") == "work_email", True, True)
    return (False,) * 3


def estimate(cost, request):
    """Reserve for requested contact types and a documented maximum page/input size."""
    rules = cost.get("contactout") or {}
    job, rates = rules.get("job"), rules.get("rates_micro") or {}
    if not job:
        return 0
    if job == "reverse":
        return round(cost["usd"] * 1_000_000)
    contact = sum(
        rates[k]
        for k, selected in zip(
            ("work_email", "personal_email", "phone"), _selected(job, request)
        )
        if selected
    )
    search = (
        rates["search"]
        if job in ("search", "decision", "person", "company_search", "domains")
        or (job == "linkedin" and _true(request.get("profile_only")))
        else 0
    )
    if job == "domains":
        domains = request.get("domains")
        n = len(domains) if isinstance(domains, list) else 30
        n = max(1, min(30, n))
    elif job in ("search", "decision", "company_search"):
        # Only people search documents page_size. Unknown/invalid input never reduces a hold.
        size = request.get("page_size", 25) if job == "search" else 25
        n = (
            size
            if isinstance(size, int) and not isinstance(size, bool) and 1 <= size <= 25
            else 25
        )
    else:
        n = 1
    return (search + contact) * n


def _present(value):
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_present(v) for v in value)
    # Availability flags and verification status maps are never contact hits.
    return False


def _hit(profile, *names):
    return any(_present(profile.get(name)) for name in names)


def _rows(doc, name):
    value = doc.get(name)
    if isinstance(value, dict):
        values = value.values()
    elif isinstance(value, list):
        values = value
    else:
        return None
    # An empty/null company/profile is not a hit. Do not count metadata.total_results.
    return [v for v in values if isinstance(v, dict) and v]


def observed(cost, evidence, doc):
    """Derived charge, not a vendor-reported dollar bill. No hit evidence means no charge."""
    rules = cost.get("contactout") or {}
    job, rates = rules.get("job"), rules.get("rates_micro") or {}
    if not job:
        return 0
    if doc.get("status_code") != 200:
        return 0
    if job == "reverse":
        return round(cost["usd"] * 1_000_000)
    request = (
        evidence.get("body")
        if job in ("person", "search", "domains", "company_search")
        else evidence.get("queryParams")
    ) or {}
    if not isinstance(request, dict):
        request = {}
    if job in ("company_search", "domains"):
        rows = _rows(doc, "companies")
        return 0 if rows is None else len(rows) * rates["search"]
    if job in ("search", "decision"):
        rows = _rows(doc, "profiles")
        if rows is None:
            return 0
    else:
        profile = doc.get("profile")
        if profile in (None, [], {}):
            return 0
        if not isinstance(profile, dict):
            return 0
        rows = [profile]
    total = (
        len(rows) * rates["search"]
        if job in ("search", "decision", "person")
        or (job == "linkedin" and _true(request.get("profile_only")))
        else 0
    )
    selected = _selected(job, request)
    for profile in rows:
        contacts = (
            profile.get("contact_info") if job in ("search", "decision") else profile
        )
        if not isinstance(contacts, dict):
            # No reveal on this profile. Availability booleans do not imply billable contacts.
            continue
        work = _hit(contacts, "work_email", "work_emails", "workEmail")
        personal = _hit(contacts, "personal_email", "personal_emails")
        # Camel-case enrichment uses `email` for personal. The submitted address is an echo,
        # not a newly revealed email. LinkedIn's `email` array combines types; never count twice.
        if (
            job in ("person", "email")
            and isinstance(contacts.get("email"), str)
            and contacts.get("email") != request.get("email")
        ):
            personal = personal or _hit(contacts, "email")
        phone = _hit(contacts, "phone", "phones")
        total += sum(
            rates[k]
            for k, hit, allowed in zip(
                ("work_email", "personal_email", "phone"),
                (work, personal, phone),
                selected,
            )
            if hit and allowed
        )
    return total
