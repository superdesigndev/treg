"""Contracts and adapters: parsing, identity matching, fixture verification."""

from __future__ import annotations

import re

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import AnyHttpUrl, TypeAdapter, ValidationError

from ... import plugins
from .. import outcome
from ..schema.v2 import is_v2
from . import paths as P

_HTTP_URL = TypeAdapter(AnyHttpUrl)


@dataclass(frozen=True)
class Check:
    """The opt-in check after a hit (`X-Treg-Route-Verify`): one more call, to `endpoint`, with the
    hit's `field` as its identity key of the same name. `prefer` orders that call's providers only.
    `when` is an expression over `{field: value}`; a hit failing it is not checked, for `skip_reason`,
    and `skip_advice` replaces the find's advice then: the caller already asked for the check.
    The check wins over the finder's own `verified`: a completed verdict other than `valid` on a
    `verified: true` hit attaches `disagree_advice`, and the finder's answer is left as it said."""
    endpoint: str
    field: str
    prefer: tuple[str, ...] = ()
    when: str = ""
    skip_reason: str = ""
    skip_advice: str = ""
    disagree_advice: str = ""


@dataclass(frozen=True)
class Contract:
    capability: str
    summary: str
    identity: tuple[tuple[str, ...], ...]      # variants, each a sorted tuple of key names
    identity_types: dict[str, str]
    derive: dict[str, str]                     # field → expression over the identity
    filters: dict[str, Any]
    output: dict[str, dict]                    # core field → {type, required?, note?}
    miss: str
    idempotent: bool = True
    # A miss rule that also sees the question: evaluated on the caller's identity merged with the
    # child's core output, after the adapter's own miss rule. A hit it marks true is a miss
    # (`question_miss`), so the route moves on. Empty = none. Used where only the question can say
    # an answer is off target (a `site:` search whose rows are all on other sites).
    question_miss: str = ""
    default_max_cost_usd: float | None = None  # the per-call ceiling when the caller sends none
    # One sentence the router attaches as `_treg.advice` to a HIT whose `output.verified` is not
    # true — a found contact the provider did not confirm deliverable, which an agent should verify
    # before outreach. Empty = no advice. A search contract has no `verified` output, so advice
    # set there attaches to EVERY hit — deliberate for `people.search`, whose rows carry emails
    # nobody vouched for. A suggestion only unless the caller asks for the check (`check`, below).
    advice_unverified: str = ""
    # One sentence the router attaches as `_treg.advice` when a hit's email domain does not match
    # the domain the caller asked for (`_treg.domain_match: false`). Disclose only — the answer is
    # not altered. Empty = flag only, no advice. A LinkedIn-only child often returns a former
    # employer when the caller also sent a domain.
    advice_domain_mismatch: str = ""
    # False = the contract exists so the archive can judge found/empty (`results.has_result_rules`
    # needs a verified adapter, and an adapter verifies only against a contract); no
    # `treg.<capability>` row is ever generated from it, however many children verify. Used where
    # the "children" are one provider's price tiers, which are not a choice treg should make.
    routed: bool = True
    # Identity keys that SCOPE the answer rather than describe it: an adapter with no place for one
    # the caller sent answers about someone else entirely (a title-only search asked for the CEO of
    # one company returns CEOs of any company), so the router drops it instead of ranking it down.
    scoping: tuple[str, ...] = ()
    # The provider order when the caller sends no `X-Treg-Route-Prefer`. Cost per hit alone ignores
    # time and reliability: for `ai-search.perplexity.answer` it ranked the cheaper, slower and less
    # reliable cloro above dataforseo. A caller's own prefer header still replaces it.
    prefer: tuple[str, ...] = ()
    # Domain suffix -> providers asked first when the caller's `domain` ends in it and the caller
    # sends no `X-Treg-Route-Prefer` (`fr: [enrow]`). The longest matching suffix wins and goes
    # ahead of `prefer`. Cost per hit is one number for every domain, but a finder's coverage
    # differs by country.
    prefer_by_domain_suffix: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # (sent keys, provider order) pairs: the first whose keys the caller all sent replaces `prefer`.
    # One order does not fit every question - a free-text `{q}` is answered best by a semantic
    # search that a `{company_domain, title}` lookup can never reach.
    prefer_when: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = ()
    # Identity key sets on which the caller's own `limit` is a target, not a cap: when the caller
    # sent exactly one of these sets and a `limit`, the route defaults to `X-Treg-Route-Min-Results:
    # <limit>` plus `X-Treg-Route-Merge: 1`, so a short answer goes on to the next provider and the
    # rows add up. A free-text `{q}` people search is a "find as many as fit" question.
    fill_limit_when: tuple[tuple[str, ...], ...] = ()
    # The per-call verdict word (`CallRecord.verdict`): `verdict_from` names the output field it is
    # read from, `verdict_words` the only words a call may be stored with, and `verdict_map` turns a
    # value of that field into one (`true` -> `verified`). Each adapter adds its provider's own
    # words. Empty `verdict_words` = this contract records no verdict.
    verdict_from: str = ""
    verdict_words: tuple[str, ...] = ()
    verdict_map: dict[str, str] = field(default_factory=dict)
    # The verdict words that DECIDE (`valid`, `invalid`): under `X-Treg-Route-Decisive` any other
    # word (unknown, catch-all, risky) sends the call on to the next provider. Empty = refused.
    verdict_decisive: tuple[str, ...] = ()
    # The check a caller may ask for with `X-Treg-Route-Verify`; None = the header is refused.
    check: Check | None = None
    # The output field two providers must agree on under `X-Treg-Route-Confirm`: a hit counts only
    # once another provider returns the same value. Empty = the header is refused.
    confirm: str = ""
    # True where a 2xx with nothing in it is the provider failing the job, not "no result": an
    # empty scraped page, an empty results list. Measured success then counts such an answer
    # against the endpoint (`stats.Tally.fold`), so a provider that answers 200 to everything
    # cannot rank on a success rate it did not earn.
    empty_is_failure: bool = False
    # True where every identity variant names the same one subject (a person's email or phone):
    # a child that reads more of the caller's keys answers the same question, so expected cost per
    # hit ranks ahead of how many keys it uses. Left false on searches, where `{title,
    # company_domain}` and `{company_domain}` are different questions.
    one_subject: bool = False
    # The output fields a COMPLETE answer carries under `X-Treg-Route-Complete`: an answer missing
    # any is weak, and an adapter that cannot map them all is asked last. Empty = more than half of
    # the output fields, the generic rule. A profile without a headline is not a profile.
    complete: tuple[str, ...] = ()
    # One row shape for a list output whose rows otherwise differ per provider (`people.search`):
    # `row_of` names the list field, `row_fields` the keys a row may carry, and every row an
    # adapter maps must fill at least one of `row_needs`. An adapter that declares `row:` returns
    # rows in this shape; one that does not keeps its provider's rows. `raw` keeps the provider's
    # body either way. Empty `row_of` = no row shape.
    row_of: str = ""
    row_fields: dict[str, dict] = field(default_factory=dict)
    row_needs: tuple[str, ...] = ()
    # Identity key -> expression over the identity and filters, a value built for a provider that
    # accepts NO variant the caller supplied (`q` from title + keywords + location, so a free-text
    # people search can answer a `{title}` request). A provider that can take the caller's own
    # fields never sees it, so its request is unchanged.
    stand_in: dict[str, str] = field(default_factory=dict)

    def prefer_for(self, given) -> tuple[str, ...]:
        """The default provider order for a caller who sent the identity keys `given`."""
        return next((order for sent, order in self.prefer_when if set(sent) <= set(given or ())), self.prefer)

    def fill_target(self, given) -> int | None:
        """The row count a routed answer should reach for the request body `given`: its `limit`,
        when the identity keys it sent are exactly one of `fill_limit_when`, else None."""
        if not self.fill_limit_when or not isinstance(given, dict):
            return None
        limit = given.get("limit")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 2:
            return None
        sent = {k for k, v in given.items() if k in self.identity_types and v not in (None, "")}
        return limit if any(sent == set(keys) for keys in self.fill_limit_when) else None

    @property
    def required_output(self) -> tuple[str, ...]:
        return tuple(k for k, v in self.output.items() if (v or {}).get("required"))


