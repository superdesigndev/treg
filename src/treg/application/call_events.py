"""What one call event says: generic columns, plus a pseudonymous view of its input and answer.

The sink (`treg.call_events`) only queues and sends; this module decides the columns. One table
serves every capability, so the capability-specific part is contract-driven maps, not columns:

- `input_*` come from the capability contract's identity AFTER derivation (`canonical_identity`),
  never from the vendor's request shape. A direct call is read back into that identity through
  its adapter's `in` map, so a finder called directly, a routed child and a routed parent asked
  the same question get the same `input_id`. Every identity field is a key (an HMAC); the
  non-sensitive segments (`domain_class`, `tld`, filters declared `analytics: value`) go to
  `input_meta`.
- `answer_*` come from the adapter's output (`from_upstream` + `safe_output`, the same reading the
  hit verdict and the Arena use). Which output field goes where is declared on the contract's
  output (`analytics: key | value | skip`, default skip), so a new capability needs no code and no
  table change. A team that opted out of the archive gets no answer columns.

Every identifier is HMAC-SHA256 under `call_events_hmac_secret`, after one normalization
(emails lower-cased, domains without scheme or `www.`, LinkedIn URLs reduced to the handle, phone
numbers to their E.164 digits), so a find's `answer_keys['email']` equals a later verify's
`input_keys['email']`. A phone written without its country code gets no key: its digits alone
could be another country's number. A keyed phone also records its country calling code
(`answer_values['phone_calling_code']`, `input_meta['calling_code']`), which names no person. No raw email,
name, domain, phone or URL is ever emitted. `answer_values` does carry raw segment text as the
provider wrote it (at most 256 characters): a company's industry, size and HQ location, and a
person's `location`, which can name a city. That is personal data next to the person's keys, kept
on purpose so analysis can derive country and region later; Iceberg retention bounds it.

The expensive part (reading bodies, hashing) is a builder the sink runs in its flusher; what is
computed on the call path is a handful of attribute reads.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from .. import call_events as sink
from ..config import get_settings
from ..domain.arena import safe_output
from ..domain.catalog import store as catalog_store
from ..domain.catalog.routing import paths as P
from ..domain.catalog.routing.contracts import Adapter, Contract, canonical_identity
from ..domain.catalog.routing.segments import SEGMENTERS

SCHEMA_VERSION = 1
ANALYTICS_MODES = ("key", "value", "skip")

# Every column, always present (None when unknown), so one Iceberg schema fits every event.
COLUMNS = (
    "schema_version", "event_id", "event_time", "event_kind", "org_id", "client", "capability",
    "endpoint_id", "provider", "credential_tier", "call_ref", "parent_ref", "step",
    "waterfall_position", "is_eval", "status_code", "refused_by", "hit", "verdict", "cached",
    "cost_estimated_micro", "cost_charged_micro", "upstream_ms", "duration_ms", "served_by",
    "tried_count", "check_verdict", "async_outcome", "archive_key_hash", "archive_content_hash",
    "adapter_fingerprint", "input_type", "input_id", "input_keys", "input_meta", "answer_keys",
    "answer_values", "answer_flags",
)

_STEP = re.compile(r"^(?P<parent>.+):(?P<step>r(?P<n>\d+)|v|u|p\d+|s\d+(?:\.\d+)?)$")
_DOMAIN_KEYS = ("domain", "company_domain", "website")
_ROLE_LOCALS = frozenset({
    "admin", "billing", "careers", "contact", "enquiries", "hello", "help", "hr", "info",
    "inquiries", "jobs", "mail", "marketing", "no-reply", "noreply", "office", "press", "sales",
    "support", "team", "webmaster",
})
_VALUE_MAX = 256
# `limit` or `country_code(country)`: an `in_expr` a filter's value can be read back through.
_field_paths: dict[int, tuple[Adapter, Contract, tuple]] = {}
_NAME_HALF = re.compile(r"split_(first|last)\(\s*([a-z_][a-z0-9_]*)\s*\)")
_SCALAR_TYPES = {"bool": bool, "int": (int,), "float": (int, float)}
_fingerprints: dict[int, tuple[Adapter, str]] = {}


def keyed(value: str) -> str:
    """HMAC-SHA256 of an already normalized value, 32 hex characters."""
    secret = get_settings().call_events_hmac_secret.encode()
    return hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()[:32]


def _host(value: str) -> str:
    v = value.strip().lower()
    v = re.sub(r"^[a-z][a-z0-9+.-]*://", "", v)
    v = re.split(r"[/?#]", v, maxsplit=1)[0].rsplit("@", 1)[-1].split(":", 1)[0]
    return v[4:] if v.startswith("www.") else v


def normalize(field: str, value: Any) -> str | None:
    """The one spelling a field's value is hashed in, so the same subject hashes the same way
    wherever it appears (a find's answer and a verify's input)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    if field == "email" or field.endswith("_email") or (field != "linkedin_url" and "@" in v
                                                         and " " not in v):
        return v.lower()
    if field in _DOMAIN_KEYS:
        return _host(v) or None
    if field == "phone" or field.endswith("_phone"):
        return P.e164_digits(v)
    if field in ("linkedin_url", "linkedin_handle"):
        handle = P.linkedin_handle(v) if field == "linkedin_url" else v.strip("/")
        return handle.lower().strip("/") if handle else _host(v) or None
    return re.sub(r"\s+", " ", v).casefold()


