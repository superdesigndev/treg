"""Arena task definitions and attributed results. No execution or money writes."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

VERSION = "2"
MAX_RESULT_BYTES = 256_000
MAX_BATCH_RAW_BYTES = 2_000_000
RETENTION_DAYS = 30
MAX_ENTRIES = 50
DISCOVERY_LIMIT = 10
DISCOVERY_ENTRIES = 10
DISCOVERY_TASKS = frozenset({"people.search", "people.company.search", "companies.similar"})
TERMINAL = frozenset({"completed", "cancelled", "interrupted", "failed"})
REASONS = frozenset({"more_complete", "correct_identity", "more_current", "better_verification",
                     "wrong_identity", "missing_fields", "conflicting_data"})


@dataclass(frozen=True)
class Task:
    capability: str
    label: str
    description: str
    variants: tuple[tuple[str, ...], ...]
    catalog_capability: str = ""


TASKS = {
    t.capability: t for t in (
        Task("people.search", "Find people", "Discover people by a search description, or by role and country.",
             (("q",), ("title", "country"))),
        Task("people.company.search", "People at a company", "Find people at a company, with an optional job-title filter.",
             (("company_domain",), ("title", "company_domain")), "people.search"),
        Task("companies.similar", "Find similar companies", "Discover companies similar to a seed company domain.",
             (("domain",),)),
        Task("people.email.find", "Find a work email", "Find a work email from a name or LinkedIn profile.",
             (("full_name", "domain"), ("linkedin_url",))),
        Task("people.enrich", "Enrich a person", "Compare the details each service knows about a person.",
             (("linkedin_url",), ("email",), ("full_name", "domain"))),
        Task("companies.enrich", "Enrich a company", "Compare company profiles, from the basics to the details.",
             (("domain",), ("name",), ("linkedin_url",))),
        Task("people.phone.find", "Find a phone number", "Find a phone number. A found number is not a verified live line.",
             (("linkedin_url",), ("email",), ("full_name", "domain"))),
        Task("people.phone.verify", "Verify phone number", "Validate number format and carrier details; this does not confirm a live line.",
             (("phone",),)),
        Task("people.email.verify", "Verify email", "Compare mailbox verdicts. Invalid is a useful answer, too.",
             (("email",),)),
        Task("people.identity.resolve", "Find a LinkedIn profile", "Find the LinkedIn profile associated with an email.",
             (("email",),)),
    )
}

# Public demo inputs only, never sampled customer queries or enrichment results.
# Contact sources: patrickcollison.com/about; hubspot.com/company-news/author/dharmesh-shah.
# Business phones: apple.com/contact; news.microsoft.com/ja-jp/cp/corpdata/.
_EXAMPLE_INPUTS = (
    {"full_name":"Patrick Collison", "domain":"stripe.com", "company_domain":"stripe.com",
     "name":"Stripe", "linkedin_url":"https://www.linkedin.com/in/patrickcollison",
     "email":"patrick@collison.ie", "phone":"+18006927753",
     "q":"Software engineers at Stripe in the United States", "title":"Software Engineer", "country":"US"},
    {"full_name":"Dharmesh Shah", "domain":"hubspot.com", "company_domain":"hubspot.com",
     "name":"HubSpot", "linkedin_url":"https://www.linkedin.com/in/dharmesh",
     "email":"dshah@hubspot.com", "phone":"+14258828080",
     "q":"Sales leaders at HubSpot in the United States", "title":"Sales Director", "country":"US"},
)


def example_inputs(task: Task) -> list[list[dict[str, str]]]:
    rows = [dict(row) for row in _EXAMPLE_INPUTS]
    if task.capability == "companies.enrich":
        for row, slug in zip(rows, ("stripe", "hubspot")):
            row["linkedin_url"] = "https://www.linkedin.com/company/" + slug
    return [[{key: row[key] for key in variant} for row in rows] for variant in task.variants]


# A personal mailbox is a different task; bulk and asynchronous jobs are excluded by the planner.
VERIFICATION_TASKS = {"people.email.find": ("people.email.verify", "email"),
                      "people.phone.find": ("people.phone.verify", "phone")}

EXCLUDED = frozenset({"leadmagic.x.personal-email-finder"})


class ArenaError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def catalog_capability(capability: str) -> str:
    task = TASKS[capability]
    return task.catalog_capability or task.capability


def supports_discovery(capability: str, identity: dict, accepted) -> bool:
    # A company-only search must not silently drop a requested title (or vice versa).
    required = {k for k, v in identity.items() if v not in (None, "") and k not in {"country", "limit"}}
    return capability not in DISCOVERY_TASKS or required <= set(accepted)


def validate_identity(capability: str, identity: dict) -> dict[str, str]:
    task = TASKS.get(capability)
    if task is None:
        raise ArenaError("Choose an Arena task.")
    allowed = {k for variant in task.variants for k in variant}
    if capability == "people.phone.verify":
        allowed.add("country_code")
    if any(k not in allowed for k in identity):
        raise ArenaError("This task does not accept those input fields.")
    clean = {}
    for k, v in identity.items():
        if not isinstance(v, str) or len(v) > 500:
            raise ArenaError("Use text inputs of at most 500 characters.")
        if v.strip():
            clean[k] = v.strip()
    if not any(set(v) <= clean.keys() for v in task.variants):
        raise ArenaError("Complete one of the task's input options.")
    if "country_code" in clean:
        if not re.fullmatch(r"[A-Za-z]{2}", clean["country_code"]):
            raise ArenaError("Use a two-letter phone country code, such as US or GB.")
        clean["country_code"] = clean["country_code"].upper()
    if "phone" in clean:
        phone = re.sub(r"[\s().-]", "", clean["phone"])
        international = re.fullmatch(r"\+[1-9][0-9]{6,14}", phone)
        national = clean.get("country_code") and re.fullmatch(r"[0-9]{6,15}", phone)
        if not international and not national:
            raise ArenaError("Use an international phone number with a + country calling code, or supply its two-letter country code.")
        clean["phone"] = phone
    if "full_name" in clean:
        parts = clean["full_name"].split()
        if len(parts) < 2 or not all(any(c.isalpha() for c in p) for p in (parts[0], parts[-1])):
            raise ArenaError("Enter both a first and last name for a name-based comparison, or use a LinkedIn URL.")
        clean["full_name"] = " ".join(parts)
    domain_key = "company_domain" if "company_domain" in clean else "domain"
    if domain_key in clean:
        url = _web_url(clean[domain_key])
        if url is None:
            raise ArenaError("Enter a company domain, such as example.com.")
        clean[domain_key] = url.hostname.lower()
    if "country" in clean:
        if not re.fullmatch(r"[A-Za-z]{2}", clean["country"]):
            raise ArenaError("Use a two-letter country code, such as US or GB.")
        clean["country"] = clean["country"].upper()
    if "email" in clean and not re.fullmatch(r"[^\s@]+@[^\s@./:?#\[\]]+(?:\.[^\s@./:?#\[\]]+)+", clean["email"]):
        raise ArenaError("Enter an email address.")
    if "linkedin_url" in clean:
        u = _web_url(clean["linkedin_url"])
        if (u is None or not clean["linkedin_url"].lower().startswith(("http://", "https://"))
                or u.hostname not in {"linkedin.com", "www.linkedin.com"}):
            raise ArenaError("Enter a full LinkedIn profile URL.")
        prefix = "/company/" if capability == "companies.enrich" else "/in/"
        if not u.path.startswith(prefix) or not u.path[len(prefix):].strip("/"):
            raise ArenaError("Use a LinkedIn company URL." if prefix == "/company/" else "Use a LinkedIn person URL.")
        clean["linkedin_url"] = "https://www.linkedin.com" + u.path.rstrip("/")
    return clean


def verification_identity(capability: str, output: dict) -> dict[str, str]:
    """Preserve a lookup's country context without guessing the number's country."""
    field = "phone" if capability == "people.phone.verify" else "email"
    identity = {field: output.get(field, "")}
    if field == "phone" and not str(identity[field]).lstrip().startswith("+"):
        country = output.get("country_code")
        if isinstance(country, str) and re.fullmatch(r"[A-Za-z]{2}", country.strip()):
            identity["country_code"] = country.strip()
        else:
            raise ArenaError("Verification needs a country code: the provider returned a local phone number without usable country information. No verification call was made.")
    return validate_identity(capability, identity)


def validate_entries(capability: str, identity: dict | None, identities: list[dict] | None) -> list[dict]:
    if (identity is None) == (identities is None):
        raise ArenaError("Provide one query or a list of entries.")
    entries = identities if identities is not None else [identity]
    maximum = DISCOVERY_ENTRIES if capability in DISCOVERY_TASKS else MAX_ENTRIES
    if not 1 <= len(entries) <= maximum:
        raise ArenaError(f"Use between 1 and {maximum} entries.")
    clean, seen = [], set()
    for i, entry in enumerate(entries):
        try:
            value = validate_identity(capability, entry)
        except ArenaError as exc:
            raise ArenaError(f"Entry {i + 1}: {exc}") from exc
        fingerprint = tuple(sorted((k, v.casefold()) for k, v in value.items()))
        if fingerprint in seen:
            raise ArenaError(f"Entry {i + 1} is a duplicate. Remove it before running.")
        if clean and value.keys() != clean[0].keys():
            raise ArenaError("Use the same input type for every entry.")
        seen.add(fingerprint)
        clean.append(value)
    return clean


def required_credit(attempts: list[dict], mode: str) -> int:
    if mode == "compare":
        return sum(a["estimate_micro"] for a in attempts)
    cheapest = {}
    for a in attempts:
        entry = a.get("entry_index", 0)
        cheapest[entry] = min(cheapest.get(entry, a["estimate_micro"]), a["estimate_micro"])
    return sum(cheapest.values())


def classify(contract, adapter, endpoint: dict, status: int, doc: Any) -> tuple[str, dict]:
    """Match the routed lookup's structural hit rule; retain verification qualifiers separately."""
    miss_status = (endpoint.get("miss") or {}).get("status")
    if status == miss_status and 400 <= status < 500:
        return "miss", {}
    if not 200 <= status < 300:
        return "error", {}
    if not isinstance(doc, (dict, list)):
        return "error", {}
    output = safe_output(adapter.from_upstream(doc), capability=contract.capability)
    empty = any(output.get(k) in (None, "", [], {}) for k in contract.required_output)
    return ("miss" if adapter.is_miss(doc) or empty else "hit"), output