@dataclass(frozen=True)
class Adapter:
    endpoint_id: str
    accepts: tuple[tuple[str, ...], ...]       # identity variants (sorted key tuples)
    in_map: dict[str, str]                     # contract field → `queryParams.x` | `body.x` | `pathParams.x`
    out_map: dict[str, str]                    # core field → expression over the provider body
    miss: str
    const: dict[str, Any] = field(default_factory=dict)   # fixed provider params (`body.type: work`)
    in_expr: dict[str, str] = field(default_factory=dict) # provider param ← expression over the request (filters)
    body_array: bool = False                   # the provider wants `[body]` (DataForSEO's task list)
    test_identity: dict[str, Any] = field(default_factory=dict)  # what the endpoint's test_request stands for, when `in` cannot read it back
    cost_units: str = ""                      # optional upper-bound chargeable units for a routed request
    filter_max: dict[str, int] = field(default_factory=dict)  # `max:` a filter this provider caps (`limit: 8`); a request above it skips the provider
    additional_capabilities: tuple[str, ...] = ()  # opt-in reuse of the same request/output mapping
    route: bool = True                        # False: hit/miss evidence only, never a router candidate
    verified_capabilities: tuple[str, ...] = ()
    verified: bool = False
    verify_note: str = ""
    # The provider's own verdict words -> the contract's (`ok: valid`), and optionally the
    # expression that reads the provider's word when the contract's `verdict.from` field cannot
    # (a separate catch-all flag). `_verdict` is the contract's rule, attached at load.
    verdict: str = ""
    verdicts: dict[str, str] = field(default_factory=dict)
    _verdict: tuple[str, tuple[str, ...], tuple[str, ...], dict[str, str]] | None = None
    _filter_keys: tuple[str, ...] = ()        # contract filter names, set at load (always sent)
    # `row:` the contract's row field -> expression over ONE provider row; `_row_of` is the
    # contract's list field those rows fill, set at load.
    row_map: dict[str, str] = field(default_factory=dict)
    _row_of: str = ""
    _row_needs: tuple[str, ...] = ()
    _row_urls: tuple[str, ...] = ()           # row fields of type `url`: never cut, set at load
    _stand_in: dict[str, str] = field(default_factory=dict)   # the contract's `stand_in`, set at load

    def with_stand_in(self, identity: dict[str, Any], keys=None) -> dict[str, Any]:
        """The identity plus the contract's stand-in values for keys it lacks (`keys`: only these)."""
        extra = {}
        for k, expr in self._stand_in.items():
            if identity.get(k) in (None, "") and (keys is None or k in keys):
                v = P.evaluate(expr, identity)
                if v not in (None, ""):
                    extra[k] = v
        return {**identity, **extra} if extra else identity

    def to_upstream(self, identity: dict[str, Any], variant: tuple[str, ...] | None = None) -> tuple[dict[str, str], Any]:
        """(query params, JSON body) for this provider from a canonical request (identity + filters).

        Only the MATCHED variant's identity keys are sent (filters always are): a caller may hand
        treg everything it knows — email, LinkedIn URL, name, domain — and a provider that insists on
        exactly one identifier (tomba: "supply exactly one of email / domain / linkedin") must not
        receive them all. `variant=None` sends every mapped key (the verifier's fixture path)."""
        query: dict[str, str] = {}
        body: dict[str, Any] = {}
        doc = {"queryParams": query, "body": body}
        identity_keys = set(variant) if variant is not None else None
        if identity_keys and self._stand_in:
            identity = self.with_stand_in(identity, identity_keys)
        for field_name, target in self.in_map.items():
            v = identity.get(field_name)
            if v is None:
                continue
            if identity_keys is not None and field_name not in identity_keys and field_name not in (self._filter_keys or ()):
                continue
            if target.startswith("pathParams."):
                # `/v1/email-verifier/{email}`: the call path fills placeholders from the query
                # (`_marketplace_upstream` consumes them), so a path param travels as a query value.
                query[target.split(".", 1)[1]] = str(v)
                continue
            P.set_path(doc, target, v if target.startswith("body.") else str(v))
        # Expressions run AFTER the plain map so they can reshape a mapped value (`list(domain)`).
        for target, expr in (self.in_expr or {}).items():
            v = P.evaluate(expr, identity)
            if v is None:
                continue
            if target.startswith("pathParams."):
                query[target.split(".", 1)[1]] = str(v)
            else:
                P.set_path(doc, target, v if target.startswith("body.") else str(v))
        for target, v in (self.const or {}).items():
            P.set_path(doc, target, v)
        return query, ([body] if self.body_array else body)

    def from_upstream(self, provider_body: Any) -> dict[str, Any]:
        core = {k: P.evaluate(expr, provider_body) for k, expr in self.out_map.items()}
        if self.row_map and self._row_of and isinstance(core.get(self._row_of), list):
            core[self._row_of] = self.rows(core[self._row_of])
        return core

    def rows(self, items: list) -> list[Any]:
        """Each provider row in the contract's row shape: empty values dropped, text cut to
        ROW_TEXT_MAX. A `url` field is kept whole: a cut link points at another resource. A row
        the map cannot read (none of the contract's `row_needs`: the provider
        changed its shape) stays the provider's own row rather than vanish from a paid answer, and
        so does an item that is not an object."""
        out = []
        for item in items:
            if not isinstance(item, dict):
                out.append(item)
                continue
            row = {}
            for key, expr in self.row_map.items():
                v = P.evaluate(expr, item)
                if isinstance(v, str):
                    v = (v.strip() if key in self._row_urls else " ".join(v.split())[:ROW_TEXT_MAX]) or None
                if v not in (None, [], {}):
                    row[key] = v
            out.append(row if any(row.get(k) for k in self._row_needs) else item)
        return out

    def is_miss(self, provider_body: Any, endpoint: dict | None = None) -> bool:
        """The adapter's `miss` expression, or, for a v2 row (whose adapter only maps fields), the
        row's own 2xx miss rule."""
        if self.miss:
            return bool(P.evaluate(self.miss, provider_body))
        if endpoint is None:
            from ..store import load
            endpoint = load().by_id.get(self.endpoint_id) or {}
        raw = json.dumps(provider_body).encode() if provider_body is not None else b""
        return outcome.miss_holds(endpoint, provider_body,
                                  answer=plugins.Answer(status=200, headers={}, raw=raw, body=provider_body))

    @property
    def records_verdict(self) -> bool:
        return self._verdict is not None

    def verdict_raw(self, provider_body: Any) -> str | None:
        """The provider's own verdict word for this answer, normalized (`Catch-All` -> `catch_all`),
        or None when the contract records no verdict or the answer does not fill the contract's
        required output (a find that found nothing has no claim to report)."""
        if self._verdict is None:
            return None
        source, _, required, _ = self._verdict
        core = self.from_upstream(provider_body)
        if any(core.get(k) in (None, "") for k in required):
            return None
        return _verdict_key(P.evaluate(self.verdict, provider_body) if self.verdict else core.get(source))

    def verdict_word(self, raw: str | None) -> str | None:
        """The contract's word for a normalized provider word; None when it maps to none."""
        if raw is None or self._verdict is None:
            return None
        _, words, _, contract_map = self._verdict
        word = self.verdicts.get(raw) or contract_map.get(raw) or raw
        return word if word in words else None