def domain_class(domain: str) -> str:
    labels = domain.lower().split(".")
    if P.free_mail(domain):
        return "webmail"
    tail = labels[-2:]
    if "edu" in tail or (len(labels) > 2 and labels[-2] == "ac"):
        return "edu"
    if "gov" in tail or "mil" in tail:
        return "gov"
    return "company"


def _stringify(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str) and value.strip():
        return value.strip()[:_VALUE_MAX]
    return None


def ref_parts(call_ref: str | None) -> tuple[str | None, str | None, int | None]:
    """`X:r2` -> (`X`, `r2`, 2); `X:v` -> (`X`, `v`, None); `X:s1.4` (a `for_each` item) ->
    (`X`, `s1.4`, None); a root ref -> (None, None, None)."""
    m = _STEP.match(call_ref or "")
    if m is None:
        return None, None, None
    return m.group("parent"), m.group("step"), int(m.group("n")) if m.group("n") else None


def adapter_fingerprint(adapter: Adapter) -> str:
    """12 hex characters naming one adapter definition, so a changed mapping is visible in the
    data (a hit rate that moves with the fingerprint moved with the adapter, not the provider)."""
    cached = _fingerprints.get(id(adapter))
    if cached is not None and cached[0] is adapter:
        return cached[1]
    spec = {"accepts": adapter.accepts, "in": adapter.in_map, "in_expr": adapter.in_expr,
            **({"in_read": adapter.in_read} if adapter.in_read else {}),
            "const": adapter.const, "out": adapter.out_map, "miss": adapter.miss,
            "verdict": adapter.verdict, "verdicts": adapter.verdicts}
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()[:12]
    _fingerprints[id(adapter)] = (adapter, digest)
    return digest


def is_eval(org_id: int | None) -> bool:
    raw = get_settings().call_events_eval_orgs
    return org_id is not None and str(org_id) in {p.strip() for p in raw.split(",") if p.strip()}


def _descend(node: Any, steps: tuple) -> Any:
    for step in steps:
        if isinstance(step, int):
            node = node[step] if isinstance(node, list) and step < len(node) else None
        else:
            node = node.get(step) if isinstance(node, dict) else None
    return node


def _locate(node: Any, want: Any, at: tuple = ()) -> list[tuple]:
    if isinstance(node, dict):
        return [p for k, v in node.items() for p in _locate(v, want, (*at, k))]
    if isinstance(node, list):
        return [p for i, v in enumerate(node) for p in _locate(v, want, (*at, i))]
    return [at] if node == want and type(node) is type(want) else []


