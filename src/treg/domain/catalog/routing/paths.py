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


def place(*parts: Any) -> str | None:
    """`place(city, state, country)` → "Berlin, Germany": the non-empty parts, in order, each once."""
    seen: list[str] = []
    for p in parts:
        if isinstance(p, str) and p.strip() and p.strip() not in seen:
            seen.append(p.strip())
    return ", ".join(seen) or None


def texts(v: Any) -> str | None:
    """`texts(highlights)` — a list of passages as one text; a string passes through."""
    if isinstance(v, list):
        v = " ".join(str(x).strip() for x in v if isinstance(x, str) and x.strip())
    return v if isinstance(v, str) and v.strip() else None


def has_type(items: Any, kind: Any) -> bool:
    return isinstance(items, list) and any(isinstance(i, dict) and i.get("type") == kind for i in items)


def length(items: Any) -> int | None:
    return len(items) if isinstance(items, (list, dict, str)) else None


def dfs_location(country: Any) -> int | None:
    """DataForSEO's location_code for a country: its country codes are Google Ads geo target ids,
    2000 + the ISO 3166-1 numeric code (us 2840, gb 2826, ma 2504). Takes an alpha-2 code, `uk`, or
    a country name. None for a value that names no country, so the adapter sends no location and
    the plan reports `country` as ignored instead of asking about the United States."""
    code = _country_code(country if country not in (None, "") else "us")
    numeric = _table("country_numeric.json").get(code or "")
    return 2000 + int(numeric) if numeric else None


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
    m = re.search(r"linkedin\.com/(?:in|company)/([^/?#]+)", v, re.I)
    return m.group(1) if m else (v if "/" not in v else None)


def linkedin_url(v: Any) -> str | None:
    """A handle → the public profile URL; a URL passes through; a scheme-less URL
    (`linkedin.com/in/x`, `www.linkedin.com/in/x`) gets `https://`. Anything else that is not
    handle-shaped (no letter or digit, or a `/`, `?`, `#` or space in it: `N/A`, `-`) is None, never `/in/N/A`."""
    if not isinstance(v, str) or not v:
        return None
    v = v.strip()
    if v.startswith("http"):
        return v
    m = re.match(r"^((?:[a-z]{2,3}\.)?(?:www\.)?linkedin\.com)(/.*)$", v, re.I)  # anchored: the HOST is linkedin, not a path that mentions it
    if m:
        return f"https://{m.group(1).lower()}{m.group(2)}"   # lower-cased host so linkedin_handle() can derive from it
    handle = v.strip("/")
    if not re.search(r"[^\W_]", handle) or re.search(r"[/?#\s]", handle):
        return None
    return f"https://www.linkedin.com/in/{handle}"


def free_mail(domain: Any) -> bool:
    """True for a mailbox domain anyone can open (gmail.com, outlook.com, ...)."""
    return isinstance(domain, str) and domain.lower() in _FREE_MAIL


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


_TABLES: dict[str, dict[str, str]] = {}


def _table(name: str) -> dict[str, str]:
    """An ISO 3166 table keyed by lowercase alpha-2 code, generated from pycountry: `countries.json`
    (common names), `country_numeric.json` (the numeric code)."""
    if name not in _TABLES:
        from pathlib import Path
        _TABLES[name] = json.loads((Path(__file__).resolve().parents[3] / "catalog" / name).read_text())
    return _TABLES[name]


def _country_code(v: Any) -> str | None:
    """`TH`, `th`, `Thailand` or `uk` -> `th`/`gb`; None when the value names no ISO country."""
    if not isinstance(v, str) or not v.strip():
        return None
    key = v.strip().lower()
    if key == "uk":
        return "gb"
    names = _table("countries.json")
    if key in names:
        return key
    return next((code for code, name in names.items() if name.lower() == key), None)


def country_code(v: Any) -> Any:
    """`country_code('United Kingdom')` → 'gb': the ISO 3166 alpha-2 code, lowercase, for providers that
    take only a code. A value that names no ISO country passes through, so the provider still says why."""
    return _country_code(v) or v


def country_name(v: Any) -> str | None:
    """`country_name('fr')` → 'France' — the ISO 3166 alpha-2 table (catalog/countries.json, generated
    from pycountry, common names). Providers that filter on a location NAME (icypeas, lusha, apollo)
    get this; code-taking providers keep the code. A name passes through."""
    if not isinstance(v, str) or not v.strip():
        return None
    return _table("countries.json").get(v.strip().lower(), v if len(v) > 3 else None)


def as_list(v: Any) -> Any:
    """A scalar the provider wants as a one-element array (`domains: ["stripe.com"]`)."""
    if v is None:
        return None
    return v if isinstance(v, list) else [v]