def _verdict_key(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if not isinstance(value, str) or not value.strip():
        return None
    return re.sub(r"[\s-]+", "_", value.strip().lower())


def _variants(raw) -> tuple[tuple[str, ...], ...]:
    out = []
    for v in raw or []:
        keys = tuple(sorted(v.keys() if isinstance(v, dict) else v))
        out.append(keys)
    return tuple(out)


CONTRACT_KEYS = frozenset({
    "summary", "identity", "derive", "filters", "output", "miss", "idempotent", "default_max_cost_usd",
    "advice_unverified", "advice_domain_mismatch", "routed", "scoping", "prefer", "prefer_by_domain_suffix", "check", "confirm",
    "empty_is_failure", "one_subject", "complete", "verdict", "question_miss", "row", "stand_in",
    "prefer_when", "fill_limit_when"})
ADAPTER_KEYS = frozenset({
    "accepts", "in", "in_expr", "body_array", "test_identity", "cost_units", "max",
    "additional_capabilities", "route", "const", "out", "miss", "verdict", "verdicts", "row"})
# The longest text a mapped row field keeps: a row is read by an agent, and one provider's
# multi-page bio would otherwise cost more tokens than the rest of the answer.
ROW_TEXT_MAX = 300


def _known_keys(what: str, raw: object, known: frozenset[str]) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{what}: a mapping")
    if unknown := sorted(set(raw) - known):
        raise ValueError(f"{what}: unknown key {', '.join(unknown)}")
    return raw


def _expressions(what: str, *exprs: object) -> None:
    for expr in exprs:
        try:
            P.parse(expr)
        except ValueError as exc:
            raise ValueError(f"{what}: {exc}") from None


def parse_contracts(doc: dict) -> dict[str, Contract]:
    out = {}
    for cap, c in (doc.get("contracts") or {}).items():
        _known_keys(f"contract {cap}", c, CONTRACT_KEYS)
        _expressions(f"contract {cap}", *(c.get("derive") or {}).values(), *(c.get("stand_in") or {}).values(),
                     *([c["miss"]] if c.get("miss") else []),
                     *([c["question_miss"]] if c.get("question_miss") else []),
                     *([c["check"]["when"]] if isinstance(c.get("check"), dict) and c["check"].get("when") else []))
        types = {}
        for v in c.get("identity") or []:
            if isinstance(v, dict):
                types.update({k: str(t) for k, t in v.items()})
        if stray := sorted(set(c.get("stand_in") or {}) - set(types)):
            raise ValueError(f"contract {cap}: stand_in must name identity keys, got {stray}")
        scoping = c.get("scoping") or []
        if not isinstance(scoping, list) or any(k not in types for k in scoping):
            # A typo'd key would silently scope nothing; failing the routing load instead makes
            # every `treg.*` capability disappear, which test_routing notices at once.
            raise ValueError(f"contract {cap}: scoping must list identity keys of the contract, got {scoping!r}")
        for section in ("output", "filters"):
            for k, spec in (c.get(section) or {}).items():
                mode = spec.get("analytics", "skip") if isinstance(spec, dict) else "skip"
                if mode not in ("key", "value", "skip") or (section == "filters" and mode == "key"):
                    # Where a field lands in a call event (application/call_events.py): a typo
                    # must fail loudly, never quietly emit an identifier as a plain value.
                    raise ValueError(f"contract {cap}: {section}.{k} analytics must be key, value or skip "
                                     f"(filters: value or skip), got {mode!r}")
        out[cap] = Contract(
            capability=cap, summary=str(c.get("summary") or ""), identity=_variants(c.get("identity")),
            identity_types=types, derive=dict(c.get("derive") or {}),
            filters={k: (v if isinstance(v, dict) else {"type": str(v)}) for k, v in (c.get("filters") or {}).items()},
            output={k: (v if isinstance(v, dict) else {"type": str(v)}) for k, v in (c.get("output") or {}).items()},
            miss=str(c.get("miss") or ""), idempotent=bool(c.get("idempotent", True)),
            question_miss=str(c.get("question_miss") or ""),
            default_max_cost_usd=(float(c["default_max_cost_usd"]) if c.get("default_max_cost_usd") is not None else None),
            advice_unverified=str(c.get("advice_unverified") or ""),
            advice_domain_mismatch=str(c.get("advice_domain_mismatch") or ""),
            routed=bool(c.get("routed", True)),
            scoping=tuple(str(k) for k in scoping),
            prefer=tuple(str(p).lower() for p in c.get("prefer") or ()),
            prefer_by_domain_suffix=_parse_prefer_by_domain_suffix(cap, c.get("prefer_by_domain_suffix"), types),
            prefer_when=_parse_prefer_when(cap, c.get("prefer_when"), types),
            fill_limit_when=_parse_fill_limit_when(cap, c.get("fill_limit_when"), types, c),
            check=_parse_check(cap, c.get("check"), c.get("output") or {}),
            confirm=_parse_confirm(cap, c.get("confirm"), c.get("output") or {}),
            empty_is_failure=bool(c.get("empty_is_failure", False)),
            one_subject=bool(c.get("one_subject", False)),
            complete=_parse_complete(cap, c.get("complete"), c.get("output") or {}),
            stand_in={str(k): str(v) for k, v in (c.get("stand_in") or {}).items()},
            **_parse_row(cap, c.get("row"), c.get("output") or {}),
            **_parse_verdict(cap, c.get("verdict")))
    return out


def _parse_prefer_when(cap: str, raw, types: dict) -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"contract {cap}: prefer_when lists one or more {{sent, prefer}} rules")

    def names(v) -> bool:
        return isinstance(v, list) and bool(v) and all(isinstance(x, str) and x.strip() for x in v)
    out = []
    for rule in raw:
        # A bare string would be read letter by letter (`prefer: exa` -> e, x, a) and reorder nothing.
        if (not isinstance(rule, dict) or set(rule) != {"sent", "prefer"} or not names(rule["sent"])
                or not names(rule["prefer"]) or any(k.strip() not in types for k in rule["sent"])):
            raise ValueError(f"contract {cap}: prefer_when rule must be {{sent: [identity keys], "
                             f"prefer: [provider names]}}, got {rule!r}")
        out.append((tuple(k.strip() for k in rule["sent"]), tuple(p.strip().lower() for p in rule["prefer"])))
    return tuple(out)


