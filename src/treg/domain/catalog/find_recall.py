"""Recall for `/catalog/find` by JOB: which units the relevance judge reads for one query.

`store.candidates` recalls endpoints, one required word at a time, and a job sold by thirty vendors
then spends thirty of the judge's sixty seats or none of them. Here the unit of recall is the job
(a capability), so one seat carries every vendor of it, and the page can list them all once the
judge says the job fits (`application.catalog_find`). Three kinds of unit:

  - a **job**: a capability, its card the id's words, its description, its platform's label and the
    names its members go by;
  - a **representative**: a member endpoint whose own words fit the query better than its job's card
    did, judged on its own (a vendor that only returns personal emails under a work-email job);
  - an **uncatalogued endpoint**: no capability, so it is its own unit.

Two channels score every unit: a whole-word lexical channel (idf over unit cards, light stemming,
the aliases table, a doubled weight for a platform's name) and an optional semantic channel whose
per-unit similarities the caller supplies (empty until vectors exist). Each channel max-pools a
job over its members and remembers which member won; the channels fuse by reciprocal rank; seats go
first to the jobs on a platform the query names, then to the best jobs, then to representatives and
uncatalogued endpoints.

Also the NAME table: a query that is a platform's, a provider's, or a model's name ("tiktok",
"semrush", "seedance") asks what is there, not for a job. Names are matched as strings - a name is
a lookup, not a judgement.

Pure: no I/O. The index is built once per `Catalog` and cached on it, like `store._search_fields`.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from . import store

JOB = "job"
ENDPOINT = "endpoint"

RRF_K = 60
CHANNEL_DEPTH = 300        # units per channel that enter the fusion
PREFIX_MIN = 5             # a query word this long may also be a prefix of a name word
PREFIX_WEIGHT = 0.5        # ...worth half a whole word: "scrap" starts every scrapecreators row
NAME_PREFIX_MIN = 4        # a typed prefix of a provider or platform slug counts from this length
EXAMPLES = 8               # member names on a job card

# Platforms whose endpoint names carry model and product names ("Seedance 2.5", "Nano Banana 2").
PRODUCT_CATEGORY = "AI generation"

_SPLIT = store._SPLIT        # the search tokenizer's cut: letters, digits, CJK
# Function words of the languages people type into the box, beyond the search stopwords.
_ROMANCE = frozenset("""de no les des la le et em os as um uma que qui une un pour moi del el los las
    en con por para da do dos das du au aux se il lo gli""".split())
STOPWORDS = store._STOPWORDS | _ROMANCE | {"many", "get"}


def fold(text: str) -> str:
    """Lowercase with diacritics removed; CJK and digits kept."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def stem(t: str) -> str:
    """Light English stemming, enough that "emails", "scraping" and "companies" meet "email",
    "scrape" and "company" on a card. A final "e" goes too, so the forms of one verb agree."""
    if len(t) >= 6 and t.endswith("ing"):
        t = t[:-3]
    elif len(t) >= 5 and t.endswith("ies"):
        t = t[:-3] + "y"
    elif len(t) >= 5 and t.endswith("es") and (t[-3] in "sxz" or t[-4:-2] in ("ch", "sh")):
        t = t[:-2]
    elif len(t) >= 5 and t.endswith("ed"):
        t = t[:-2]
    elif len(t) >= 4 and t.endswith("s") and not t.endswith("ss"):
        t = t[:-1]
    if len(t) >= 4 and t.endswith("e"):
        t = t[:-1]
    return t


def words(text: str) -> list[str]:
    return [w for w in _SPLIT.split(fold(text)) if w]


def query_words(query: str) -> list[tuple[str, str]]:
    """The query's selecting words as (word, stem): stopwords and single characters out."""
    return [(w, stem(w)) for w in words(query) if w not in STOPWORDS and len(w) >= 2]


def query_tokens(query: str) -> list[str]:
    return [t for _, t in query_words(query)]


@dataclass(frozen=True)
class Unit:
    kind: str                       # JOB | ENDPOINT
    id: str                         # capability id for a job, endpoint id otherwise
    cap: str                        # capability ("" for an uncatalogued endpoint)
    platform: str
    text: str                       # the card
    providers: tuple[str, ...] = ()     # a job's vendors
    examples: tuple[str, ...] = ()      # a job's member names
    members: tuple[str, ...] = ()       # a job's member endpoint ids


