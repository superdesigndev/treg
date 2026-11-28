"""Strict-filter row checks: which rows of a routed list answer break a filter the caller sent.

A contract's `row_filters` maps a caller key to a test and the row field(s) it reads. Under
`X-Treg-Route-Strict-Filters` the router leaves out of `output` every row whose own field breaks
the sent value; `raw` keeps the provider's body whole. A row that carries no comparable field is
never judged: it stays, counted as unchecked. Pure."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from . import paths as P

TESTS = frozenset({"place", "domain", "site", "within_days"})

# Words a place name carries around its name ("Greater Phoenix Area", "New York City"): the place is the rest.
_PLACE_NOISE = frozenset({"greater", "area", "metropolitan", "metro", "region", "city", "bay"})
_COUNTRY_ALIASES = {"usa": "us", "u s": "us", "u s a": "us", "england": "gb", "scotland": "gb",
                    "wales": "gb", "northern ireland": "gb", "great britain": "gb"}
_US_STATES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar", "california": "ca",
    "colorado": "co", "connecticut": "ct", "delaware": "de", "district of columbia": "dc",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id", "illinois": "il",
    "indiana": "in", "iowa": "ia", "kansas": "ks", "kentucky": "ky", "louisiana": "la",
    "maine": "me", "maryland": "md", "massachusetts": "ma", "michigan": "mi", "minnesota": "mn",
    "mississippi": "ms", "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny",
    "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok", "oregon": "or",
    "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc", "south dakota": "sd",
    "tennessee": "tn", "texas": "tx", "utah": "ut", "vermont": "vt", "virginia": "va",
    "washington": "wa", "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
}
_STATE_CODES = frozenset(_US_STATES.values())
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


@dataclass(frozen=True)
class RowFilter:
    key: str                  # the caller's identity key or filter
    test: str                 # one of TESTS
    fields: tuple[str, ...]   # row paths, the first one present is read


@dataclass
class Result:
    rows: list
    removed: int = 0
    unchecked: int = 0
    by: dict[str, int] = field(default_factory=dict)

    def note(self) -> dict:
        return {"removed": self.removed, "kept": len(self.rows), "by": dict(self.by),
                **({"unchecked": self.unchecked} if self.unchecked else {}), "in_raw": True}


def _segments(value: Any) -> list[str]:
    return [f for s in P._strings(value) for part in s.split(",") if (f := P._fold(part))]


def _bare(segment: str) -> str:
    words = [w for w in segment.split() if w not in _PLACE_NOISE]
    return " ".join(words) or segment


def _state_name(segment: str) -> str | None:
    """The state a part names in full ('Texas Metropolitan Area' is Texas); 'city' is kept, so
    'Kansas City' is no state."""
    words = [w for w in segment.split() if w not in _PLACE_NOISE - {"city"}]
    return _US_STATES.get(" ".join(words))


def _country(text: str) -> str | None:
    return _COUNTRY_ALIASES.get(text) or P.iso_country(text)


@dataclass
class _Place:
    city: str = ""
    state: str = ""
    country: str = ""
    either: frozenset[str] = frozenset()   # a row's two-letter code that is a US state and a country ('IN', 'CA')


def _code(segment: str) -> str:
    tokens = segment.split()
    return tokens[0] if len(tokens) <= 2 and len(tokens[0]) == 2 else ""


def _read_place(segments: list[str], *, asked: bool) -> _Place:
    """The city, US state and country a place names. The first part is a state only when no later
    part names a state, else a country when it is the only part, else the city. A later part is a
    state by name or code ('TX', 'NY 14607'), else a country. A code that is both a state and a
    country is the state when the caller wrote it after a city, or when a country follows it in a
    row; otherwise a row's code is either. A state names its country."""
    out = _Place()
    either: set[str] = set()
    later_state = any(_state_name(seg) or (_code(seg) in _STATE_CODES) for seg in segments[1:])
    for i, seg in enumerate(segments):
        if i == 0:
            if (state := _state_name(seg)) and not later_state:
                out.state = state
            elif len(segments) == 1 and (code := _country(_bare(seg))):
                out.country = code
            else:
                out.city = _bare(seg)
            continue
        code = _code(seg)
        if state := _state_name(seg):
            out.state = out.state or state
        elif code in _STATE_CODES and _country(code) and not asked:
            if any(_country(_bare(later)) for later in segments[i + 1:]):
                out.state = out.state or code
            else:
                either.add(code)
        elif code in _STATE_CODES:
            out.state = out.state or code
        elif country := _country(_bare(seg)):
            out.country = out.country or country
    if out.state and not out.country:
        out.country = "us"
    out.either = frozenset(either)
    return out