def _web_url(value: str):
    """Parse a vendor URL without treating credentials or executable schemes as web addresses."""
    try:
        url = urlsplit(value if "://" in value else "https://" + value)
        if (url.scheme not in {"http", "https"} or not url.hostname or "." not in url.hostname
                or url.username is not None or url.password is not None
                or any(c.isspace() for c in value)):
            return None
        _ = url.port  # Malformed ports must not make historical results unreadable.
        return url
    except ValueError:
        return None


def safe_output(output: dict, *, capability: str = "") -> dict:
    """Normalize Arena's scalar fields; preserve vendor snapshots separately.

    Booleans in text fields are often vendor masking flags, not real data. Never turn them
    into names, locations or URLs. Verification booleans and numeric metrics retain their types.
    """
    result = {}
    for key, value in output.items():
        if capability in DISCOVERY_TASKS and key in {"people", "companies"}:
            result[key] = search_rows(value, company=key == "companies")
            continue
        if capability in DISCOVERY_TASKS and key == "count":
            continue  # Provider totals are not the number of returned, usable rows.
        if key in {"verified", "valid"}:
            result[key] = value if isinstance(value, bool) else None
            continue
        if key in {"confidence", "score", "employees", "founded"}:
            result[key] = (value[:4000] if isinstance(value, str) else value
                           if isinstance(value, (int, float)) and not isinstance(value, bool) else None)
            continue
        if not isinstance(value, str):
            result[key] = None
            continue
        value = value.strip()[:4000]
        if key == "linkedin_url":
            prefix = "/company/" if capability == "companies.enrich" else "/in/"
            if re.fullmatch(r"[\w-]+", value):
                value = "https://www.linkedin.com" + prefix + value
            url = _web_url(value)
            value = ("https://www.linkedin.com" + url.path.rstrip("/")
                     if url and url.hostname in {"linkedin.com", "www.linkedin.com"}
                     and url.path.startswith(prefix) and url.path[len(prefix):].strip("/") else None)
        elif key in {"domain", "company_domain", "website"}:
            url = _web_url(value)
            value = (urlunsplit(url) if key == "website" else url.hostname.lower()) if url else None
        result[key] = value
    if capability in DISCOVERY_TASKS:
        result["count"] = len(result.get("companies" if capability == "companies.similar" else "people", []))
    return result