def _parse_fill_limit_when(cap: str, raw, types: dict, c: dict) -> tuple[tuple[str, ...], ...]:
    if raw is None:
        return ()
    lists = [k for k, v in (c.get("output") or {}).items() if isinstance(v, dict) and v.get("type") == "list" and v.get("required")]
    if (not isinstance(raw, list) or not raw or "limit" not in (c.get("filters") or {}) or not lists
            or any(not isinstance(keys, list) or not keys or any(not isinstance(k, str) or k.strip() not in types
                                                                 for k in keys) for keys in raw)):
        # A set that can never match would quietly leave every request at one provider's answer.
        raise ValueError(f"contract {cap}: fill_limit_when lists identity key sets ([[q]]) and needs a "
                         f"`limit` filter and a list output, got {raw!r}")
    return tuple(tuple(k.strip() for k in keys) for keys in raw)


def _parse_prefer_by_domain_suffix(cap: str, raw, types: dict) -> dict[str, tuple[str, ...]]:
    if raw is None:
        return {}
    if "domain" not in types or not isinstance(raw, dict) or not raw:
        raise ValueError(f"contract {cap}: prefer_by_domain_suffix maps one or more domain suffixes to "
                         "providers and needs a `domain` identity key")
    out = {}
    for suffix, providers in raw.items():
        key = suffix.strip().lower().strip(".") if isinstance(suffix, str) else ""
        if (not key or not isinstance(providers, list) or not providers
                or any(not isinstance(p, str) or not p.strip() for p in providers)):
            # A rule that names no provider would match and reorder nothing: fail the load instead
            # of quietly routing that country by cost per hit.
            raise ValueError(f"contract {cap}: prefer_by_domain_suffix.{suffix} must list one or more provider names")
        out[key] = tuple(p.strip().lower() for p in providers)
    return out