@dataclass(frozen=True)
class Candidate:
    """One unit handed to the judge. `via` is "rep" for a representative, else ""."""
    unit: Unit
    via: str = ""


@dataclass(frozen=True)
class NameHit:
    kind: str                       # platform | provider | product
    keys: tuple[str, ...]           # platform slugs (best first) | a provider | endpoint ids
    exact: bool                     # the query IS the name, not a prefix of it
    label: str = ""                 # the product's name as matched


@dataclass
class Index:
    units: list[Unit]
    pos: dict[str, int]                      # endpoint id -> unit
    job_pos: dict[str, int]                  # capability id -> unit (an endpoint may share the id)
    members: dict[int, list[int]]            # job unit -> member endpoint units
    postings: dict[str, list[int]]           # card word (stemmed) -> units
    name_postings: dict[str, list[int]]      # name word (unstemmed) -> units, for prefix hits
    idf: dict[str, float]
    # a platform's name as query tokens -> the platforms it names, longest phrase first:
    # ("tiktok", "ad") -> tiktok-ads, from the slug's words and the label's short form
    platform_phrases: list[tuple[tuple[str, ...], tuple[str, ...]]]
    platforms: dict[str, dict] = field(default_factory=dict)
    # per platform with endpoints: (slug, short label words, label and slug words, featured, jobs)
    platform_names: list[tuple[str, str, list[str], int | None, int]] = field(default_factory=list)
    providers: tuple[str, ...] = ()
    providers_on: dict[str, frozenset[str]] = field(default_factory=dict)   # platform -> its providers
    products: dict[str, tuple[str, ...]] = field(default_factory=dict)   # folded name -> endpoint ids
    product_labels: dict[str, str] = field(default_factory=dict)


def shown_endpoints(cat: store.Catalog) -> list[dict]:
    """What find may answer with: the browse surface, routed parents out."""
    return [e for e in cat.endpoints
            if store.browsable(e) and e.get("kind") != "routed" and not store.paused(e)]


def short_label(label: str) -> str:
    """A platform label without its gloss: "Google Analytics (GA4)" -> "Google Analytics"."""
    return label.split(" — ")[0].split(" (")[0].strip()


def index(cat: store.Catalog) -> Index:
    """The find index for `cat`, built on first use and cached on the instance."""
    cached = getattr(cat, "_find_index", None)
    if cached is None:
        cached = build(cat)
        object.__setattr__(cat, "_find_index", cached)   # frozen dataclass, deliberate
    return cached


def build(cat: store.Catalog) -> Index:
    eps = shown_endpoints(cat)
    by_cap: dict[str, list[dict]] = defaultdict(list)
    for e in eps:
        if e["capability"]:
            by_cap[e["capability"]].append(e)

    def plat_label(slug: str) -> str:
        return cat.platforms.get(slug, {}).get("label", slug)

    units: list[Unit] = []
    for cap, members in by_cap.items():
        names: list[str] = []
        for e in members:
            n = (e.get("name") or "").strip()
            if n and n not in names:
                names.append(n)
        platform = cap.split(".")[0]
        units.append(Unit(
            kind=JOB, id=cap, cap=cap, platform=platform,
            text=f"{cap.replace('.', ' ')}. {cat.capability_titles.get(cap, '')}. "
                 f"{cat.capabilities.get(cap, '')}. {plat_label(platform)}. "
                 + "; ".join(names[:EXAMPLES]),
            providers=tuple(sorted({e["provider"] for e in members})), examples=tuple(names[:6]),
            members=tuple(e["id"] for e in members)))
    for e in eps:
        units.append(Unit(
            kind=ENDPOINT, id=e["id"], cap=e["capability"] or "", platform=e["platform"],
            text=f"{e.get('name') or ''}. {e.get('summary') or ''}. {plat_label(e['platform'])}. {e['provider']}",
            providers=(e["provider"],)))
    pos = {u.id: i for i, u in enumerate(units) if u.kind == ENDPOINT}
    job_pos = {u.id: i for i, u in enumerate(units) if u.kind == JOB}
    members = {i: [pos[m] for m in u.members] for i, u in enumerate(units) if u.kind == JOB}

    postings: dict[str, list[int]] = defaultdict(list)
    name_postings: dict[str, list[int]] = defaultdict(list)
    for i, u in enumerate(units):
        for t in {stem(w) for w in words(u.text)}:
            postings[t].append(i)
        name = f"{u.id} {u.platform} {plat_label(u.platform)} {' '.join(u.providers)}"
        for w in set(words(name)):
            name_postings[w].append(i)
    n = len(units)
    idf = {t: math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in postings.items()}

    providers_on: dict[str, set[str]] = defaultdict(set)
    for e in eps:
        providers_on[e["platform"]].add(e["provider"])
    jobs = defaultdict(int)
    for u in units:
        if u.kind == JOB:
            jobs[u.platform] += 1
    platforms = {slug: p for slug, p in cat.platforms.items() if slug in providers_on}
    products, product_labels = _products(eps, cat)
    return Index(
        units=units, pos=pos, job_pos=job_pos, members=members, postings=dict(postings), name_postings=dict(name_postings),
        idf=idf, platform_phrases=_platform_phrases(platforms), platforms=platforms,
        platform_names=[(slug, " ".join(words(short_label(p.get("label", slug)))),
                         words(f"{p.get('label', '')} {slug} {slug.replace('-', ' ')}"), p.get("featured"), jobs[slug])
                        for slug, p in platforms.items()],
        providers=tuple(sorted({e["provider"] for e in eps})),
        providers_on={k: frozenset(v) for k, v in providers_on.items()},
        products=products, product_labels=product_labels)


