"""Strict-filter row checks: which rows of a routed list answer break a filter the caller sent.

A contract's `row_filters` maps a caller key to a test and the row field(s) it reads. Under
`X-Treg-Route-Strict-Filters` the router leaves out of `output` every row whose own field breaks
the sent value; `raw` keeps the provider's body whole. A row that carries no comparable field is
never judged: it stays, counted as unchecked. Pure."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from . import paths as P

TESTS = frozenset({"place", "domain", "site", "within_days"})

# Words a place name carries around its name ("Greater Phoenix Area"): the place is the rest.
_PLACE_NOISE = frozenset({"greater", "area", "metropolitan", "metro", "region"})
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


def _country(text: str) -> str | None:
    return _COUNTRY_ALIASES.get(text) or P.iso_country(text)


def place(sent: Any, value: Any) -> bool | None:
    """True when the row's place names the most specific part of the sent one ('Austin' of
    'Austin, Texas'), folded, or its country or US-state code as a part of its own ('Paris, FR',
    'Portland, OR'). A row that names only a larger place ('Texas' for Austin) is not in it."""
    if not isinstance(sent, str):
        return None
    asked = [f for part in sent.split(",") if (f := P._fold(part))]
    segs = _segments(value)
    if not asked or not segs:
        return None
    words = [w for w in asked[0].split() if w not in _PLACE_NOISE] or asked[0].split()
    want = " ".join(words)
    if f" {want} " in " " + " ".join(segs) + " ":
        return True
    code = _country(want)
    if code and any(_country(s) == code for s in segs):
        return True
    state = _US_STATES.get(want)
    return bool(state) and any(s.split()[0] == state and len(s.split()) <= 2 for s in segs[1:])


def domain(sent: Any, value: Any) -> bool | None:
    """True when the row's domain is the sent one or a subdomain of it."""
    want, got = P.host(sent), P.host(value) if isinstance(value, str) else None
    if not want or not got:
        return None
    return got == want or got.endswith("." + want)


def site(sent: Any, value: Any) -> bool | None:
    """True when the row's link is on a site the question's `site:` restricts to; None when the
    question has no such restriction."""
    sites = P.site_restrictions(sent)
    if not sites or not isinstance(value, str) or not value:
        return None
    link = P.host_path(value)
    return any(P.on_site(s, link) for s in sites)


def within_days(sent: Any, value: Any, today: date) -> bool | None:
    """True when the row's date (its leading YYYY-MM-DD) is at most `sent` days before today."""
    if isinstance(sent, bool) or not isinstance(sent, int) or not isinstance(value, str):
        return None
    m = _DATE.match(value.strip())
    if not m:
        return None
    try:
        day = date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None
    return day >= today - timedelta(days=sent)


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