def default_prefer(contract: Contract, identity: dict[str, Any], given=None) -> list[str]:
    """The provider order a caller who sent no `X-Treg-Route-Prefer` gets: the providers named for
    the longest suffix of the identity's `domain`, then the contract's `prefer` for the identity
    keys `given` (`prefer_when`)."""
    first: tuple[str, ...] = ()
    domain = identity.get("domain")
    if contract.prefer_by_domain_suffix and isinstance(domain, str):
        host = domain.strip().lower().rstrip(".")
        matches = [s for s in contract.prefer_by_domain_suffix if host == s or host.endswith("." + s)]
        if matches:
            first = contract.prefer_by_domain_suffix[max(matches, key=len)]
    return [*first, *(p for p in contract.prefer_for(given) if p not in first)]


def _parse_row(cap: str, raw, output: dict) -> dict:
    if not raw:
        return {}
    of, fields = raw.get("of") if isinstance(raw, dict) else None, (raw or {}).get("fields") if isinstance(raw, dict) else None
    if of not in output or (output[of] or {}).get("type") != "list" or not isinstance(fields, dict) or not fields:
        raise ValueError(f"contract {cap}: row needs `of` (a list field of its output) and `fields`")
    needs = tuple(str(k) for k in raw.get("needs") or ())
    if not needs or any(k not in fields for k in needs):
        raise ValueError(f"contract {cap}: row `needs` must name one or more of its `fields`")
    return {"row_of": str(of), "row_fields": {str(k): (v if isinstance(v, dict) else {"type": str(v)}) for k, v in fields.items()},
            "row_needs": needs}


def _parse_complete(cap: str, raw, output: dict) -> tuple[str, ...]:
    fields = tuple(str(k) for k in raw or ())
    if unknown := [k for k in fields if k not in output]:
        raise ValueError(f"contract {cap}: complete names fields outside its output: {', '.join(unknown)}")
    return fields


