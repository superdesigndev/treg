"""Request segments: categories computed from what a caller asked, for analysis and route rules.

A contract names a segment set (`segments: people_search`); `call_events.segment` adds its
values to the event's `input_meta`, and a published route rule may match on any of them. Every
value is a word from a fixed vocabulary (`Segmenter.vocab`): no title, query, name, place or
domain is ever recorded, only the category it falls in. A set carries a version, recorded as
`segments_version`, so analysis can tell a call classified by an older rule from one classified
by the current rule. Change a rule or a vocabulary and the version moves.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from typing import Any, Callable

from . import paths as P

TRUE_FALSE = frozenset({"true", "false"})


@dataclass(frozen=True)
class Segmenter:
    version: str
    vocab: dict[str, frozenset[str] | Callable[[str], bool]]
    compute: Callable[[dict], dict[str, str]]
    # (the asked identity and filters, the answer's core output) -> counts, as strings. None = the
    # set records nothing about answers.
    answer: Callable[[dict, dict], dict[str, str]] | None = None


# ---- job titles ---------------------------------------------------------------------------------

# (function, pattern over the lower-cased title); the first match wins, so specific functions come
# before the executive catch-all.
TITLE_FUNCTIONS: tuple[tuple[str, str], ...] = (
    ("sales", r"sales|account exec|account manag|business develop|\bbdr\b|\bsdr\b|revenue|\bcro\b"
              r"|vertrieb|vendit|ventas|venda|comercial|commercial"),
    ("marketing", r"marketing|growth|brand|demand gen|\bcmo\b|communication|\bpr\b|content|seo"),
    ("product", r"product"),
    ("data", r"\bdata\b|analytic|machine learning|\bml\b|\bai\b|scientist"),
    ("engineering", r"engineer|developer|\bcto\b|software|devops|architect|technolog|tech lead"
                    r"|programm|r&d|research"),
    ("it_security", r"\bit\b|information technology|\bcio\b|\bciso\b|security|infrastructure|system"),
    ("hr_talent", r"\bhr\b|human resources|people|talent|recruit|personal|\bchro\b"),
    ("finance", r"financ|\bcfo\b|accountant|accounting|controller|treasur|tax"),
    ("operations", r"operation|\bcoo\b|supply|logistic|procure|purchas|buyer|facilit"),
    ("customer", r"customer|support|success|service|client"),
    ("legal", r"legal|counsel|compliance|attorney|lawyer"),
    ("design", r"design|creative|\bux\b"),
    ("education", r"teacher|professor|lecturer|\bdean\b|school|faculty|academic"),
    ("healthcare", r"nurse|physician|doctor|medical|clinical|dentist|pharmac|therap"),
    ("real_estate", r"real estate|realtor|broker|property|leasing"),
    ("executive", r"\bceo\b|founder|owner|(?<!vice )(?<!vice-)president|managing director|general manager|geschaftsf"
                  r"|geschäftsf|gesch\.f|chief executive|\bpartner\b|principal|directeur g[ée]n[ée]ral"
                  r"|director general|amministratore delegato"),
)
# (seniority, pattern); the first match wins, a title matching none is `individual`.
TITLE_SENIORITIES: tuple[tuple[str, str], ...] = (
    ("c_level_founder", r"\bc[a-z]{1,2}o\b|chief|(?<!vice )(?<!vice-)president|founder|owner|\bpartner\b"
                        r"|managing director|geschäftsf|geschaftsf|amministratore delegato"
                        r"|directeur g[ée]n[ée]ral|director general"),
    ("vp", r"\bvp\b|\bsvp\b|\bevp\b|vice[ -]president"),
    ("head_director", r"\bhead\b|director|directeur|direttore|directora?\b|leiter|diretor"),
    ("manager_lead", r"manager|\blead\b|responsabile|gerente|supervisor|coordinator"),
)
# A non-ASCII letter, or a common non-English role word written in ASCII.
NON_ENGLISH = (r"[^\x01-\x7f]|directeur|direttore|responsabile|gerente|geschäfts|geschafts|leiter"
               r"|jefe|diretor|vendite|ventas|vendas|commerciale|responsable|amministratore"
               r"|vertrieb|verkauf|leitung|ingenieur|ingeniero|engenheiro|gestionnaire")
# Several titles in one string: `CEO, Founder`, `CTO / VP Engineering`, `CMO or Head of Growth`.
MULTI_TITLE = r",|/|\||;|\bor\b"

FUNCTIONS = frozenset(name for name, _ in TITLE_FUNCTIONS) | {"other", "none"}
SENIORITIES = frozenset(name for name, _ in TITLE_SENIORITIES) | {"individual", "none"}
TITLE_CLASSES = frozenset({"none", "leader", "function", "multi", "other"})
LANGS = frozenset({"en", "non_en", "none"})

_FN = tuple((name, re.compile(rx)) for name, rx in TITLE_FUNCTIONS)
_SEN = tuple((name, re.compile(rx)) for name, rx in TITLE_SENIORITIES)
_NON_EN = re.compile(NON_ENGLISH)
_MULTI = re.compile(MULTI_TITLE)


def _text(value: Any) -> str:
    """One string from a scalar or a list of them, lower-cased; '' for nothing."""
    if isinstance(value, list):
        value = ", ".join(v for v in value if isinstance(v, str) and v.strip())
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip().lower()


def title_function(title: str) -> str:
    t = _text(title)
    if not t:
        return "none"
    return next((name for name, rx in _FN if rx.search(t)), "other")


def title_seniority(title: str) -> str:
    t = _text(title)
    if not t:
        return "none"
    return next((name for name, rx in _SEN if rx.search(t)), "individual")


def lang(text: str) -> str:
    t = _text(text)
    if not t:
        return "none"
    return "non_en" if _NON_EN.search(t) else "en"


def title_class(title: str) -> str:
    """`none`, `multi` (several titles in one string), `leader` (a company's head: CEO, founder,
    owner, president, managing director, partner, in any of the languages above), `function` (a
    job function, at any level) or `other`."""
    t = _text(title)
    if not t:
        return "none"
    if _MULTI.search(t):
        return "multi"
    fn = title_function(t)
    if fn == "executive":
        return "leader"
    return "function" if fn != "other" else "other"


def words_bucket(text: str, edges: tuple[tuple[int, str], ...], top: str) -> str:
    t = _text(text)
    if not t:
        return "none"
    n = len(t.split())
    return next((name for limit, name in edges if n <= limit), top)


# ---- places -------------------------------------------------------------------------------------

_COUNTRY_ALIASES = frozenset({
    "usa", "u.s.", "u.s.a.", "united states of america", "america", "uk", "u.k.", "england",
    "scotland", "wales", "northern ireland", "great britain", "britain", "uae", "holland",
    "south korea", "korea", "russia", "vietnam", "czechia", "türkiye", "turkiye", "iran", "syria",
    "taiwan", "bolivia", "venezuela", "tanzania", "laos", "moldova",
})
_US_STATES = frozenset({
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
    "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
    "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming", "district of columbia",
})
_REGIONS = _US_STATES | frozenset({
    # Canadian provinces, Australian states, large European and Asian regions
    "ontario", "quebec", "british columbia", "alberta", "manitoba", "saskatchewan", "nova scotia",
    "new south wales", "victoria", "queensland", "western australia", "south australia",
    "tasmania", "bavaria", "bayern", "hesse", "hessen", "saxony", "north rhine-westphalia",
    "nordrhein-westfalen", "baden-württemberg", "catalonia", "cataluña", "andalusia", "lombardy",
    "lombardia", "île-de-france", "ile-de-france", "provence", "brittany", "flanders", "wallonia",
    "maharashtra", "karnataka", "tamil nadu", "gujarat", "punjab", "guangdong", "são paulo state",
    # multi-country regions
    "europe", "asia", "africa", "north america", "south america", "latin america", "latam", "emea",
    "apac", "asia pacific", "middle east", "mena", "dach", "benelux", "nordics", "scandinavia",
    "eu", "the americas", "southeast asia", "oceania", "midwest", "new england", "bay area",
    "silicon valley", "west coast", "east coast",
})
_REGION_WORDS = re.compile(r"\b(greater|area|region|metro|metropolitan|county|state|province"
                           r"|territory|district|coast)\b")
_CITIES = frozenset({
    "new york", "nyc", "los angeles", "chicago", "houston", "phoenix", "philadelphia",
    "san antonio", "san diego", "dallas", "austin", "san jose", "san francisco", "seattle",
    "denver", "boston", "atlanta", "miami", "nashville", "portland", "las vegas", "detroit",
    "minneapolis", "charlotte", "raleigh", "salt lake city", "pittsburgh", "baltimore",
    "toronto", "montreal", "vancouver", "calgary", "ottawa", "mexico city", "guadalajara",
    "monterrey", "são paulo", "sao paulo", "rio de janeiro", "buenos aires", "bogotá", "bogota",
    "lima", "santiago", "london", "manchester", "birmingham", "edinburgh", "glasgow", "dublin",
    "paris", "lyon", "marseille", "berlin", "munich", "münchen", "hamburg", "frankfurt",
    "cologne", "köln", "düsseldorf", "stuttgart", "amsterdam", "rotterdam", "brussels",
    "antwerp", "zurich", "zürich", "geneva", "vienna", "wien", "madrid", "barcelona",
    "valencia", "lisbon", "porto", "milan", "milano", "rome", "roma", "turin", "stockholm",
    "oslo", "copenhagen", "helsinki", "warsaw", "kraków", "krakow", "prague", "budapest",
    "bucharest", "athens", "istanbul", "tel aviv", "dubai", "abu dhabi", "riyadh", "doha",
    "cairo", "lagos", "nairobi", "johannesburg", "cape town", "mumbai", "delhi", "new delhi",
    "bangalore", "bengaluru", "hyderabad", "chennai", "pune", "kolkata", "singapore",
    "hong kong", "shanghai", "beijing", "shenzhen", "guangzhou", "tokyo", "osaka", "seoul",
    "taipei", "bangkok", "jakarta", "kuala lumpur", "manila", "ho chi minh city", "hanoi",
    "sydney", "melbourne", "brisbane", "perth", "auckland", "wellington",
})
# Country names that are also common given names: a q naming a person must not read as a place.
_NOT_PLACES = frozenset({"chad", "jordan"})
_PLACE_RX: re.Pattern | None = None


@cache
def _country_names() -> frozenset[str]:
    return P.country_names() | _COUNTRY_ALIASES


def _place_rx() -> re.Pattern:
    global _PLACE_RX
    if _PLACE_RX is None:
        names = (_country_names() | _REGIONS | _CITIES) - _NOT_PLACES
        alternation = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
        _PLACE_RX = re.compile(rf"(?<!\w)(?:{alternation})(?!\w)")
    return _PLACE_RX


def iso2(value: Any) -> str | None:
    """`us`, `US`, `United States`, `uk` -> `US`/`GB`; None when the value names no ISO country."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip().lower()
    alias = {"usa": "us", "u.s.": "us", "u.s.a.": "us", "united states of america": "us",
             "america": "us", "uae": "ae", "england": "gb", "scotland": "gb", "wales": "gb",
             "great britain": "gb", "britain": "gb", "northern ireland": "gb"}.get(v)
    code = alias or P.iso_country(v)
    return code.upper() if code and len(code) == 2 else None