def _platform_phrases(platforms: dict[str, dict]) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    by_phrase: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for slug, p in platforms.items():
        for name in (slug.replace("-", " "), short_label(p.get("label", slug))):
            phrase = tuple(query_tokens(name))
            if phrase:
                by_phrase[phrase].add(slug)
    return sorted(((ph, tuple(sorted(slugs))) for ph, slugs in by_phrase.items()), key=lambda t: (-len(t[0]), t[0]))


def named_platforms(tokens: Sequence[str], ix: Index) -> tuple[set[str], set[int]]:
    """The platforms a query names, and which of its token positions name them. Names are token
    sequences, matched longest first and left to right, so "tiktok ads library" names TikTok Ads (not
    TikTok) and "search console clicks" names Search Console."""
    named: set[str] = set()
    covered: set[int] = set()
    i = 0
    while i < len(tokens):
        for phrase, slugs in ix.platform_phrases:
            if tuple(tokens[i:i + len(phrase)]) == phrase:
                named.update(slugs)
                covered.update(range(i, i + len(phrase)))
                i += len(phrase)
                break
        else:
            i += 1
    return named, covered


def _products(eps: list[dict], cat: store.Catalog) -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    """Model and product names: words in the endpoint names on the AI generation platforms that at
    least two of those names share and that names elsewhere rarely use ("gemini" yes, "image" no),
    and that `aliases.yaml` does not already file as a way of saying a job ("tts" is text-to-speech),
    plus the adjacent pairs of them ("nano banana"). On a platform with a single endpoint no two
    names can share a word, so its name's words that appear nowhere else in the catalog count too
    ("jev", the one judge). Each maps, spaces and hyphens folded away, to every shown endpoint whose
    name carries it, on any platform."""
    gen = {slug for slug, p in cat.platforms.items() if p.get("category") == PRODUCT_CATEGORY}
    plat_words = {w for slug, p in cat.platforms.items() for w in words(f"{slug} {p.get('label', '')}")}
    vocabulary = {w for key in cat.aliases for w in words(key)}   # "tts", "t2v": a way of saying a job
    inside: dict[str, set[str]] = defaultdict(set)
    outside: dict[str, int] = defaultdict(int)
    alone: set[str] = set()                                        # words of a one-endpoint platform's name
    size = Counter(e["platform"] for e in eps if e["platform"] in gen)
    name_words = {e["id"]: words(e.get("name") or "") for e in eps}
    for e in eps:
        for w in set(name_words[e["id"]]):
            if e["platform"] in gen:
                inside[w].add(e["id"])
                if size[e["platform"]] == 1:
                    alone.add(w)
            else:
                outside[w] += 1
    product = {w for w, ids in inside.items()
               if len(w) >= 3 and w.isalpha() and w not in STOPWORDS and w not in plat_words and w not in vocabulary
               and ((len(ids) >= 2 and outside[w] <= 3 and outside[w] * 2 < len(ids))
                    or (w in alone and outside[w] == 0))}
    keys: dict[str, set[str]] = defaultdict(set)
    labels: dict[str, str] = {}
    for e in eps:
        ws = name_words[e["id"]]
        for i, w in enumerate(ws):
            if w in product:
                keys[w].add(e["id"])
                labels.setdefault(w, w)
                if i + 1 < len(ws) and ws[i + 1] in product:
                    keys[w + ws[i + 1]].add(e["id"])
                    labels.setdefault(w + ws[i + 1], f"{w} {ws[i + 1]}")
    return {k: tuple(sorted(v)) for k, v in keys.items()}, labels


