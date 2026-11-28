"""Strict-filter row checks: which rows of a routed list answer break a filter the caller sent.

A contract's `row_filters` maps a caller key to a test and the row field(s) it reads. Under
`X-Treg-Route-Strict-Filters` the router leaves out of `output` every row whose own field breaks
the sent value; `raw` keeps the provider's body whole. A row that carries no comparable field is
never judged: it stays, counted as unchecked. Pure."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from datetime import UTC, date, datetime, timedelta
from typing import Any

from . import paths as P

TESTS = frozenset({"place", "domain", "site", "within_days"})

# Words a place name carries around its name ("Greater Phoenix Area", "New York City"): the place is the rest.
_PLACE_NOISE = frozenset({"greater", "area", "metropolitan", "metro", "region", "city", "bay"})
_COUNTRY_ALIASES = {"usa": "us", "u s": "us", "u s a": "us", "england": "gb", "scotland": "gb",
                    "wales": "gb", "northern ireland": "gb", "great britain": "gb"}
_US_STATES = {
    "d c": "dc", "columbia district": "dc", "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar", "california": "ca",
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


# A name that is a US state and a country: never read as one of them alone.
_STATE_OR_COUNTRY = {"georgia": ("ga", "ge")}
# Two-letter country names, not codes: a row that says 'UK' or 'US' names the country in full.
_TWO_LETTER_NAMES = {"uk": "gb", "us": "us"}


@dataclass
class _Place:
    city: str = ""
    state: str = ""
    country: str = ""
    # What one part could be either way: 'Georgia' (state GA, country GE), a lone 'Washington'
    # in a row (the state or the district), or in the ask a code that is a state and a country.
    either_states: frozenset[str] = frozenset()
    either_countries: frozenset[str] = frozenset()
    # A row's two-letter codes no full country settles ('TX', 'SK', 'WA'): a region abroad as
    # often as a US state, so they remove a row only for an ask in the US.
    codes: frozenset[str] = frozenset()


def _code(segment: str) -> str:
    tokens = segment.split()
    return tokens[0] if len(tokens) <= 2 and len(tokens[0]) == 2 else ""


def _full_country(segment: str) -> str | None:
    bare = _bare(segment)
    return _TWO_LETTER_NAMES.get(bare) or (_country(bare) if len(bare) > 2 else None)


def _read_place(segments: list[str]) -> _Place:
    """The city, US state and country the caller asked. The first part is a state only when no
    later part names one, a country when it is the only part, else the city. A later part is a
    state by name or code ('TX', 'NY 14607'), else a country; a code before a country other than
    the US is a region. A code that is both a state and a country is the state when a country
    follows it, else either; 'Georgia' is always either. A state names its country."""
    out = _Place()
    states: set[str] = set()
    countries: set[str] = set()

    def both(state: str, country: str) -> None:
        states.add(state)
        countries.add(country)

    later_state = any(_state_name(seg) or (_code(_bare(seg)) in _STATE_CODES) for seg in segments[1:])
    for i, seg in enumerate(segments):
        if i == 0:
            if (pair := _STATE_OR_COUNTRY.get(_bare(seg))) and not later_state and len(segments) == 1:
                both(*pair)
            elif (state := _state_name(seg)) and not later_state:
                out.state = state
            elif len(segments) == 1 and (code := _country(_bare(seg))):
                out.country = code
            else:
                out.city = _bare(seg)
            continue
        code = _code(_bare(seg))
        after = [c for later in segments[i + 1:] if (c := _full_country(later))]
        if pair := _STATE_OR_COUNTRY.get(_bare(seg)):
            both(*pair)
        elif state := _state_name(seg):
            out.state = out.state or state
        elif code in _STATE_CODES and any(c != "us" for c in after):
            continue   # a region of the country that follows ('Perth, WA, Australia')
        elif code in _STATE_CODES and _country(code):
            if after:
                out.state = out.state or code
            else:
                both(code, code)
        elif code in _STATE_CODES:
            out.state = out.state or code
        elif (len(_bare(seg)) > 2 or i == len(segments) - 1) and (country := _country(_bare(seg))):
            out.country = out.country or country
    if out.state and not out.country:
        out.country = "us"
    if out.state or out.country:
        states, countries = set(), set()   # a confident reading settles the other part
    out.either_states, out.either_countries = frozenset(states), frozenset(countries)
    return out


def _read_row(segments: list[str]) -> _Place:
    """The city, US state and country a row names. Only a full name settles a state or a country
    ('Texas', 'Canada', 'UK'); a two-letter code is a state only before 'United States', else it
    stays a code. 'Georgia' and a lone 'Washington' are either. A state names its country."""
    out = _Place()
    states: set[str] = set()
    countries: set[str] = set()
    codes: set[str] = set()
    washington = False
    if len(segments) == 1:
        seg = segments[0]
        bare = _bare(seg)
        if country := _TWO_LETTER_NAMES.get(bare):
            out.country = country
        elif len(bare) == 2:
            codes.add(bare)
        elif pair := _STATE_OR_COUNTRY.get(bare):
            states.add(pair[0])
            countries.add(pair[1])
        elif bare == "washington":
            states.update(("wa", "dc"))
        elif state := _state_name(seg):
            out.state = state
        elif country := _country(bare):
            out.country = country
        else:
            out.city = bare
    else:
        later_state = any(_state_name(seg) or _code(_bare(seg)) in _STATE_CODES for seg in segments[1:])
        out.city = "" if (_state_name(segments[0]) and not later_state) else _bare(segments[0])
        if not out.city:
            out.state = _state_name(segments[0]) or ""
        if out.state == "wa" and _bare(segments[0]) == "washington":
            # 'Washington, United States': the state or the district, as a lone 'Washington'.
            out.state, out.city, washington = "", "washington", True
        for i, seg in enumerate(segments[1:], start=1):
            code = _code(_bare(seg))
            if pair := _STATE_OR_COUNTRY.get(_bare(seg)):
                states.add(pair[0])
                countries.add(pair[1])
            elif state := _state_name(seg):
                out.state = out.state or state
            elif country := _full_country(seg):
                out.country = out.country or country
            elif code:
                if code in _STATE_CODES and any(_full_country(later) == "us" for later in segments[i + 1:]):
                    out.state = out.state or code
                else:
                    codes.add(code)
    if out.state and not out.country:
        out.country = "us"
    if out.state or out.country:
        states, countries = set(), set()
    if washington and not out.state:
        states = {"wa", "dc"}
    out.either_states, out.either_countries, out.codes = frozenset(states), frozenset(countries), frozenset(codes)
    return out


def _readings(asked: _Place) -> list[_Place]:
    """An ask whose part is either way ('Mumbai, IN', 'Georgia') is judged under each reading."""
    if not (asked.either_states or asked.either_countries):
        return [asked]
    return ([_Place(city=asked.city, state=s, country="us") for s in asked.either_states]
            + [_Place(city=asked.city, country=c) for c in asked.either_countries])


_US_TERRITORIES = frozenset({"pr", "vi", "gu", "as", "mp"})


def _other_country(a: str, r: str) -> bool:
    """Two different countries, a US territory and the US aside, and the two Congos (their codes
    are mixed up as often as not)."""
    pair = {a, r}
    return a != r and pair != {"cg", "cd"} and not ("us" in pair and pair - {"us"} <= _US_TERRITORIES)


def _judge(asked: _Place, row: _Place) -> bool | None:
    if asked.state and row.country in _US_TERRITORIES:
        return False   # a territory is in no US state
    if asked.country and row.country and _other_country(asked.country, row.country):
        return False
    if asked.state and row.state and asked.state != row.state:
        return False
    # A row's either part ('Georgia', a lone 'Washington') breaks the ask only when it is neither
    # the state asked nor, read as a country, the country asked.
    if row.either_states and not row.state and asked.state and asked.state not in row.either_states:
        return False
    if (row.either_countries and not row.state and not row.country and asked.country and asked.country != "us"
            and asked.country not in row.either_countries):
        return False
    state_codes = row.codes & _STATE_CODES
    in_us = asked.country == "us"
    # A two-letter code removes a row only for an ask in a US state, and only as a US state code.
    if in_us and asked.state and not row.state and state_codes and asked.state not in state_codes:
        return False
    if asked.city:
        # Another city part is not judged: a borough, a county, a metro area or an address reads
        # as another city.
        return True if row.city and f" {asked.city} " in f" {row.city} " else None
    if asked.state:
        return True if row.state == asked.state or (in_us and asked.state in state_codes) else None
    if row.country:
        return True if row.country == asked.country else None
    if in_us and state_codes:
        return True
    # A code that is no US state may confirm the country asked ('Paris, FR'); one that is also a
    # state ('IN') confirms nothing.
    return True if asked.country in row.codes - _STATE_CODES else None


@lru_cache(maxsize=256)
def _asked(sent: str) -> tuple[tuple[str, ...], tuple[_Place, ...]]:
    segs = tuple(f for part in sent.split(",") if (f := P._fold(part)))
    return segs, tuple(_readings(_read_place(list(segs)))) if segs else ()


def _one_place(asked_segs: tuple[str, ...], readings: tuple[_Place, ...], text: str) -> bool | None:
    segs = [f for part in text.split(",") if (f := P._fold(part))]
    if not segs:
        return None
    # The asked place itself, or a one-part row naming its first part ('Lebanon' for 'Lebanon, PA').
    if tuple(segs) == asked_segs or (len(segs) == 1 and _bare(segs[0]) == _bare(asked_segs[0])):
        return True
    row = _read_row(segs)
    verdicts = [_judge(a, row) for a in readings]
    if all(v is False for v in verdicts):
        return False
    return True if any(verdicts) else None


def place(sent: Any, value: Any) -> bool | None:
    """False only when the row clearly names another place than the one asked: within the US
    another state or country, abroad another country. True when the row's city, state or country
    is the asked place at its most specific level ('Austin, TX, US' for 'Austin, Texas'). An ask
    that reads two ways ('Toronto, CA') removes a row only when both readings do, and a row naming
    several places ('New York, NY; San Francisco, CA', a list) only when every one does. Anything
    else is not judged: another city part (a borough, a county, an address), a row naming only a
    larger place ('Texas' for Austin), a city asked with nothing around it, and a row whose part
    could be the place asked ('Indianapolis, IN' for India)."""
    if not isinstance(sent, str):
        return None
    asked_segs, readings = _asked(sent)
    places = [t for s in P._strings(value) for t in re.split(r"[;|]", s) if t.strip()]
    if not asked_segs or not places:
        return None
    verdicts = [_one_place(asked_segs, readings, t) for t in places]
    if all(v is False for v in verdicts):
        return False
    return True if any(verdicts) else None


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