def field_paths(adapter: Adapter, contract: Contract) -> tuple[tuple[str, str, tuple], ...]:
    """(field, request target, steps inside the value there) for every field an adapter's
    `in_expr` sends where it can be read back: found by evaluating each expression on placeholder
    values. A place counts only when it holds that filter's placeholder alone, no other field's
    placeholder reaches the expression, and none fills the place when the filter is not sent, so
    `list(country)`, `obj('include', list(upper(country)))` and `at_most(limit, 50)` read back,
    and `coalesce(location, country_name(country))` or a `join` of several fields do not."""
    cached = _field_paths.get(id(adapter))
    if cached is not None and cached[0] is adapter and cached[1] is contract:
        return cached[2]
    fields = [*contract.identity_types, *(contract.filters or {})]
    kinds = {k: str(((contract.filters or {}).get(k) or {}).get("type") or contract.identity_types.get(k, "str"))
             for k in fields}
    # Several upper-case words: case and country transforms pass a placeholder through whole,
    # while a lossy one (`split_first(full_name)`) returns part of it, which is never found.
    marks: dict[str, Any] = {}
    for i, k in enumerate(fields):
        text = "ZQX%d QZX%d WXQ%d" % (i, i, i)
        marks[k] = 900 + i if kinds[k] == "int" else [text] if kinds[k] == "list" else text
    out = []
    for field_name in fields:
        if kinds[field_name] == "bool":
            continue
        # A number is tried at sizes on both sides of the usual page-size clamps
        # (`at_most(limit, 10)`, `at_least(limit, 10)`), so one of them passes through untouched.
        tries = (7, 37, 900) if kinds[field_name] == "int" else (marks[field_name],)
        others = {(m[0] if isinstance(m, list) else m) for k, m in marks.items() if k != field_name}
        for target, expr in (adapter.in_expr or {}).items():
            if not isinstance(expr, str):
                continue
            hit = None
            for value in tries:
                env = {**marks, field_name: value}
                mark = value[0] if isinstance(value, list) else value
                try:
                    full = P.evaluate(expr, env)
                    without = P.evaluate(expr, {k: v for k, v in marks.items() if k != field_name})
                except Exception:  # noqa: BLE001 - an expression this probe cannot run is not read back
                    break
                found = _locate(full, mark)
                if any(_locate(full, o) for o in others):
                    break
                if len(found) == 1 and not any(_locate(_descend(without, found[0]), o) for o in others):
                    hit = found[0]
                    break
            if hit is not None:
                out.append((field_name, target, hit))
                break
    result = tuple(out)
    _field_paths[id(adapter)] = (adapter, contract, result)
    return result


def vendor_identity(adapter: Adapter, contract: Contract, query_items: Iterable[tuple[str, str]],
                    body: bytes | None) -> dict:
    """The contract identity a vendor-shaped request expresses, read back through the adapter's
    `in` map (the fixture verifier's reading). Path parameters travel as query values."""
    query = {k: v for k, v in query_items}
    doc_body: Any = {}
    if body:
        try:
            doc_body = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            doc_body = {}
    if isinstance(doc_body, list):
        doc_body = doc_body[0] if doc_body and isinstance(doc_body[0], dict) else {}
    if not isinstance(doc_body, dict):
        doc_body = {}
    doc = {"queryParams": query, "body": doc_body, "pathParams": query}
    given: dict = {}
    for field_name, target in adapter.in_map.items():
        v = P.get_path(doc, target)
        if isinstance(v, list) and len(v) == 1 and contract.identity_types.get(field_name, "str") != "list":
            v = v[0]
        if isinstance(v, dict) and contract.identity_types.get(field_name) != "object":
            continue  # the field sits inside this object (`{v: title, s: i}`): read by its path below
        if v not in (None, ""):
            given[field_name] = v
    # An expression that is just an identity field (`body.url: linkedin_url`, used where the
    # body is an array) reads back like a plain `in` entry; computed ones cannot be inverted.
    for target, expr in (adapter.in_expr or {}).items():
        if isinstance(expr, str) and expr in contract.identity_types and expr not in given:
            v = P.get_path(doc, target)
            if isinstance(v, (str, int)) and not isinstance(v, bool) and v != "":
                given[expr] = v
    # Any other field, filters included, is read back from wherever its expression puts it
    # (`field_paths`: `list(obj('domain', company_domain))`, `at_most(limit, 50)`): the value
    # sent, which is the field's category, though not always its caller's spelling.
    for field_name, target, steps in field_paths(adapter, contract):
        if field_name not in given:
            v = _descend(P.get_path(doc, target), steps)
            if isinstance(v, (str, int)) and not isinstance(v, bool) and v != "":
                given[field_name] = v
    # A name sent as its first and last word (`split_first(full_name)`, `split_last(full_name)`)
    # reads back as those two words: exactly what the provider was asked, never one word alone.
    halves: dict[str, dict[str, Any]] = {}
    for target, expr in (adapter.in_expr or {}).items():
        if isinstance(expr, str) and (m := _NAME_HALF.fullmatch(expr.strip())):
            halves.setdefault(m.group(2), {})[m.group(1)] = P.get_path(doc, target)
    for field_name, got in halves.items():
        first, last = got.get("first"), got.get("last")
        if field_name in contract.identity_types and field_name not in given \
                and isinstance(first, str) and first.strip():
            given[field_name] = f"{first.strip()} {last.strip()}" if isinstance(last, str) and last.strip() \
                else first.strip()
    readable = {**contract.identity_types, **(contract.filters or {})}
    # A field the provider takes reshaped is read back by the adapter's own `in_read` rule.
    for field_name, expr in (adapter.in_read or {}).items():
        if field_name in contract.identity_types:
            try:
                v = P.evaluate(expr, doc)
            except (TypeError, ValueError, AttributeError, IndexError):
                continue  # a malformed request reads back as nothing, never as a failed event
            if isinstance(v, (str, int)) and not isinstance(v, bool) and v != "":
                given[field_name] = v
    # A provider often takes more shapes than the adapter sends: the router sends `full_name`,
    # while a direct caller of the same finder may send `first_name` + `last_name`. A request
    # parameter named exactly like an identity field means that field; derivation then joins
    # the shapes, so both direct calls get the routed call's `input_id`.
    for field_name in readable:
        if field_name in given:
            continue
        v = doc_body.get(field_name, query.get(field_name))
        if isinstance(v, (str, int)) and not isinstance(v, bool) and v not in ("",):
            given[field_name] = v
    return given