def place(sent: Any, value: Any) -> bool | None:
    """False only when the row clearly names another place than the one asked: another country,
    another US state, or another city in the same US state. True when it names the asked place at
    its most specific level ('Austin, TX, US' for 'Austin, Texas'). Anything else is not judged: a
    row naming only a larger place ('Texas' for Austin), another city outside the US (a city's
    other-language name reads as another city), a city asked with nothing around it, and a row
    whose two-letter code could be the place asked ('Indianapolis, IN' for India)."""
    if not isinstance(sent, str):
        return None
    asked_segs = [f for part in sent.split(",") if (f := P._fold(part))]
    segs = _segments(value)
    if not asked_segs or not segs:
        return None
    if asked_segs == segs:
        return True
    asked, row = _read_place(asked_segs, asked=True), _read_place(segs, asked=False)
    if asked.country and row.country and asked.country != row.country:
        return False
    if asked.state and row.state and asked.state != row.state:
        return False
    # A row's code that is a state and a country ('MN') breaks the ask when it is neither the
    # state asked nor, read as a country, the country asked.
    if row.either and not row.state and not row.country and (
            (asked.state and asked.state not in row.either)
            or (asked.country and asked.country != "us" and asked.country not in row.either)):
        return False
    if asked.city:
        if f" {asked.city} " in " " + " ".join(_bare(s) for s in segs) + " ":
            return True
        # A row's either-code that is the asked state: as a state it is the same state, as a
        # country it is another country, so another city there is a mismatch both ways.
        same_state = bool(asked.state) and (row.state == asked.state
                                            or (not row.state and asked.state in row.either))
        return False if row.city and same_state and row.city != asked.city else None
    if asked.state:
        return True if row.state == asked.state else None
    return True if row.country and row.country == asked.country else None


def _host(value: Any) -> str | None:
    if isinstance(value, str) and "@" in value:
        value = value.rsplit("@", 1)[1]
    h = P.host(value) if isinstance(value, str) else None
    h = (h or "").split(":")[0].rstrip(".")
    return h if "." in h and "*" not in h and not h.startswith(".") else None


def domain(sent: Any, value: Any) -> bool | None:
    """True when the row's domain is the sent one or a subdomain of it (a port and a trailing
    dot aside)."""
    want, got = _host(sent), _host(value)
    if not want or not got:
        return None
    return got == want or got.endswith("." + want)


def site(sent: Any, value: Any) -> bool | None:
    """True when the row's link is on a site the question's `site:` restricts to. None when the
    question has no such restriction, a restriction is not one readable host (`site:.edu`,
    `site:*.a.com`), or the link cannot be read as the page (a redirect `/url?q=` link, a
    percent-encoded address)."""
    sites = P.site_restrictions(sent)
    if not sites or any(not _host(h) or "%" in h + p for h, p in sites):
        return None
    if not isinstance(value, str) or not value or "%" in value or "/url?" in value:
        return None
    link = P.host_path(value)
    if not _host(link[0]):
        return None
    link = (_host(link[0]), link[1])
    return any(P.on_site(s, link) for s in sites)


def _day(value: str) -> date | None:
    text = value.strip()
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        moment = None
    if moment is not None:
        return moment.astimezone(UTC).date() if moment.tzinfo else moment.date()
    m = _DATE.match(text)
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def within_days(sent: Any, value: Any, today: date) -> bool | None:
    """True when the row's date, as a UTC day, is at most `sent` days before `today` (a UTC day)."""
    if isinstance(sent, bool) or not isinstance(sent, int) or not isinstance(value, str):
        return None
    day = _day(value)
    return None if day is None else day >= today - timedelta(days=sent)


def _read(row: dict, fields: tuple[str, ...]) -> Any:
    for f in fields:
        v = P.get_path(row, f)
        if v not in (None, "", [], {}):
            return v
    return None


def judge(test: str, sent: Any, value: Any, today: date) -> bool | None:
    if test == "place":
        return place(sent, value)
    if test == "domain":
        return domain(sent, value)
    if test == "site":
        return site(sent, value)
    return within_days(sent, value, today)


def apply(filters: tuple[RowFilter, ...], identity: dict, given: set[str], rows: list, today: date) -> Result | None:
    """The rows that break no sent filter on a field they carry, and how many each filter removed.
    None when no declared filter was sent (nothing to judge)."""
    active = [f for f in filters if f.key in given and identity.get(f.key) not in (None, "", [])
              and (f.test != "site" or P.site_restrictions(identity.get(f.key)))]
    if not active or not isinstance(rows, list):
        return None
    out = Result(rows=[])
    for row in rows:
        verdicts = [(f, judge(f.test, identity.get(f.key), _read(row, f.fields), today))
                    if isinstance(row, dict) else (f, None) for f in active]
        failed = next((f for f, ok in verdicts if ok is False), None)
        if failed is not None:
            out.removed += 1
            out.by[failed.key] = out.by.get(failed.key, 0) + 1
            continue
        if any(ok is None for _, ok in verdicts):
            out.unchecked += 1
        out.rows.append(row)
    return out


def parse(cap: str, raw: Any, keys: set[str], row_fields: dict | None) -> tuple[RowFilter, ...]:
    """`row_filters: {location: {test: place, field: location}}`: each key a caller identity key
    or filter, each test one of TESTS, each field a row path (a row field of the contract's `row`
    when it has one) or a list of them."""
    if not raw:
        return ()
    if not isinstance(raw, dict):
        raise ValueError(f"contract {cap}: row_filters maps a caller key to {{test, field}}")
    out = []
    for key, spec in raw.items():
        fields = spec.get("field") if isinstance(spec, dict) else None
        fields = [fields] if isinstance(fields, str) else fields
        if (key not in keys or not isinstance(spec, dict) or set(spec) != {"test", "field"}
                or spec["test"] not in TESTS or not isinstance(fields, list) or not fields
                or not all(isinstance(f, str) and f for f in fields)
                or (row_fields and any(f not in row_fields for f in fields))):
            # A misnamed key or field would filter nothing, silently.
            raise ValueError(f"contract {cap}: row_filters.{key} needs a caller key, test one of "
                             f"{sorted(TESTS)} and field (row paths{', fields of its row' if row_fields else ''}), got {spec!r}")
        out.append(RowFilter(str(key), str(spec["test"]), tuple(fields)))
    return tuple(out)