def _parse_check(cap: str, raw, output: dict) -> Check | None:
    if not raw:
        return None
    if not isinstance(raw, dict) or not raw.get("endpoint") or raw.get("field") not in output:
        raise ValueError(f"contract {cap}: check needs an `endpoint` and a `field` from its output")
    if bool(raw.get("when")) != bool(raw.get("skip_reason")):
        raise ValueError(f"contract {cap}: check `when` and `skip_reason` come together")
    if raw.get("skip_advice") and not raw.get("when"):
        raise ValueError(f"contract {cap}: check `skip_advice` needs a `when`")
    if raw.get("disagree_advice") and "verified" not in output:
        raise ValueError(f"contract {cap}: check `disagree_advice` needs a `verified` output")
    return Check(endpoint=str(raw["endpoint"]), field=str(raw["field"]),
                 prefer=tuple(str(p).lower() for p in raw.get("prefer") or ()),
                 when=str(raw.get("when") or ""), skip_reason=str(raw.get("skip_reason") or ""),
                 skip_advice=str(raw.get("skip_advice") or ""),
                 disagree_advice=str(raw.get("disagree_advice") or ""))


def _parse_confirm(cap: str, raw, output: dict) -> str:
    if not raw:
        return ""
    if raw not in output:
        raise ValueError(f"contract {cap}: confirm must name a field of its output, got {raw!r}")
    return str(raw)


def _parse_verdict(cap: str, raw) -> dict:
    if not raw:
        return {}
    words = tuple(str(w) for w in raw.get("words") or ())
    mapping = {_verdict_key(k) or str(k): str(v) for k, v in (raw.get("map") or {}).items()}
    if not raw.get("from") or not words or any(v not in words for v in mapping.values()):
        raise ValueError(f"contract {cap}: verdict needs `from`, `words`, and a `map` into those words")
    decisive = tuple(str(w) for w in raw.get("decisive") or ())
    if any(w not in words for w in decisive):
        raise ValueError(f"contract {cap}: verdict `decisive` must be among its `words`")
    return {"verdict_from": str(raw["from"]), "verdict_words": words, "verdict_map": mapping,
            "verdict_decisive": decisive}


def parse_adapters(doc: dict) -> dict[str, Adapter]:
    out = {}
    for eid, a in (doc.get("adapters") or {}).items():
        _known_keys(f"adapter {eid}", a, ADAPTER_KEYS)
        _expressions(f"adapter {eid}", *(a.get("in_expr") or {}).values(), *(a.get("out") or {}).values(),
                     *(a.get("row") or {}).values(), *(a[k] for k in ("miss", "verdict") if a.get(k)))
        out[eid] = Adapter(
            endpoint_id=eid, accepts=_variants(a.get("accepts")), in_map=dict(a.get("in") or {}),
            in_expr=dict(a.get("in_expr") or {}), body_array=bool(a.get("body_array")),
            test_identity=dict(a.get("test_identity") or {}),
            cost_units=str(a.get("cost_units") or ""),
            filter_max={str(k): int(v) for k, v in (a.get("max") or {}).items()},
            additional_capabilities=tuple(a.get("additional_capabilities") or ()),
            route=a.get("route", True) is not False,
            const=dict(a.get("const") or {}), out_map=dict(a.get("out") or {}), miss=str(a.get("miss") or ""),
            row_map={str(k): str(v) for k, v in (a.get("row") or {}).items()},
            verdict=str(a.get("verdict") or ""),
            verdicts={_verdict_key(k) or str(k): str(v) for k, v in (a.get("verdicts") or {}).items()})
    return out


def miss_status(endpoint: dict) -> int | None:
    """The ERROR status this endpoint's YAML declares as "no result" (`miss: {status, means}`),
    or None. Only a 4xx counts: a `status: 200` block documents a 2xx the adapter's own `miss`
    predicate decides, and honouring it here would call every success a miss. A v2 row lists its
    statuses; this is the first."""
    m = endpoint.get("miss")
    if is_v2(endpoint):
        return m["status"][0] if isinstance(m, dict) and m.get("status") else None
    if isinstance(m, dict) and m.get("status") is not None:
        try:
            status = int(m["status"])
        except (TypeError, ValueError):
            return None
        return status if 400 <= status < 500 else None
    return None


def declared_miss(endpoint: dict, status: int, body: Any) -> bool:
    """True when a child's ERROR status is the endpoint's declared "no result" answer — the ONE
    reader of the `miss:` block for the router and the arena, so both agree.

    `miss: {status}` matches on status alone. `miss: {status, when}` adds a body predicate in the
    adapter expression language (`when: "error_code == 'NO_MATCH'"`) for providers whose one
    status carries both a miss and a fault (prospeo 400: NO_MATCH vs INVALID_DATAPOINTS). `body`
    is the raw bytes or the parsed document; only a JSON object can satisfy a predicate, and a
    predicate that raises reads as "not a miss"."""
    if is_v2(endpoint):
        doc = _document(body)
        raw = bytes(body) if isinstance(body, (bytes, bytearray)) else body.encode() \
            if isinstance(body, str) else json.dumps(body).encode()
        return outcome.status_miss(endpoint, status, doc,
                                   answer=plugins.Answer(status=status, headers={}, raw=raw, body=doc))
    if status != miss_status(endpoint):
        return False
    return _body_matches((endpoint.get("miss") or {}).get("when"), body)