# ---- channels ------------------------------------------------------------------------------------
def lexical(query: str, ix: Index, aliases: dict[str, list[str]]) -> list[float]:
    """Per unit: the idf of each query word it carries as a whole word (or through an alias; or, at
    half weight, for a word of five letters or more, as the prefix of a word of its id, platform or
    provider), a platform's name counting double."""
    scores = [0.0] * len(ix.units)
    qwords = query_words(query)
    _, platform_positions = named_platforms([t for _, t in qwords], ix)
    for pos, (w, t) in enumerate(qwords):
        hit: set[int] = set(ix.postings.get(t, ()))
        for a in aliases.get(w) or aliases.get(t) or ():
            parts = [stem(x) for x in words(a)]
            if parts:   # a phrase alias ("text-to-video") hits where all its words are
                hit |= set.intersection(*(set(ix.postings.get(x, ())) for x in parts))
        prefix: set[int] = set()
        if len(t) >= PREFIX_MIN:
            for name, units in ix.name_postings.items():
                if name.startswith(t):
                    prefix.update(units)
        prefix -= hit
        if not hit and not prefix:
            continue
        weight = ix.idf.get(t) or math.log(1 + (len(ix.units) + 0.5) / 1.5)
        weight *= 2 if pos in platform_positions else 1
        for i in hit:
            scores[i] += weight
        for i in prefix:
            scores[i] += weight * PREFIX_WEIGHT
    return scores


def pooled(scores: Sequence[float], ix: Index) -> tuple[list[float], dict[int, int | None]]:
    """A job's score is the best of its own card and its members'; `src` names the member that won,
    or None when the job's own card did."""
    out = list(scores)
    src: dict[int, int | None] = {}
    for j, mem in ix.members.items():
        best, bv = None, scores[j]
        for m in mem:
            if scores[m] > bv:
                best, bv = m, scores[m]
        out[j] = bv
        src[j] = best
    return out, src


def fuse(*channels: tuple[Sequence[float], Sequence[float], bool]) -> list[int]:
    """Reciprocal rank fusion over each channel's top units. A channel is (pooled, own, ranked):
    equal pooled scores - one common word hits many jobs through one member each - rank the unit
    whose own card scored higher first. A lexical score of 0 is no hit and admits nothing; a ranked
    channel (similarity) admits its top units whatever their sign, so it can fill the seats alone."""
    acc: dict[int, float] = defaultdict(float)
    for ch, own, ranked in channels:
        order = sorted((i for i, v in enumerate(ch) if ranked or v > 0),
                       key=lambda i: (-ch[i], -own[i], i))[:CHANNEL_DEPTH]
        for rank, i in enumerate(order):
            acc[i] += 1 / (RRF_K + rank)
    return sorted(acc, key=lambda i: (-acc[i], i))


