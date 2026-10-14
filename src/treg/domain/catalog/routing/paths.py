"""The adapters' tiny expression language: dotted paths, `coalesce`, `/ N`, predicates, and a few
named transforms. Small on purpose — anything that needs code is a named transform here, not YAML."""

from __future__ import annotations

import json

import re
from typing import Any

_MISSING = object()
_INDEX = re.compile(r"^(\w+)\[(\d+)\]$")


def get_path(doc: Any, path: str) -> Any:
    """`a.b[0].c` → value or None. Missing anywhere → None (never raises). `.` is the root (a body
    that IS the list: seranking's keyword rows)."""
    if path in (".", ""):
        return doc
    cur = doc
    for seg in path.split("."):
        if cur is None:
            return None
        if re.fullmatch(r"\[(\d+)\]", seg):  # a root-level list: `[0].followers`
            i = int(seg[1:-1])
            cur = cur[i] if isinstance(cur, list) and i < len(cur) else None
            continue
        m = _INDEX.match(seg)
        if m:
            cur = cur.get(m.group(1)) if isinstance(cur, dict) else None
            i = int(m.group(2))
            cur = cur[i] if isinstance(cur, list) and i < len(cur) else None
        elif isinstance(cur, dict):
            cur = cur.get(seg, None)
        else:
            return None
    return cur


def values(value: Any) -> list | None:
    """Read rows from an object keyed by ID/domain, or preserve an existing row list."""
    if isinstance(value, dict):
        return list(value.values())
    return value if isinstance(value, list) else None


def set_path(doc: dict, path: str, value: Any) -> None:
    """`body.enrichmentType.getWorkEmails` → nested set (creating dicts)."""
    cur = doc
    parts = path.split(".")
    for seg in parts[:-1]:
        nxt = cur.get(seg)
        if not isinstance(nxt, dict):
            nxt = cur[seg] = {}
        cur = nxt
    cur[parts[-1]] = value


# ---- named transforms -----------------------------------------------------------------------

def split_first(name: Any) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip().split()[0]


def split_last(name: Any) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return None
    parts = name.strip().split()
    return parts[-1] if len(parts) > 1 else None


def join(*parts: Any) -> str | None:
    words = [str(p).strip() for p in parts if isinstance(p, str) and p.strip()]
    return " ".join(words) if words else None


def has_type(items: Any, kind: Any) -> bool:
    return isinstance(items, list) and any(isinstance(i, dict) and i.get("type") == kind for i in items)


def length(items: Any) -> int | None:
    return len(items) if isinstance(items, (list, dict, str)) else None


# The location table, in miniature (routing plan §1.1 names a full one as an R1 deliverable): the
# countries the SEO providers are actually called for, mapped to each provider's own code.
_DFS_LOCATION = {"us": 2840, "gb": 2826, "uk": 2826, "ca": 2124, "au": 2036, "de": 2276, "fr": 2250, "es": 2724,
                 "it": 2380, "nl": 2528, "in": 2356, "br": 2076, "mx": 2484, "jp": 2392, "sg": 2702, "ie": 2372,
                 "se": 2752, "ch": 2756, "at": 2040, "be": 2056, "pl": 2616, "pt": 2620, "nz": 2554, "za": 2710,
                 "ae": 2784, "kr": 2410, "tr": 2792, "id": 2360, "ph": 2608, "vn": 2704}


def dfs_location(country: Any) -> int:
    return _DFS_LOCATION.get(str(country or "us").lower(), 2840)


def seranking_source(country: Any) -> str:
    c = str(country or "us").lower()
    return "uk" if c == "gb" else c


def lower(v: Any) -> Any:
    return v.lower() if isinstance(v, str) else v


def upper(v: Any) -> Any:
    return v.upper() if isinstance(v, str) else v


def at_least(v: Any, floor: Any) -> Any:
    """A provider minimum (`pagination.size must not be less than 10`, lusha) applied to the
    caller's limit — the caller still pays the provider's price for the rows it insists on."""
    try:
        return max(int(v), int(floor))
    except (TypeError, ValueError):
        return floor