def location_country(value: Any) -> str | None:
    """The ISO2 country a free-text location names (`Berlin, Germany` -> `DE`, `Austin, Texas` ->
    `US`), from its comma parts, last first. A two-letter part is never read as a country code:
    `San Francisco, CA` names a state, not Canada. None when no part names a country."""
    for part in reversed([p.strip() for p in _text(value).split(",") if p.strip()]):
        if part in _US_STATES:
            return "US"
        if (len(part) > 3 or part in _COUNTRY_ALIASES) and (code := iso2(part)) is not None:
            return code
    return None


def _is_iso2(value: str) -> bool:
    return len(value) == 2 and value.isupper() and iso2(value) == value


def country_case(value: Any) -> str:
    """How the caller spelled the country: `upper` (`US`), `lower` (`us`), `name` (`Germany`),
    `other` (`Us`, digits) or `none`."""
    if not isinstance(value, str) or not value.strip():
        return "none"
    v = value.strip()
    if len(v) <= 3 and v.isalpha():
        return "upper" if v.isupper() else "lower" if v.islower() else "other"
    return "name" if re.fullmatch(r"[^\W\d_]+(?:[ .'-]+[^\W\d_]+)*\.?", v) else "other"


def location_kind(value: Any) -> str:
    """`none`, `country_name` (`Germany`, `USA`), `region` (`Oregon, United States`,
    `Greater Phoenix`, `EMEA`), `city` (`Paris`, `Austin, TX`) or `unknown`."""
    t = _text(value)
    if not t:
        return "none"
    parts = [p.strip() for p in t.split(",") if p.strip()]
    first = parts[0] if parts else t
    if len(parts) == 1 and (first in _country_names() or iso2(first) is not None and len(first) > 3):
        return "country_name"
    if first in _REGIONS or _REGION_WORDS.search(first):
        return "region"
    if first in _CITIES or len(parts) >= 2:
        return "city"
    return "unknown"