def recall(query: str, ix: Index, aliases: dict[str, list[str]], *,
           semantic: Sequence[float] | None = None, platform: str | None = None,
           n_jobs: int = 25, n_delta: int = 10, n_raw: int = 10, n_plat: int = 8) -> list[Candidate]:
    """The units the judge reads for `query`: jobs, then representatives, then uncatalogued
    endpoints. `semantic` is one similarity per unit (None = that channel is off). `platform` keeps
    one shelf's units only."""
    raw_lex = lexical(query, ix, aliases)
    lex, lsrc = pooled(raw_lex, ix)
    channels = [(lex, raw_lex, False)]
    srcs = [lsrc]
    if semantic is not None:
        sem, ssrc = pooled(semantic, ix)
        channels.append((sem, semantic, True))
        srcs.append(ssrc)
    order = [i for i in fuse(*channels) if not platform or ix.units[i].platform == platform]
    rank = {i: r for r, i in enumerate(order)}

    named, _ = named_platforms(query_tokens(query), ix)
    jobs: list[int] = []
    if named:
        for i in order:
            if len(jobs) >= min(n_plat, n_jobs):
                break
            if ix.units[i].kind == JOB and ix.units[i].platform in named:
                jobs.append(i)
    taken = set(jobs)
    for i in order:
        if len(jobs) >= n_jobs:
            break
        if ix.units[i].kind == JOB and i not in taken:
            jobs.append(i)
            taken.add(i)

    reps: list[int] = []
    for j in jobs:
        if len(ix.members[j]) < 2:   # a one-vendor job and its member are the same thing
            continue
        for src in srcs:
            m = src.get(j)
            if m is not None and m not in reps and (not platform or ix.units[m].platform == platform):
                reps.append(m)
    reps = sorted(reps, key=lambda m: rank.get(m, 10 ** 9))[:n_delta]
    raw = [i for i in order if ix.units[i].kind == ENDPOINT and not ix.units[i].cap][:n_raw]
    return ([Candidate(ix.units[i]) for i in jobs] + [Candidate(ix.units[i], "rep") for i in reps]
            + [Candidate(ix.units[i]) for i in raw])


# ---- names ---------------------------------------------------------------------------------------
def name_of(query: str, ix: Index, platform: str | None = None,
            provider_display: Callable[[str], str] = lambda s: s) -> NameHit | None:
    """What a query names, if it is a name: a platform (exactly its name or slug; or, from four
    letters, every query word is or starts a word of exactly one platform's label or slug), else a provider (exactly its name or its display name at any length, or, from
    four letters, a prefix of exactly one provider's), else a product or model name (exactly). On a shelf only a
    provider there counts."""
    q = " ".join(words(re.sub(r"[^\w\s-]", "", query)))
    if not q:
        return None
    forms = {q, q.replace(" ", ""), q.replace(" ", "-")}
    if platform is None:
        exact_hits: list[tuple] = []
        loose: list[tuple] = []
        qwords = q.split()
        long_enough = len(q.replace(" ", "")) >= NAME_PREFIX_MIN
        for slug, short, hay, featured, jobs in ix.platform_names:
            # exactly named first, then one whose name starts with the query, then the shelves' own
            # featured order, then the most jobs (as the platform name page)
            rank = (not (slug.startswith(q) or short.startswith(q)), featured is None, featured or 0, -jobs, slug)
            if slug in forms or short in forms or slug.replace("-", " ") in forms:
                exact_hits.append(rank)
            elif long_enough and (all(any(h == w or h.startswith(w) for h in hay) for w in qwords)
                                  or any(slug.startswith(f) for f in forms)):
                loose.append((rank, not rank[0] and q not in hay))
        # A name, or a typed prefix of exactly one platform's: the only match, or the only platform
        # whose own name the query starts without being a whole word of it ("instagra" is Instagram,
        # though Meta Ads mentions Instagram). A word several platforms share ("video", "search",
        # "ads") names none of them: the judged answer reads it.
        if exact_hits:
            return NameHit("platform", tuple(h[-1] for h in sorted(exact_hits) + sorted(r for r, _ in loose)),
                           exact=True)
        typed = [r for r, is_typed in loose if is_typed]
        if len(loose) == 1 or len(typed) == 1:
            return NameHit("platform", ((loose[0][0] if len(loose) == 1 else typed[0])[-1],), exact=False)
    providers = ix.providers if platform is None else sorted(ix.providers_on.get(platform, ()))
    prefixed: list[str] = []
    for prov in providers:
        d = " ".join(words(provider_display(prov)))
        names = {prov, d, d.replace(" ", ""), d.replace(" ", "-")}
        if forms & names:
            return NameHit("provider", (prov,), exact=True)
        if any(len(f) >= NAME_PREFIX_MIN and any(x.startswith(f) for x in names) for f in forms):
            prefixed.append(prov)
    if len(prefixed) == 1:   # a typed prefix names a provider only when it names one
        return NameHit("provider", (prefixed[0],), exact=False)
    if platform is None:
        key = q.replace(" ", "").replace("-", "")
        if key in ix.products:
            return NameHit("product", ix.products[key], exact=True, label=ix.product_labels[key])
    return None