def declared_not_found(endpoint: dict, status: int, body: Any) -> bool:
    """True when a child's answer is the endpoint's declared "the target does not exist" answer
    (`not_found: {status, when?, means}`): the page or record is gone at its source, so another
    provider can only find the same nothing, or answer an empty page that looks like a success.
    `status` is one status or a list (a 2xx is allowed: some providers answer 200 and name the
    target's 404 in the body); `when` narrows it with a body predicate, as for `miss:`."""
    if is_v2(endpoint):
        return outcome.not_found(endpoint, status, _document(body))
    nf = endpoint.get("not_found")
    if not isinstance(nf, dict) or nf.get("status") is None:
        return False
    wanted = nf["status"] if isinstance(nf["status"], list) else [nf["status"]]
    try:
        if status not in {int(s) for s in wanted}:
            return False
    except (TypeError, ValueError):
        return False
    return _body_matches(nf.get("when"), body)


def _document(body: Any) -> Any:
    if isinstance(body, (bytes, bytearray, str)):
        try:
            return json.loads(body)
        except ValueError:
            return None
    return body


def _body_matches(when: Any, body: Any) -> bool:
    """No predicate matches every body; a predicate matches only a JSON object it evaluates true on."""
    if not when:
        return True
    doc = body
    if isinstance(body, (bytes, bytearray, str)):
        try:
            doc = json.loads(body)
        except ValueError:
            return False
    if not isinstance(doc, dict):
        return False
    try:
        return bool(P.evaluate(str(when), doc))
    except Exception:  # noqa: BLE001 — a broken predicate must never crash a call
        return False


def _as_url(v: Any) -> str | None:
    """An http(s) URL with a dotted host and no userinfo, or None. Parsing is the WHATWG parser's;
    the value is kept as sent, a scheme-less one with `https://` added."""
    if not isinstance(v, str) or not (s := v.strip()):
        return None
    candidate = s if re.match(r"(?i)https?://", s) else "https://" + s
    try:
        url = _HTTP_URL.validate_python(candidate)
    except ValidationError:
        return None
    host = (url.host or "").strip(".")
    if url.username is not None or not ("." in host or host.startswith("[")):
        return None
    return candidate


_TYPE_NORMALISERS = {"url": _as_url}


def _sent(v: Any) -> bool:
    return v is not None and not (isinstance(v, str) and not v.strip())


def invalid_identity(contract: Contract, given: dict[str, Any]) -> list[dict[str, Any]]:
    """The caller's identity fields that are present but not of their declared type."""
    bad = []
    for k, t in contract.identity_types.items():
        v = given.get(k)
        if _sent(v) and (norm := _TYPE_NORMALISERS.get(t)) and norm(v) is None:
            shown = v if isinstance(v, str) else json.dumps(v, default=str)
            bad.append({"field": k, "type": t,
                        "value": shown[:200].encode("utf-8", "backslashreplace").decode("utf-8")})
    return bad


def canonical_identity(contract: Contract, given: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...] | None]:
    """The caller's fields + everything derivable → (identity, the variant they supplied), or
    (identity, None) when no variant is complete. Derived keys count for matching adapters."""
    ident = {k: v for k, v in given.items() if k in contract.identity_types and _sent(v)}
    supplied = next((v for v in contract.identity if all(k in ident for k in v)), None)
    if supplied is None:
        return ident, None
    for k, t in contract.identity_types.items():
        if k in ident and (norm := _TYPE_NORMALISERS.get(t)):
            ident[k] = norm(ident[k]) or ident[k]
    for _ in range(2):  # derive until stable (join needs first+last; split needs full_name)
        for k, expr in contract.derive.items():
            if ident.get(k) in (None, ""):
                v = P.evaluate(expr, ident)
                if v not in (None, ""):
                    ident[k] = v
    for k, spec in (contract.filters or {}).items():  # shared filters ride with the identity
        v = given.get(k)
        ident[k] = v if v not in (None, "") else (spec or {}).get("default")
    return ident, supplied


def adapter_accepts(adapter: Adapter, identity: dict[str, Any]) -> tuple[str, ...] | None:
    """The first accepted variant fully present in the (derived) identity, or None. Only when the
    caller's own fields fit none does a contract stand-in (`q` built from them) get a turn."""
    def first(ident):
        return next((v for v in adapter.accepts if all(ident.get(k) not in (None, "") for k in v)), None)
    found = first(identity)
    if found is None and getattr(adapter, "_stand_in", None):
        found = first(adapter.with_stand_in(identity))
    return found