def segment(contract: Contract, given: dict) -> tuple[str | None, dict[str, str], dict]:
    """(`input_type`, `input_meta`, the normalized identity): the segment a call is in, with no
    hashing, so published route rules match calls exactly as their events record them."""
    identity, supplied = canonical_identity(contract, given or {})
    norm = {k: n for k in contract.identity_types
            if (n := normalize(k, identity.get(k))) is not None}
    variant = next((v for v in contract.identity if all(k in norm for k in v)), None)
    meta: dict[str, str] = {}
    domain = next((norm[k] for k in _DOMAIN_KEYS if k in norm), None)
    if domain is None and "@" in norm.get("email", ""):
        domain = norm["email"].rsplit("@", 1)[1]
    # `acme.fr.` is acme.fr written with the DNS root.
    domain = domain.rstrip(".") if domain else domain
    if domain and "." in domain:
        meta["domain_class"] = domain_class(domain)
        meta["tld"] = domain.rsplit(".", 1)[1]
    if (code := P.calling_code(identity.get("phone"))) is not None:
        meta["calling_code"] = code
    for k, spec in (contract.filters or {}).items():
        if (spec or {}).get("analytics") == "value" and (s := _stringify(identity.get(k))) is not None:
            meta[k] = s
    if supplied is not None and (segmenter := SEGMENTERS.get(contract.segments)) is not None:
        # Categories only: the raw title, query, name or place never reaches the event. A value
        # a filter already records (`country`) keeps its own spelling. A request read back into
        # no complete variant carries no filters either, so its categories would claim "none
        # sent" for what was only unreadable: it records none.
        meta.update({k: v for k, v in segmenter.compute(identity).items() if k not in meta})
        meta["segments_version"] = segmenter.version
    return ("+".join(variant) if variant else None), meta, norm


_DOMAIN_CLASSES = frozenset({"company", "webmail", "edu", "gov"})
# Placeholder values for asking which variant a call carrying some identity fields is recorded as.
_SAMPLE = {"linkedin_url": "https://www.linkedin.com/in/example-person", "email": "ann.lee@example.com",
           "domain": "example.com", "company_domain": "example.com", "website": "https://example.com",
           "url": "https://example.com", "phone": "+14155550100", "full_name": "Ann Lee",
           "first_name": "Ann", "last_name": "Lee"}
_SAMPLE_BY_TYPE = {"bool": True, "list": ["example"], "object": {"type": "object"}}


def _sample(contract: Contract, key: str):
    return _SAMPLE.get(key, _SAMPLE_BY_TYPE.get(str(contract.identity_types.get(key)), "example"))


def recorded_variants(contract: Contract) -> dict[str, dict]:
    """Each `input_type` this contract's calls can be recorded as, with the widest identity a call
    recorded as it can carry. A variant an earlier one always shadows (`{ai_overview, q}` behind
    `{q}`) is never recorded and is absent."""
    out: dict[str, dict] = {}
    for variant in contract.identity:
        name = "+".join(variant)
        fields = {k: _sample(contract, k) for k in variant}
        if segment(contract, fields)[0] != name:
            continue
        for key in contract.identity_types:
            if key not in fields and segment(contract, {**fields, key: _sample(contract, key)})[0] == name:
                fields[key] = _sample(contract, key)
        out[name] = fields
    return out