# Role words in a job title → the seniority and department a people search can filter by. Checked
# in order; the first seniority that matches wins, every department that matches is kept.
_SENIORITY = (
    ("owner_founder", r"\b(owner|founder|co-?founder|proprietor)\b"),
    ("c_level", r"\b(c[a-z]o|chief\b|president|managing director|general manager)\b"),
    ("vp", r"\b(vp|svp|evp|vice[ -]president)\b"),
    ("director", r"\b(director|head of|head)\b"),
    ("manager", r"\b(manager|lead|supervisor)\b"),
    ("intern", r"\b(intern|trainee)\b"),
)
_DEPARTMENT = (
    ("executive", r"\b(ceo|coo|founder|owner|president|managing director|general manager)\b"),
    ("engineering", r"\b(cto|engineer\w*|developer|devops|software)\b"),
    ("product", r"\b(cpo|product)\b"),
    ("sales", r"\b(cro|sales|account executive|business development|bdr|sdr)\b"),
    ("marketing", r"\b(cmo|marketing|growth|brand|seo|content)\b"),
    ("finance", r"\b(cfo|finance|financial|accounting|controller)\b"),
    ("hr", r"\b(chro|hr|human resources|people|talent|recruit\w*)\b"),
    ("operations", r"\b(operations|ops)\b"),
    ("legal", r"\b(legal|counsel|general counsel)\b"),
    ("it", r"\b(cio|it|information technology|security|ciso)\b"),
    ("design", r"\b(design\w*|ux|ui)\b"),
)


def _role_texts(v: Any) -> list[str]:
    items = v if isinstance(v, list) else [v]
    return [str(x).lower() for x in items if x not in (None, "")]


def role_seniority(v: Any) -> list[str] | None:
    """`role_seniority(['CMO', 'VP Marketing'])` → ['c_level', 'vp']: each title's seniority, once."""
    out: list[str] = []
    for text in _role_texts(v):
        hit = next((name for name, rx in _SENIORITY if re.search(rx, text)), None)
        if hit and hit not in out:
            out.append(hit)
    return out or None


def role_department(v: Any) -> list[str] | None:
    """`role_department(['CMO', 'Head of Growth'])` → ['marketing']: every department a title names."""
    out: list[str] = []
    for text in _role_texts(v):
        for name, rx in _DEPARTMENT:
            if re.search(rx, text) and name not in out:
                out.append(name)
    return out or None


def scalar(v: Any) -> Any:
    """A one-element list → its element; anything else unchanged (`['stripe.com']` → 'stripe.com')."""
    return v[0] if isinstance(v, list) and len(v) == 1 else v


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


def count_with(items: Any, path: Any) -> int | None:
    """`count_with(people, 'email.value')` → how many rows carry a non-empty value at `path`."""
    if not isinstance(items, list) or not isinstance(path, str):
        return None
    return sum(1 for item in items if get_path(item, path) not in (None, "", [], {}))


_SITE = re.compile(r"(?:^|[\s(])site:([^\s()]+)", re.I)


def _host_path(url: str) -> tuple[str, str]:
    from urllib.parse import urlsplit
    u = url.strip()
    parts = urlsplit(u if "//" in u else "//" + u)
    host = (parts.hostname or "").lower().removeprefix("www.")
    return host, parts.path.rstrip("/").lower()


def no_result_on_site(q: Any, rows: Any) -> bool:
    """True when the question restricts results to sites (`site:stripe.com`, `site:a.com/blog`, several
    joined by OR) and not one row's link is on any of them: a subdomain counts, and a path in the
    restriction must prefix the row's path. A question with no `site:` (a negated `-site:` is not a
    restriction), no rows, or no row with a readable link is never judged."""
    if not isinstance(q, str) or not isinstance(rows, list) or not rows:
        return False
    sites = [_host_path(s) for s in _SITE.findall(q)]
    sites = [(h, p) for h, p in sites if h]
    if not sites:
        return False
    links = []
    for row in rows:
        if isinstance(row, dict):
            link = next((row[k] for k in ("url", "link", "href") if isinstance(row.get(k), str) and row[k]), None)
            if link:
                links.append(_host_path(link))
    if not links:
        return False
    def on(site, link):
        (sh, sp), (lh, lp) = site, link
        return (lh == sh or lh.endswith("." + sh)) and (not sp or lp == sp or lp.startswith(sp + "/"))
    return not any(on(s, ln) for s in sites for ln in links)


_QUOTED = re.compile(r'"([^"]{2,})"|\u201c([^\u201d]{2,})\u201d')


def _fold(text: str) -> str:
    """Lowercase, accents stripped, punctuation to spaces: `Café-Société` and `cafe societe` meet."""
    import unicodedata
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def _strings(v: Any) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [s for x in v.values() for s in _strings(x)]
    if isinstance(v, list):
        return [s for x in v for s in _strings(x)]
    return []