def ceil_to(v: Any, step: Any) -> Any:
    """Round a caller's limit UP to a provider's step (results come in pages of 10): 25 -> 30."""
    try:
        s = int(step)
        return max(s, -(-int(v) // s) * s)
    except (TypeError, ValueError):
        return step


def at_most(v: Any, ceiling: Any) -> int:
    """Cap a provider's page size while keeping the routed quote at that same maximum."""
    try:
        return max(1, min(int(v), int(ceiling)))
    except (TypeError, ValueError):
        return int(ceiling)


def linkedin_handle(v: Any) -> str | None:
    """`https://www.linkedin.com/in/patrickcollison/` → `patrickcollison` (hunter wants the handle)."""
    if not isinstance(v, str) or not v:
        return None
    m = re.search(r"linkedin\.com/(?:in|company)/([^/?#]+)", v)
    return m.group(1) if m else (v if "/" not in v else None)


def linkedin_url(v: Any) -> str | None:
    """A handle → the public profile URL; a URL passes through; a scheme-less URL
    (`linkedin.com/in/x`, `www.linkedin.com/in/x`) gets `https://` — quickenrich 422'd 311 routed
    calls in two days with "linkedin url must be a valid URL" on exactly that shape (2026-09-18)."""
    if not isinstance(v, str) or not v:
        return None
    v = v.strip()
    if v.startswith("http"):
        return v
    m = re.match(r"^((?:[a-z]{2,3}\.)?(?:www\.)?linkedin\.com)(/.*)$", v, re.I)  # anchored: the HOST is linkedin, not a path that mentions it
    if m:
        return f"https://{m.group(1).lower()}{m.group(2)}"   # lower-cased host so linkedin_handle() can derive from it
    return f"https://www.linkedin.com/in/{v.strip('/')}"


def email_domain(v: Any) -> str | None:
    if not isinstance(v, str) or "@" not in v:
        return None
    d = v.rsplit("@", 1)[1].lower()
    return None if d in _FREE_MAIL else d


# Mailboxes anyone can open (global and regional free mail, ISP mail, common disposable and alias services):
# their domain says nothing about the person's company.
_FREE_MAIL = frozenset({
    # global free mail
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com",
    "ymail.com", "rocketmail.com", "aol.com", "aim.com", "icloud.com", "me.com", "mac.com",
    "proton.me", "protonmail.com", "protonmail.ch", "pm.me", "tutanota.com", "tuta.io", "zoho.com",
    "zohomail.com", "gmx.com", "gmx.net", "mail.com", "email.com", "hey.com", "fastmail.com",
    "yandex.com", "yandex.ru", "ya.ru", "mail.ru", "inbox.ru", "list.ru", "bk.ru", "rambler.ru",
    # regional free mail
    "qq.com", "foxmail.com", "163.com", "126.com", "yeah.net", "sina.com", "sina.cn", "sohu.com",
    "aliyun.com", "139.com", "naver.com", "daum.net", "hanmail.net", "nate.com", "yahoo.co.jp",
    "yahoo.co.uk", "yahoo.fr", "yahoo.de", "yahoo.es", "yahoo.it", "yahoo.com.br", "yahoo.co.in",
    "hotmail.co.uk", "hotmail.fr", "hotmail.de", "hotmail.it", "hotmail.es", "outlook.de", "outlook.fr",
    "outlook.es", "outlook.jp", "live.co.uk", "live.fr", "web.de", "gmx.de", "gmx.at", "gmx.ch",
    "t-online.de", "freenet.de", "orange.fr", "wanadoo.fr", "free.fr", "laposte.net", "sfr.fr",
    "libero.it", "virgilio.it", "tiscali.it", "seznam.cz", "wp.pl", "onet.pl", "o2.pl", "interia.pl",
    "uol.com.br", "bol.com.br", "terra.com.br", "rediffmail.com",
    # ISP mail
    "comcast.net", "verizon.net", "att.net", "sbcglobal.net", "bellsouth.net", "cox.net", "charter.net",
    "earthlink.net", "optonline.net", "btinternet.com", "sky.com", "virginmedia.com", "talktalk.net",
    "shaw.ca", "rogers.com", "sympatico.ca", "bigpond.com", "optusnet.com.au", "xtra.co.nz",
    # disposable
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "temp-mail.org", "tempmail.com",
    "yopmail.com", "trashmail.com", "sharklasers.com", "getnada.com", "dispostable.com",
    "maildrop.cc", "throwawaymail.com", "mohmal.com", "emailondeck.com", "fakeinbox.com",
    # forwarding aliases: a real company's site, but anyone can get an address there
    "duck.com", "privaterelay.appleid.com", "mozmail.com", "relay.firefox.com", "simplelogin.com",
    "simplelogin.co", "simplelogin.fr", "slmail.me", "aleeas.com", "8alias.com", "addy.io", "anonaddy.com",
    "anonaddy.me", "passmail.net", "passmail.com", "passinbox.com", "33mail.com",
})


def host(v: Any) -> str | None:
    """`https://www.stripe.com/about` → `stripe.com`; a bare domain passes through."""
    if not isinstance(v, str) or not v:
        return None
    s = re.sub(r"^[a-z]+://", "", v.strip().lower()).split("/")[0].split("?")[0]
    return s[4:] if s.startswith("www.") else s or None


def fmt(template: Any, *args: Any) -> str | None:
    """`fmt('https://www.instagram.com/{}', username)` — a URL from a key. None if any arg is missing."""
    if not isinstance(template, str) or any(a is None for a in args):
        return None
    return template.format(*args)


def obj(*kv: Any) -> dict | None:
    """`obj('domain', company_domain, 'limit', 5)` → {"domain": …, "limit": 5}; missing values are dropped."""
    out = {kv[i]: kv[i + 1] for i in range(0, len(kv) - 1, 2) if kv[i + 1] is not None}
    return out or None


def tca_filter(attribute: Any, value: Any) -> str | None:
    """`tca_filter('about.industries', industry)` — The Companies API's JSON-string `query` filter."""
    if value is None:
        return None
    return json.dumps([{"attribute": attribute, "operator": "or", "sign": "equals", "values": [value]}])


def csv(v: Any) -> str | None:
    """`csv(keywords)` — a list as one comma-separated value (serpapi's `q` for several keywords)."""
    if v is None:
        return None
    return ",".join(str(x) for x in v) if isinstance(v, list) else str(v)


_COUNTRIES: dict[str, str] | None = None


def country_name(v: Any) -> str | None:
    """`country_name('fr')` → 'France' — the ISO 3166 alpha-2 table (catalog/countries.json, generated
    from pycountry, 249 rows, common names). Providers that filter on a location NAME (icypeas,
    lusha, apollo) get this; code-taking providers keep the code. A name passes through."""
    global _COUNTRIES
    if not isinstance(v, str) or not v.strip():
        return None
    if _COUNTRIES is None:
        import json
        from pathlib import Path
        _COUNTRIES = json.loads((Path(__file__).resolve().parents[3] / "catalog" / "countries.json").read_text())
    return _COUNTRIES.get(v.strip().lower(), v if len(v) > 3 else None)


def as_list(v: Any) -> Any:
    """A scalar the provider wants as a one-element array (`domains: ["stripe.com"]`)."""
    if v is None:
        return None
    return v if isinstance(v, list) else [v]


def null_if(value: Any, *sentinels: Any) -> Any:
    """Convert declared empty markers to null; retain the original nonempty value."""
    key = value.strip().lower() if isinstance(value, str) else value
    return None if key in sentinels else value


def choose(condition: Any, when_true: Any, when_false: Any) -> Any:
    """Select a value for a request-dependent adapter rule."""
    return when_true if condition else when_false


def e164_digits(v: Any) -> str | None:
    """`+44 7790 606023` or `0044…` → `447790606023`; None for a number not written internationally.

    Digits alone are never trusted: a national `6175551212` reads as a `61` (Australian) number."""
    if not isinstance(v, str):
        return None
    s = v.strip()
    if s.startswith("+"):
        digits = re.sub(r"\D", "", s)
    elif s.startswith("00"):
        digits = re.sub(r"\D", "", s)[2:]
    else:
        return None
    return digits if 7 <= len(digits) <= 15 else None


# A national number is trusted only with a country whose numbering plan it fits exactly.
_NATIONAL_PLANS = {"US": ("1", re.compile(r"[2-9]\d{9}")), "CA": ("1", re.compile(r"[2-9]\d{9}"))}


def with_country_code(v: Any, country: Any) -> Any:
    """`6175550142` or `(617) 555-0142` with country `US` → `+16175550142`; anything else comes back unchanged.

    Only national digits (spaces, `-`, `.` and parentheses aside) that fit the plan of a country in
    `_NATIONAL_PLANS` are rewritten."""
    plan = _NATIONAL_PLANS.get(country.strip().upper()) if isinstance(country, str) else None
    digits = re.sub(r"[\s\-.()]", "", v) if isinstance(v, str) else ""
    if plan is None or not plan[1].fullmatch(digits):
        return v
    return f"+{plan[0]}{digits}"


def starts_with(v: Any, prefix: Any) -> bool:
    return isinstance(v, str) and prefix is not None and v.startswith(str(prefix))


TRANSFORMS = {"values": values, "get": get_path, "null_if": null_if, "choose": choose, "split_first": split_first, "split_last": split_last, "join": join, "has_type": has_type, "len": length,
              "dfs_location": dfs_location, "seranking_source": seranking_source, "lower": lower, "upper": upper,
              "list": as_list, "at_least": at_least, "at_most": at_most, "ceil_to": ceil_to, "linkedin_handle": linkedin_handle, "linkedin_url": linkedin_url,
              "email_domain": email_domain, "host": host, "fmt": fmt, "obj": obj, "tca_filter": tca_filter, "csv": csv, "country_name": country_name,
              "e164_digits": e164_digits, "starts_with": starts_with, "with_country_code": with_country_code}

_CALL = re.compile(r"^(\w+)\((.*)\)$")
_DIV = re.compile(r"^(.+?)\s*/\s*(\d+(?:\.\d+)?)$")
_CMP = re.compile(r"^(.+?)\s*(==|!=)\s*(.+)$")


def _literal(tok: str):
    t = tok.strip()
    if t == "null":
        return None
    if t == "[]":
        return []
    if t in ("true", "false"):
        return t == "true"
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"":
        return t[1:-1]
    try:
        return float(t) if "." in t else int(t)
    except ValueError:
        return _MISSING


def evaluate(expr: str, doc: Any) -> Any:
    """Evaluate one adapter expression against `doc` (the provider body, or the contract input)."""
    e = expr.strip()
    m = _CMP.match(e)
    if m and not _CALL.match(e):
        left, op, right = m.groups()
        lv = evaluate(left, doc)
        rv = _literal(right)
        if rv is _MISSING:
            rv = evaluate(right, doc)
        return (lv == rv) if op == "==" else (lv != rv)
    m = _DIV.match(e)
    if m and not _CALL.match(e):
        v = evaluate(m.group(1), doc)
        return (float(v) / float(m.group(2))) if isinstance(v, (int, float)) else None
    m = _CALL.match(e)
    if m:
        name, args = m.group(1), _split_args(m.group(2))
        vals = [evaluate(a, doc) for a in args]
        if name == "coalesce":
            # First non-empty value, else the LAST argument: `coalesce(x, []) == []` must hold
            # when x is missing or empty, or the miss rule written that way never fires.
            return next((v for v in vals if v not in (None, "", [])), vals[-1] if vals else None)
        fn = TRANSFORMS.get(name)
        if fn is None:
            raise ValueError(f"unknown transform {name!r}")
        return fn(*vals)
    lit = _literal(e)
    if lit is not _MISSING and (not re.match(r"^[A-Za-z_]", e) or e in ("null", "true", "false")):
        return lit
    return get_path(doc, e)


def _split_args(s: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [a.strip() for a in out]