def _tld(value: str) -> bool:
    # The last label of a lower-cased host, as `segment` records it: an internationalized name
    # stays in its own script.
    return 0 < len(value) <= 63 and value == value.lower() and "." not in value \
        and not any(c.isspace() for c in value)


def _calling_code(value: str) -> bool:
    return value.isdigit() and 1 <= len(value) <= 3


def segment_checks(contract: Contract) -> dict:
    """Every key `segment` can produce for this contract's calls, each with a test of a value it
    can record: a recorded variant for `input_type`, a lower-cased label for `tld`."""
    checks: dict = {"input_type": recorded_variants(contract).__contains__}
    if any(k in contract.identity_types for k in (*_DOMAIN_KEYS, "email")):
        checks["tld"] = _tld
        checks["domain_class"] = _DOMAIN_CLASSES.__contains__
    if "phone" in contract.identity_types:
        checks["calling_code"] = _calling_code
    for k, spec in (contract.filters or {}).items():
        if (spec or {}).get("analytics") == "value":
            checks[k] = lambda v: True
    if (segmenter := SEGMENTERS.get(contract.segments)) is not None:
        for k, vocab in segmenter.vocab.items():
            checks.setdefault(k, vocab if callable(vocab) else vocab.__contains__)
    return checks


def input_columns(contract: Contract, given: dict) -> tuple[dict, dict]:
    """(input columns, the normalized identity) for what the caller asked."""
    input_type, meta, norm = segment(contract, given)
    variant = tuple(input_type.split("+")) if input_type else None
    cols = {
        "input_type": input_type,
        "input_id": (keyed("\x1f".join(f"{k}={norm[k]}" for k in variant)) if variant else None),
        "input_keys": {k: keyed(v) for k, v in norm.items()} or None,
        "input_meta": meta or None,
    }
    return cols, norm


def adapter_output(adapter: Adapter | None, capability: str, status: int | None,
                   body: bytes | None) -> dict | None:
    """The adapter's core output for a 2xx JSON answer, as the Arena reads it; else None."""
    if adapter is None or not adapter.verified or status is None or not 200 <= status < 300 or not body:
        return None
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(doc, (dict, list)):
        return None
    raw = adapter.from_upstream(doc)
    core = safe_output(raw, capability=capability)
    # The Arena keeps only the booleans and numbers it ranks on (`valid`, `score`, …) and blanks
    # the rest as likely masking flags. A field the contract records and declares `bool`, `int` or
    # `float` is the provider's verdict (`name_match`, `ported`), kept when it has that type.
    contract = catalog_store.load().contracts.get(capability)
    if contract and contract.segments and contract.row_of and isinstance(raw.get(contract.row_of), list):
        # The segment set counts every row the adapter mapped, in the contract's row shape; the
        # Arena's reading keeps the first few and drops the profile and company keys it judges.
        core[contract.row_of] = raw[contract.row_of]
    for field_name, spec in (contract.output if contract else {}).items():
        spec = spec or {}
        kind = _SCALAR_TYPES.get(str(spec.get("type")))
        v = raw.get(field_name)
        if kind and spec.get("analytics", "skip") != "skip" and isinstance(v, kind) \
                and (kind is bool or not isinstance(v, bool)):
            core[field_name] = v
    return core