def search_rows(value, *, company=False):
    """Bounded, scalar display projection; original vendor bodies remain separate."""
    if not isinstance(value, list):
        return []
    rows = []
    for item in value[:DISCOVERY_LIMIT]:
        if not isinstance(item, dict):
            continue
        def pick(*paths):
            for path in paths:
                node = item
                for part in path.split("."):
                    node = node.get(part) if isinstance(node, dict) else None
                if isinstance(node, str) and node.strip():
                    return node.strip()[:1000]
            return None
        row = {"name": pick("name", "full_name", "fullName", "displayName", "basic_profile.name"),
               "title": pick("title", "lastJobTitle", "headline", "position", "job_title", "jobTitle", "basic_profile.headline"),
               "company": pick("company.name", "company", "company_name", "companyName", "lastCompanyName", "organization.name"),
               "location": pick("location", "location.name", "location.address", "locality", "country", "address", "basic_profile.location"),
               "email": pick("email", "work_email", "value"),
               "linkedin_url": pick("linkedin_url", "linkedinUrl", "profile_url", "profileUrl", "socials.linkedin_url", "URLs.linkedin", "url", "basic_profile.linkedin_url"),
               "domain": pick("domain", "website_url", "website", "company.domain"),
               "description": pick("description", "industry")}
        if not row["name"]:
            row["name"] = " ".join(filter(None, [pick("first_name", "firstName", "firstname"), pick("last_name", "lastName", "lastname")])) or None
        row = safe_output(row, capability="companies.enrich" if company else "people.enrich")
        if any(row.get(k) for k in (("name", "domain") if company else ("name", "linkedin_url", "email"))):
            rows.append({k: v for k, v in row.items() if v is not None})
    return rows