def quoted_absent(q: Any, rows: Any) -> bool:
    """True when the question quotes phrases (`"Dolby Family Ventures"`) and not one row mentions any of
    them in any of its text (title, snippet, link: every string the row carries), compared without
    case, accents or punctuation. A phrase the search operator excludes (`-"careers"`,
    `-intitle:"jobs"`) is not asked for. A question with no quoted phrase, or no rows, is never judged."""
    if not isinstance(q, str) or not isinstance(rows, list) or not rows:
        return False
    phrases = [_fold(m.group(1) or m.group(2)) for m in _QUOTED.finditer(q)
               if not re.search(r"\S*$", q[:m.start()]).group().startswith("-")]
    phrases = [p for p in phrases if p]
    if not phrases:
        return False
    texts = [" " + _fold(" ".join(_strings(r))) + " " for r in rows if isinstance(r, dict)]
    if not texts:
        return False
    return not any(" " + p + " " in t for p in phrases for t in texts)


_LOCALE_SEGMENT = re.compile(r"[a-z]{2}(?:[-_][a-z]{2})?")


def lost_path(requested: Any, final: Any) -> bool:
    """True when the page asked for had a path but the provider ended on the site's home page: the
    root, or one locale segment (`/`, `/en`, `/en-us/`). A provider that reports where it finally
    read the page names both fields in its miss rule, so a redirect to a storefront or a sign-in
    root is not taken for the page. Missing either address is never a loss."""
    from urllib.parse import urlsplit
    if not isinstance(requested, str) or not isinstance(final, str) or not requested or not final:
        return False
    asked = [s for s in urlsplit(requested).path.split("/") if s]
    ended = [s for s in urlsplit(final).path.split("/") if s]
    if not asked or len(ended) > 1:
        return False
    if ended and not _LOCALE_SEGMENT.fullmatch(ended[0].lower()):
        return False
    return len(asked) > len(ended)


def http_error(v: Any) -> bool:
    """True when `v` is an HTTP error status, 400 to 599 (an int or a numeric string). A provider that
    answers 200 while reporting the TARGET's own status in the body names that field in its miss rule,
    so the target's error page is not an extracted page."""
    try:
        return 400 <= int(v) <= 599
    except (TypeError, ValueError):
        return False


def starts_with(v: Any, prefix: Any) -> bool:
    return isinstance(v, str) and prefix is not None and v.startswith(str(prefix))


TRANSFORMS = {"values": values, "get": get_path, "null_if": null_if, "choose": choose, "split_first": split_first, "split_last": split_last, "join": join, "place": place, "texts": texts, "has_type": has_type, "len": length,
              "dfs_location": dfs_location, "seranking_source": seranking_source, "lower": lower, "upper": upper,
              "list": as_list, "at_least": at_least, "at_most": at_most, "ceil_to": ceil_to, "linkedin_handle": linkedin_handle, "linkedin_url": linkedin_url,
              "email_domain": email_domain, "host": host, "fmt": fmt, "obj": obj, "tca_filter": tca_filter, "csv": csv, "country_name": country_name, "country_code": country_code,
              "e164_digits": e164_digits, "starts_with": starts_with, "http_error": http_error, "lost_path": lost_path, "no_result_on_site": no_result_on_site, "quoted_absent": quoted_absent, "with_country_code": with_country_code,
              "count_with": count_with, "role_seniority": role_seniority, "role_department": role_department,
              "scalar": scalar}

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


_PATH_STEP = re.compile(r"[A-Za-z_][\w-]*(?:\[\d+\])?|\[\d+\]")


def parse(expr: str) -> None:
    """Raise ValueError unless `evaluate` reads `expr` as written: every call names a known
    transform, every path is dotted steps, and the parentheses balance."""
    e = expr.strip() if isinstance(expr, str) else ""
    if not e:
        raise ValueError(f"empty expression {expr!r}")
    m = _CMP.match(e)
    if m and not _CALL.match(e):
        left, _, right = m.groups()
        parse(left)
        if _literal(right) is _MISSING:
            parse(right)
        return
    m = _DIV.match(e)
    if m and not _CALL.match(e):
        parse(m.group(1))
        return
    m = _CALL.match(e)
    if m:
        name, args = m.group(1), m.group(2)
        if name != "coalesce" and name not in TRANSFORMS:
            raise ValueError(f"unknown transform {name!r} in {expr!r}")
        depth = 0
        for ch in args:
            depth += {"(": 1, ")": -1}.get(ch, 0)
            if depth < 0:
                break
        if depth:
            raise ValueError(f"unbalanced parentheses in {expr!r}")
        for arg in _split_args(args):
            parse(arg)
        return
    if _literal(e) is not _MISSING and (not re.match(r"^[A-Za-z_]", e) or e in ("null", "true", "false")):
        return
    if e != "." and not all(_PATH_STEP.fullmatch(step) for step in e.split(".")):
        raise ValueError(f"not a path, literal or call: {expr!r}")


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