def answer_columns(contract: Contract, output: dict, input_norm: dict | None = None,
                   asked: dict | None = None) -> dict:
    """`asked` (the identity and filters after derivation) lets the contract's segment set count
    how the rows fit the question (`rows_company_match`); counts only, never a row's value."""
    keys: dict[str, str] = {}
    values: dict[str, str] = {}
    flags: dict[str, bool] = {}
    input_domain = next((input_norm[k] for k in _DOMAIN_KEYS if k in (input_norm or {})), None)
    for field_name, spec in contract.output.items():
        mode = (spec or {}).get("analytics", "skip")
        v = output.get(field_name)
        if mode == "key":
            n = normalize(field_name, v)
            if n is None:
                continue
            keys[field_name] = keyed(n)
            if field_name == "phone" or field_name.endswith("_phone"):
                # The country a number dials into: no person in it, and the segment country
                # analysis needs when the provider names none.
                if (code := P.calling_code(f"+{n}")) is not None:
                    values[f"{field_name}_calling_code"] = code
            if "@" in n:
                local, dom = n.rsplit("@", 1)
                p = "" if field_name == "email" else f"{field_name}_"
                flags[f"{p}is_role"] = local in _ROLE_LOCALS
                flags[f"{p}free_mail"] = P.free_mail(dom)
                if input_domain:
                    flags[f"{p}domain_match"] = dom == input_domain
        elif mode == "value" and (s := _stringify(v)) is not None:
            values[field_name] = s
    segmenter = SEGMENTERS.get(contract.segments)
    if segmenter is not None and segmenter.answer is not None:
        # Without the question (a request that cannot be read back) the row counts still hold;
        # only the checks against it are absent.
        if counts := segmenter.answer(asked or {}, output):
            values.update(counts)
            values["segments_version"] = segmenter.version
    return {"answer_keys": keys or None, "answer_values": values or None,
            "answer_flags": flags or None}


def emit(kind: str, *, org_id: int | None, endpoint_id: str | None, call_ref: str | None,
         capability: str | None = None, query_items: Iterable[tuple[str, str]] = (),
         request_body: bytes | None = None, identity: dict | None = None,
         answer_status: int | None = None, answer_body: bytes | None = None,
         output: dict | None = None, answers_allowed: bool = True, retained_extra: int = 0,
         route_rule: dict[str, str] | None = None, **columns: Any) -> None:
    """Queue one event. Cheap and never raising: the bodies are read later, in the flusher.

    `identity` (a routed parent's caller fields) or `query_items` + `request_body` (a direct
    call's vendor request) say what was asked; `output` (a routed parent's core) or
    `answer_status` + `answer_body` (a vendor answer) what came back. Everything the builder
    keeps alive counts against the sink's byte budget: the bodies, the query, and
    `retained_extra` (the serialized size of `identity` and `output`, which the caller knows).
    `route_rule` (a routed parent's ordering: `route_order`, which of header, rule, quality or
    default decided it, plus `route_rules_version` and `route_rule` when published rules are on)
    joins `input_meta`, so analysis can split a segment by the rules it ran under."""
    try:
        if not sink.enabled():
            return
        parent_ref, step, position = ref_parts(call_ref)
        if capability is None and endpoint_id:
            capability = (catalog_store.load().by_id.get(endpoint_id) or {}).get("capability")
        event = dict.fromkeys(COLUMNS)
        event.update({k: v for k, v in columns.items() if k in event})
        event.update(
            schema_version=SCHEMA_VERSION, event_id=uuid.uuid4().hex,
            event_time=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            event_kind=kind, org_id=org_id, endpoint_id=endpoint_id, capability=capability,
            call_ref=call_ref, parent_ref=parent_ref, step=step, waterfall_position=position,
            is_eval=is_eval(org_id))
        query = tuple(query_items)
        retained = (len(request_body or b"") + len(answer_body or b"") + max(0, retained_extra)
                    + sum(len(k) + len(v) for k, v in query))

        def build() -> dict:
            out = _derived(endpoint_id, capability, query, request_body, identity,
                           answer_status, answer_body, output, answers_allowed)
            if route_rule:
                out["input_meta"] = {**(out.get("input_meta") or {}), **route_rule}
            return out

        sink.emit(event, build, retained_bytes=retained)
    except Exception:  # noqa: BLE001 — an event must never surface into a call
        pass


def _derived(endpoint_id, capability, query, request_body, identity, answer_status, answer_body,
             output, answers_allowed) -> dict:
    cat = catalog_store.load()
    contract = cat.contracts.get(capability or "")
    adapter = cat.adapters.get(endpoint_id or "")
    out: dict = {}
    if adapter is not None:
        out["adapter_fingerprint"] = adapter_fingerprint(adapter)
    if contract is None:
        return out
    if identity is not None:
        given = identity
    elif adapter is not None and (query or request_body):
        given = vendor_identity(adapter, contract, query, request_body)
    else:
        given = {}
    norm: dict = {}
    asked: dict | None = None
    if given:
        cols, norm = input_columns(contract, given)
        out.update(cols)
        asked, _ = canonical_identity(contract, given)
    if answers_allowed:
        core = output if output is not None else adapter_output(adapter, contract.capability,
                                                                answer_status, answer_body)
        if core:
            out.update(answer_columns(contract, core, norm, asked))
    return out