def verification_rejected_email(attempt: dict, capability: str) -> bool:
    verification = attempt.get("verification") or {}
    output = verification.get("output") or {}
    # All current email adapters map catch-all/unknown to valid=False too.
    # Only an explicit negative mailbox verdict justifies an issue report.
    return (capability == "people.email.find" and attempt.get("state") == "hit"
            and verification.get("capability") == "people.email.verify"
            and verification.get("state") == "hit"
            and output.get("valid") is False
            and str(output.get("status", "")).strip().lower() in {"invalid", "undeliverable"})


def present(payload: dict, *, mode: str, state: str, capability: str = "") -> dict:
    attempts = payload.get("attempts", [])
    ordered = sorted(attempts, key=lambda a: (a.get("entry_index", 0), a["order"] if mode == "waterfall" else a["display_order"]))
    results = []
    for a in ordered:
        row = {"id": a["id"], "entry_index": a.get("entry_index", 0), "label": chr(65 + a["display_order"]), "state": a["state"],
               "output": safe_output(a.get("output", {}), capability=capability)}
        row.update({k: a.get(k) for k in ("provider", "endpoint_id", "tier", "estimate_micro",
                   "charged_micro", "duration_ms", "started_ms", "detail", "raw", "raw_omitted", "cached", "call_ref", "report", "rating", "manual")})
        # Distinguish the vendor's 402 from our team's balance/admission errors, including history.
        row["upstream_status"] = a.get("status") if not a.get("failure_kind") else None
        row["can_try"] = state in TERMINAL | {"running"} and a["state"] in {"not_attempted", "skipped"} and not a.get("call_ref") and not a.get("manual")
        v = a.get("verification")
        row["can_verify"] = capability in VERIFICATION_TASKS and a["state"] == "hit" and not v and (not payload.get("auto_verify") or state in TERMINAL)
        if v:
            row["verification"] = {k:v.get(k) for k in ("id", "capability", "state", "not_started", "provider", "endpoint_id", "charged_micro", "duration_ms", "detail", "raw", "call_ref", "tried", "served_by")}
            row["verification"]["output"] = safe_output(v.get("output", {}), capability=v.get("capability", ""))
            row["lookup_charged_micro"] = row["charged_micro"]
            row["charged_micro"] = None if row["charged_micro"] is None or v.get("charged_micro") is None else row["charged_micro"] + v["charged_micro"]
        results.append(row)
    out = {"progress": sum(a["state"] not in {"queued", "running"} for a in attempts),
           "total": len(attempts), "results": results}
    out["charged_micro"] = sum((a.get("charged_micro") or 0) + ((a.get("verification") or {}).get("charged_micro") or 0) for a in attempts)
    out["charge_pending"] = any((a.get("charged_micro") is None and a["state"] != "queued") or (a.get("verification") and a["verification"].get("charged_micro") is None) for a in attempts)
    out["stop_reason"] = payload.get("stop_reason", "")
    return out


def validate_vote(kind: str, selected: list[str], reasons: list[str], attempts: list[dict]) -> None:
    eligible = {a["id"] for a in attempts if a["state"] == "hit"}
    if len(set(selected)) != len(selected) or not set(selected) <= eligible:
        raise ArenaError("Choose from the results shown in this comparison.")
    expected = {"winner": len(selected) == 1, "tie": len(selected) >= 2,
                "none": not selected, "cannot_judge": not selected, "skip": not selected}
    if kind not in expected or not expected[kind]:
        raise ArenaError("Choose a result, a tie, none useful, or cannot judge.")
    if not set(reasons) <= REASONS:
        raise ArenaError("Unknown feedback reason.")