def has_place(text: Any) -> str:
    t = _text(text)
    if not t:
        return "none"
    return "true" if _place_rx().search(t) else "false"


# ---- people.search ------------------------------------------------------------------------------

LIMIT_BUCKETS = frozenset({"<=5", "6-10", "11-25", ">25", "none"})
Q_WORDS = frozenset({"1-3", "4-8", "9+", "none"})
TITLE_WORDS = frozenset({"1-2", "3-4", "5+", "none"})


def limit_bucket(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return "none"
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "none"
    return "<=5" if n <= 5 else "6-10" if n <= 10 else "11-25" if n <= 25 else ">25"


def _given(value: Any) -> str:
    present = bool(_text(value)) if not isinstance(value, list) else any(_text(v) for v in value)
    return "true" if present else "false"


def people_search(identity: dict) -> dict[str, str]:
    """The categories of one people search, from its identity and filters after derivation."""
    title, q = identity.get("title"), identity.get("q")
    country, location = identity.get("country"), identity.get("location")
    out = {
        "title_class": title_class(title),
        "title_function": title_function(title),
        "title_seniority": title_seniority(title),
        "title_lang": lang(title),
        "title_words": words_bucket(title, ((2, "1-2"), (4, "3-4")), "5+"),
        "has_country": _given(country),
        "country_raw_case": country_case(country),
        "has_location": _given(location),
        "location_kind": location_kind(location),
        "limit_bucket": limit_bucket(identity.get("limit")),
        "q_words": words_bucket(q, ((3, "1-3"), (8, "4-8")), "9+"),
        "q_lang": lang(q),
        "q_has_place": has_place(q),
        "name_given": _given(identity.get("full_name")),
        "has_keywords": _given(identity.get("keywords")),
    }
    if (code := iso2(country)) is not None:
        out["country_iso2"] = code
    return out


def _row_text(row: dict, *keys: str) -> str:
    for k in keys:
        v = row.get(k)
        if isinstance(v, dict):
            v = v.get("name")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _squash(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.casefold())


def _host(value: str) -> str:
    v = re.sub(r"^[a-z][a-z0-9+.-]*://", "", value.strip().lower())
    v = re.split(r"[/?#:]", v, maxsplit=1)[0]
    return v[4:] if v.startswith("www.") else v


def _profile_url(row: dict) -> str | None:
    url = _row_text(row, "profile_url", "linkedin_url", "linkedinUrl", "url")
    m = re.fullmatch(r"https?://([^/?#\s]+)(\S*)", url, re.IGNORECASE)
    return url if m and "." in m.group(1) else None


def _person_key(row: dict) -> str | None:
    if (url := _profile_url(row)) is not None:
        handle = P.linkedin_handle(url)
        return "u:" + (handle.lower().strip("/") if handle
                       else re.sub(r"^https?://(www\.)?", "", url.lower()).rstrip("/"))
    name = _squash(_row_text(row, "name", "full_name", "fullName"))
    return f"n:{name}|{_squash(_row_text(row, 'company', 'company_name', 'companyName'))}" if name else None


# Legal-form words a company name may end in: `Stripe, Inc.` is the company at stripe.com.
_LEGAL_FORMS = frozenset({
    "inc", "incorporated", "llc", "llp", "ltd", "limited", "corp", "corporation", "co", "company",
    "plc", "gmbh", "ag", "sa", "sas", "sarl", "srl", "spa", "bv", "nv", "ab", "as", "oy", "kk",
    "pte", "pty", "pvt", "lda", "ltda"})


def _company_match(row: dict, domain: str) -> bool:
    """The row's company is the asked one: its domain is the asked domain or a subdomain of it,
    or, for a row with no domain, its name minus a legal form is the domain's first label.
    A longer name is another company (`MetaMask` is not meta.com, `Applebee's` not apple.com)."""
    row_domain = _host(_row_text(row, "company_domain", "domain", "company_website", "website"))
    if row_domain:
        return row_domain == domain or row_domain.endswith("." + domain)
    label = _squash(domain.split(".", 1)[0])
    words = re.findall(r"[^\W_]+", _row_text(row, "company", "company_name", "companyName", "organization").casefold())
    while len(words) > 1 and words[-1] in _LEGAL_FORMS:
        words.pop()
    return bool(label) and "".join(words) == label


def people_search_answer(asked: dict, output: dict) -> dict[str, str]:
    """Counts over one people search's rows, judged against what was asked: no row value leaves.
    A check that does not apply to the question (no company, no classifiable title, no country)
    is absent rather than zero."""
    rows = output.get("people")
    if not isinstance(rows, list):
        return {}
    rows = [r for r in rows if isinstance(r, dict)]
    out = {"rows": len(rows), "rows_with_profile_url": sum(_profile_url(r) is not None for r in rows)}
    seen: set[str] = set()
    dup = 0
    for r in rows:
        if (k := _person_key(r)) is not None:
            dup += k in seen
            seen.add(k)
    out["rows_dup"] = dup
    domain = _host(asked.get("company_domain")) if isinstance(asked.get("company_domain"), str) else ""
    if domain:
        out["rows_company_match"] = sum(_company_match(r, domain) for r in rows)
    fn = title_function(asked.get("title"))
    if fn not in ("other", "none"):
        out["rows_title_match"] = sum(title_function(_row_text(r, "title", "headline", "job_title")) == fn
                                      for r in rows)
    if (code := iso2(asked.get("country"))) is not None:
        out["rows_country_match"] = sum(location_country(_row_text(r, "location", "country")) == code
                                        for r in rows)
    return {k: str(v) for k, v in out.items()}


PEOPLE_SEARCH = Segmenter(
    version="1",
    vocab={
        "title_class": TITLE_CLASSES, "title_function": FUNCTIONS, "title_seniority": SENIORITIES,
        "title_lang": LANGS, "title_words": TITLE_WORDS, "has_country": TRUE_FALSE,
        "country_iso2": _is_iso2,
        "country_raw_case": frozenset({"upper", "lower", "name", "other", "none"}),
        "has_location": TRUE_FALSE,
        "location_kind": frozenset({"city", "region", "country_name", "unknown", "none"}),
        "limit_bucket": LIMIT_BUCKETS, "q_words": Q_WORDS, "q_lang": LANGS,
        "q_has_place": TRUE_FALSE | {"none"}, "name_given": TRUE_FALSE, "has_keywords": TRUE_FALSE,
    },
    compute=people_search,
    answer=people_search_answer,
)

SEGMENTERS: dict[str, Segmenter] = {"people_search": PEOPLE_SEARCH}