def verify(adapter: Adapter, contract: Contract, endpoint: dict, example: Any) -> tuple[bool, str]:
    """Fixture round-trip: `in` must reproduce the endpoint's own `test_request` from the contract's
    view of it, and `out` must fill every required core field from the example response (or the
    example must be a recognised miss). Anything else = not a candidate."""
    if not adapter.accepts or not adapter.out_map or not (adapter.miss or outcome.declares_miss(endpoint)):
        return False, "adapter incomplete"
    tr = endpoint.get("test_request") or {}
    # Reconstruct the identity from the test request through the adapter's own `in` map.
    ident: dict[str, Any] = {}
    tr_body = tr.get("body") or {}
    if isinstance(tr_body, list):
        tr_body = tr_body[0] if tr_body and isinstance(tr_body[0], dict) else {}
    doc = {"queryParams": tr.get("queryParams") or {}, "body": tr_body,
           "pathParams": tr.get("pathParams") or {}}
    for field_name, target in adapter.in_map.items():
        v = P.get_path(doc, target)
        if isinstance(v, list) and len(v) == 1 and contract.identity_types.get(field_name, "str") != "list":
            v = v[0]  # `domains: ["stripe.com"]` in the fixture is the scalar `company_domain`
        if v not in (None, ""):
            ident[field_name] = v
    if adapter.test_identity:  # the fixture's identity is stated, because `in` builds a value (a URL) rather than copying one
        ident = dict(adapter.test_identity)
    ident, variant = canonical_identity(contract, ident)
    if variant is None or adapter_accepts(adapter, ident) is None:
        return False, "test_request does not express an accepted identity variant"
    q, b = adapter.to_upstream(ident)
    if isinstance(b, list):
        b = b[0] if b else {}
    for k, v in (tr.get("pathParams") or {}).items():
        if k in {t.split(".", 1)[1] for t in adapter.in_map.values() if t.startswith("pathParams.")} and str(q.get(k)) != str(v):
            return False, f"in: pathParams.{k} → {q.get(k)!r}, test_request has {v!r}"
    for k, v in (tr.get("queryParams") or {}).items():
        if k in {t.split(".", 1)[1] for t in adapter.in_map.values() if t.startswith("queryParams.")} and str(q.get(k)) != str(v):
            return False, f"in: queryParams.{k} → {q.get(k)!r}, test_request has {v!r}"
    for k, v in tr_body.items():
        # `in_expr` targets count only when they are built from the identity (a URL from a handle);
        # filter expressions carry defaults the fixture need not share.
        ident_names = set(contract.identity_types)
        expr_targets = [t for t, e in (adapter.in_expr or {}).items()
                        if any(re.search(rf"\b{re.escape(n)}\b", e) for n in ident_names)]
        mapped = {t.split(".", 1)[1].split(".")[0] for t in list(adapter.in_map.values()) + expr_targets if t.startswith("body.")}
        if k in mapped and b.get(k) != v:
            return False, f"in: body.{k} → {b.get(k)!r}, test_request has {v!r}"
    if example is None:
        return False, "no example_response to verify `out` against"
    if adapter.is_miss(example, endpoint):
        return True, "example is a miss; out unverified on a hit"
    core = adapter.from_upstream(example)
    missing = [k for k in contract.required_output if core.get(k) in (None, "")]
    if missing:
        return False, f"out: example lacks required {missing}"
    if adapter.row_map:
        if not contract.row_of:
            return False, "row: the contract has no row shape"
        if unknown := sorted(set(adapter.row_map) - set(contract.row_fields)):
            return False, f"row: fields outside the contract's row: {unknown}"
        # a saved example's truncation marker ("… 8 more item(s) truncated") is not a row
        rows = [r for r in core.get(contract.row_of) or [] if isinstance(r, dict)]
        if not rows or any(not any(r.get(k) for k in contract.row_needs) for r in rows):
            return False, f"row: every example row must fill one of {list(contract.row_needs)}"
    return True, ""


def load_routing(directory: Path, endpoints_by_id: dict[str, dict], read_yaml, read_example) -> tuple[dict[str, Contract], dict[str, Adapter]]:
    """Parse both files and verify every adapter against its endpoint's fixtures."""
    contracts = parse_contracts(read_yaml(directory / "contracts.yaml") or {})
    adapters = parse_adapters(read_yaml(directory / "adapters.yaml") or {})
    verified: dict[str, Adapter] = {}
    for eid, ad in adapters.items():
        ep = endpoints_by_id.get(eid)
        contract = contracts.get((ep or {}).get("capability") or "")
        if contract is not None:
            ad = Adapter(**{**ad.__dict__, "_filter_keys": tuple(contract.filters or ()),
                            "_row_of": contract.row_of if ad.row_map else "", "_row_needs": contract.row_needs,
                            "_row_urls": tuple(k for k, spec in contract.row_fields.items() if spec.get("type") == "url"),
                            "_stand_in": contract.stand_in,
                            "_verdict": (contract.verdict_from, contract.verdict_words,
                                         contract.required_output, contract.verdict_map)
                            if contract.verdict_words else None})
        if ep is None or contract is None:
            verified[eid] = Adapter(**{**ad.__dict__, "verified": False, "verify_note": "unknown endpoint or no contract"})
            continue
        ok, note = verify(ad, contract, ep, read_example(ep))
        additional = []
        for capability in ad.additional_capabilities:
            extra = contracts.get(capability)
            # One adapter has one filter mapping. Different filter contracts need separate adapters.
            if ok and extra is not None and extra.filters == contract.filters:
                extra_ok, _ = verify(ad, extra, ep, read_example(ep))
                if extra_ok:
                    additional.append(capability)
        verified[eid] = Adapter(**{**ad.__dict__, "verified": ok, "verify_note": note,
                                   "verified_capabilities": tuple(additional)})
    return contracts, verified
