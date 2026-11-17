"""The catalog views: `treg catalog`, `catalog <platform>`, `catalog search`, `catalog get`."""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable

from . import toon

GET_HELP = "treg catalog get <id>   # params, price, a ready treg call line"
ROUTED_NOTE = "lets treg pick the provider (own keys first, then cheapest per hit); its price varies"
METRICS_NOTE = ("works is the share of calls the provider answered without error; hit is the share that "
                "found something, so a provider can work 100% and still miss; both are measured by treg")
COMPARE_NOTE = METRICS_NOTE + "; last_ok_days is measured, verified is the catalog's hand-run stamp"
COMPARE_FIELDS = ("id", "price", "works", "calls", "hit", "p50", "last_ok_days", "verified")
EXAMPLE_FIELDS = 40

# `catalog get <id>` in the order an agent acts on it, one paragraph each.
GET_GROUPS = [
    ("id", "name", "summary", "note", "job", "upstream"),
    ("price", "native_price", "price_note", "overflow_price", "settle", "access", "call", "requires",
     "stops_at", "complete"),
    ("route_headers",),
    ("params_note", "params_shared", "params", "param_examples"),
    ("price_table", "async"),
    ("example_response", "example_more"),
    ("routes_note", "routes", "also_total", "also"),
    ("routed", "pinned", "compare_total", "compare_note", "compare"),
    ("provider", "limits", "pricing", "docs", "verified", "added"),
]

View = tuple[dict, list[str]]
Failure = tuple[str, str, list[str] | None, dict]


def cost_label(cost) -> str:
    if not isinstance(cost, dict):
        return "-"
    if cost.get("display_unit") and cost.get("display_usd") is not None:
        return (cost.get("display_prefix", "") + f"${cost['display_usd']:.3g}" + cost.get("display_suffix", "")
                + "/" + cost["display_unit"])
    kind = (cost.get("type") or "").replace("_", " ")
    value, currency = cost.get("value"), cost.get("currency") or ""
    if value in (None, "") and isinstance(cost.get("table"), list):
        value = (cost.get("fallback") or {}).get("value")
    if kind == "free":
        return "free"
    if value in (None, ""):
        return kind or "-"
    unit = {"per call": "call", "per result": "result", "per success": "success",
            "quota rows": "call"}.get(kind, kind or "call")
    usd = cost.get("usd")
    if usd is not None:
        native = "" if currency in ("USD", "") else f" ({native_amount(value, currency, cost.get('unit') or '')})"
        return f"${usd:g}/{unit}{native}"
    return f"{native_amount(value, currency, cost.get('unit') or '')}/{unit}"


def native_amount(value, currency: str, meter: str = "") -> str:
    if currency == "credit":
        return f"{value:g} credit{'' if value == 1 else 's'}"
    if currency == "unit":
        noun = (meter or "unit").replace("_", " ")
        return f"{value:g} {noun}{'' if value == 1 else 's'}"
    return f"${value:g}" if currency in ("USD", "") else f"{currency} {value:g}"


def cost_usd(cost: dict | None) -> str:
    """One currency, so a column of prices compares; the native amount is `catalog get`'s."""
    if not isinstance(cost, dict):
        return "-"
    if cost.get("varies"):
        return "variable"
    if cost.get("display_unit") and cost.get("display_usd") is not None:
        return (cost.get("display_prefix", "") + f"${cost['display_usd']:.3g}" + cost.get("display_suffix", "")
                + "/" + cost["display_unit"])
    usd = cost.get("usd")
    if usd is None:
        return cost_label(cost)
    unit = {"per_call": "call", "per_result": "result", "per_success": "success",
            "per call": "call", "per result": "result", "per success": "success"}.get(cost.get("type"), "call")
    if not usd:
        return "free"
    rate = cost.get("rate_usd")
    if isinstance(rate, (int, float)) and cost.get("rate_unit"):
        low = cost.get("rate_usd_min")
        if isinstance(low, (int, float)) and low < rate:
            return f"${low:.3g}-${rate:.3g}/{cost['rate_unit']}"
        return f"${rate:.3g}/{cost['rate_unit']}"
    low = cost.get("usd_min")
    if isinstance(low, (int, float)) and low < usd:
        return f"${low:.3g}-${usd:.3g}/{unit}"
    return f"${usd:.3g}/{unit}"


def clip(text: str, width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def as_sent(value) -> str:
    """A value as the request carries it: JSON, not Python's repr."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def count(n) -> int | str | None:
    if not isinstance(n, int) or n < 1000:
        return n
    v, unit = (n / 1e6, "M") if n >= 999_500 else (n / 1e3, "k")
    return (f"{v:.1f}".rstrip("0").rstrip(".") if v < 10 else str(round(v))) + unit


def pct(rate) -> str | None:
    return None if rate is None else f"{round(rate * 100, 1):g}%"


def duration(ms) -> str | None:
    return None if ms is None else (f"{ms}ms" if ms < 1000 else f"{ms / 1000:.1f}s")


def usd_or_none(usd) -> str | None:
    return None if usd is None else ("free" if usd == 0 else f"${usd:.4g}")


def refusal(status: int, body: dict, text: str) -> Failure:
    """A registry refusal as the CLI states it. `body` is the JSON object answered, or {}."""
    detail = body.get("detail", body) if body else None
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("error") or f"HTTP {status}"
    else:
        message = detail or text[:500] or f"HTTP {status}"
    return (f"http_{status}", str(message), None,
            {"status": status, "detail": detail if isinstance(detail, dict) else None})


def failure_text(failure: Failure) -> str:
    error, message, help_lines, details = failure
    return toon.printed({"error": error, "message": message, **details}, help_lines)


def platforms(body: dict) -> View:
    rows = body.get("platforms", [])
    shelves = [{"platform": p["slug"], "category": p.get("category") or "Other", "endpoints": p["endpoints"],
                "jobs": p["capabilities"], "providers": ", ".join(p["providers"])} for p in rows]
    return ({"total": len(rows),
             "platforms": toon.table(shelves, ("platform", "category", "endpoints", "jobs", "providers"))},
            ["treg catalog <platform>   # its endpoints, by job",
             "treg catalog search \"<task>\"   # find a tool by what it does"])


def platform(body: dict, connected: set) -> View:
    endpoints = [(cap["id"], e) for cap in body.get("capabilities", []) for e in cap["endpoints"]] \
        + [(None, e) for e in body.get("extended", [])]
    own_key = any(e["provider"] in connected for _, e in endpoints)
    fields = ("id", "job", "price") + (("own_key",) if own_key else ()) + ("name",)
    rows = [{"id": e["id"], "job": job, "price": cost_usd(e.get("cost")), "own_key": e["provider"] in connected,
             "name": clip(e.get("name") or e.get("summary") or "", 120)} for job, e in endpoints]
    jobs = [{"job": cap["id"], "does": cap.get("description")} for cap in body.get("capabilities", [])]
    return ({"platform": body["platform"]["slug"], "label": body["platform"]["label"],
             "jobs": toon.table(jobs, ("job", "does")) or None,
             "endpoints": toon.table(rows, fields),
             "routed": f"a treg.<job> id {ROUTED_NOTE}; any other id is one provider"
             if any(e.get("kind") == "routed" for _, e in endpoints) else None},
            [GET_HELP])


def search(body: dict, *, query: str, days: int | None, sort: str, connected: Callable[[], set]) -> View:
    """`days` and `sort` are the options as the caller typed them. `connected()` names the
    providers the caller holds a credential for, asked only when there are results."""
    added = days is not None or sort == "newest"
    rows = body.get("results", [])
    view: dict = {"query": query or None}
    if added:
        view |= {"added_within_days": body.get("added_within_days"), "capped_at_days": body.get("capped_at_days"),
                 "sort": body.get("sort")}
    if body.get("verdict") == "none":
        return (view | {"total": 0, "results": [], "note": f"the catalog has no tool for \"{query}\""},
                [f"treg catalog request \"{query}\"   # requests steer what gets added next"])
    if not rows:
        near = [{"id": n["endpoint_id"], "matches": ", ".join(n["matches"]), "missing": ", ".join(n["missing"])}
                for n in (body.get("near") or [])[:3]]
        return (view | {"total": 0, "results": [], "almost": toon.table(near, ("id", "matches", "missing")) or None},
                ["treg catalog search \"<other task words>\"",
                 f"treg catalog request \"{query}\"   # requests steer what gets added next"])
    held = connected()
    own_key = any(e["provider"] in held for e in rows)
    fields = ("id", "job", "price") + (("added",) if added else ()) + (("own_key",) if own_key else ()) + ("name",)
    out_rows, help_lines = [], []
    for e in rows:
        name = e.get("name") or e.get("summary") or ""
        if e.get("kind") == "hub":
            name = f"hub tool by {e.get('provider', '')}: {name}"
        if e.get("kind") == "routed" and e.get("children_hidden"):
            help_lines.append(f"treg catalog get {e['id']}   # all providers of {e.get('capability')}")
        out_rows.append({"id": e["id"], "job": e.get("capability"), "price": cost_usd(e.get("cost")),
                         "added": e.get("added"), "own_key": e["provider"] in held, "name": clip(name, 120)})
    view |= {"total": body.get("total", len(rows)), "results": toon.table(out_rows, fields)}
    if body.get("verdict") == "closest":
        view["note"] = "no exact fit: read treg catalog get before relying on one, or say the task differently"
    elif body.get("verdict") == "keyword" and body.get("reason") not in (None, "", "not_task"):
        view["note"] = "ranked by keywords only (the judge did not answer)"
    if any(e.get("kind") == "routed" for e in rows):
        view["routed"] = f"a treg.<job> id {ROUTED_NOTE}; any other id is one provider"
    help_lines.insert(0, GET_HELP)
    if body.get("total", 0) > len(rows):
        again = ([shlex.quote(query.strip())] if query.strip() else []) + ([f"--new {days}"] if days is not None else []) \
            + (["--sort newest"] if sort == "newest" else [])
        help_lines.append(" ".join(["treg catalog search", *again, f"--limit {min(body['total'], 100)}"]))
    return view, help_lines


def unknown_endpoint(endpoint_id: str, body: dict) -> Failure:
    detail = body.get("detail")
    near = (detail.get("did_you_mean") or []) if isinstance(detail, dict) else []
    # The whole id minus its provider, as words: the last segment alone matches half the catalog.
    terms = " ".join(endpoint_id.split(".")[1:] or [endpoint_id]).replace("-", " ").replace("_", " ").strip()
    return ("unknown_endpoint", f"no endpoint or job {endpoint_id!r} in the catalog",
            [f"treg catalog get {near[0]}"] if near else [f"treg catalog search {terms}"],
            {"did_you_mean": near or None})


def head(e: dict) -> dict:
    return {"id": e["id"], "name": e.get("name") or None, "summary": e.get("summary") or None,
            "note": e.get("note") or None}


def hub(e: dict) -> dict:
    """A hub tool's contract: what a caller needs, nothing the maker hides."""
    chk = e.get("check") or {}
    out = e.get("output") or {}
    inputs = [{"name": k, "type": v.get("type"), "required": "default" not in v,
               "default": as_sent(v["default"]) if "default" in v else None, "max": v.get("max"),
               "note": v.get("note")} for k, v in (e.get("inputs") or {}).items()]
    ct = e.get("call_template") or {}
    return {
        "maker": e.get("provider"),
        "made_of": f"{e.get('made_of')} tools; {e.get('recipe')} recipe, v{e.get('version')}",
        "price": e.get("price_line"),
        "price_per_run": e.get("price_range"),
        "health": f"{e.get('status')}; check {chk.get('status') or 'none'}"
                  + (f" at {str(chk.get('checked_at'))[:16]}" if chk.get("checked_at") else ""),
        "call": ct.get("cli"),
        "http": f"{ct.get('http', '')} with X-Treg-Token and a JSON body of inputs" if ct.get("http") else None,
        "inputs": toon.table(inputs, ("name", "type", "required", "default", "max", "note")) or None,
        "output": out.get("fields") if isinstance(out, dict) and "fields" in out else list(out) or None,
        "added": e.get("added"),
        "page": e.get("page"),
    }


def endpoint(body: dict, *, access: str | None, pinned: Callable[[str | None], str | None],
             limit: int | None, base_url: str) -> tuple[dict, list[str]]:
    """One catalog endpoint, everything about it. `access` is which credential would serve this
    caller; `pinned(capability)` is the provider its team pinned, asked only where it matters."""
    e, prov = body["endpoint"], body.get("provider") or {}
    cost = e.get("cost") or {}
    template = body["call_template"]
    if e.get("async") and "--await" not in template:
        template += " --await --timeout 900"
    routed_id = e.get("kind") == "routed"
    h = head(e)
    view = h | {
        "name": None if routed_id or h.get("summary") else h.get("name"),
        "job": None if routed_id else e.get("capability"),
        "upstream": f"{e['method']} {e['path']}" if e.get("method") and e.get("path") and not routed_id else None,
        "price": routed_price(body) if routed_id else cost_usd(cost) if cost else "not priced",
        "native_price": native_amount(cost["value"], cost["currency"])
        if cost.get("currency", "USD") != "USD" and cost.get("value") is not None and cost.get("usd") is not None else None,
        "price_note": cost.get("note"),
        "overflow_price": f"${e['overflow_price_usd']:g}/{e.get('overflow_price_unit') or 'call'} through "
                          f"{e.get('overflow_via')} when treg's own account for it is out"
        if e.get("overflow_price_usd") is not None else None,
        "access": None if routed_id else access,
        "call": with_route_cap(template, (body.get("routing") or {}).get("max_cost_usd")) if routed_id else template,
        "requires": requires(body.get("routing")),
        **answer_bar(body.get("routing")),
        **params_view(e.get("input") or {}),
        **price_table_view(cost),
        "async": async_view(e.get("async")),
        **example_view(body.get("example_response"), e["id"], base_url),
        **compare_view(e, body, limit, pinned),
        "provider": None if routed_id else f"{prov.get('display_name', e['provider'])} ({e['provider']})",
        "limits": prov.get("limits"),
        "pricing": prov.get("pricing_url"),
        "docs": e.get("docs_url") or prov.get("docs"),
        "verified": "generated from its children's verified adapters" if e.get("kind") == "routed"
        else e.get("verified") or "not verified against the live API",
        "added": e.get("added"),
    }
    help_lines = []
    rows = view.get("compare") if isinstance(view.get("compare"), list) else []
    routed = e["id"] if routed_id else next((x["id"] for x in rows if x["id"].startswith("treg.")), None)
    if routed_id and view.get("also_total") and not view.get("also"):
        help_lines.append(f"treg catalog get {e['id']} --limit {view['also_total']}   # the {view['also_total']} "
                          f"providers of {e.get('capability')} treg does not route to, called by id")
    elif routed and not routed_id:
        total = view.get("compare_total")
        help_lines.append(f"treg catalog get {routed}{f' --limit {total}' if total else ''}   # let treg pick the "
                          f"provider; its plan and every other provider of this job")
    elif view.get("compare_total") and e.get("capability"):
        help_lines.append(f"treg catalog get {e['capability']}   # every provider of this job")
    if view.get("example_more"):
        help_lines.append(f"treg --json catalog get {e['id']} | jq .example_response   # the whole example")
    if not routed and (others := [row["id"] for row in rows if row["id"] != e["id"]]):
        help_lines.append(f"treg catalog get {others[0]}   # the next way to do this job in compare")
    return view, help_lines


def routed_member(capability: dict) -> str | None:
    """The routed id among a job's endpoints, which `catalog get <job>` answers with."""
    return next((x["id"] for x in capability["endpoints"] if x.get("kind") == "routed"), None)


def job(job_id: str, does: str | None, body: dict, pinned: str | None) -> View:
    """A job without a routed id: every provider of it, ranked as `compare` ranks them. `body` is
    the `catalog get` answer for the job's first endpoint."""
    members = [body["endpoint"], *(body.get("siblings") or [])]
    ranked = sorted((x for x in members if not pinned or x["provider"] == pinned), key=compare_rank)
    return ({"job": job_id, "does": does,
             "providers": len({x["provider"] for x in ranked}),
             "pinned": pinned_note(pinned, job_id),
             "compare_note": COMPARE_NOTE, "compare": compare_rows(ranked)},
            [GET_HELP])


def example_view(example, endpoint_id: str, base_url: str) -> dict:
    """The example response's shape, cut at about `EXAMPLE_FIELDS` values. Each object keeps its
    plain fields before its nested ones, a list keeps its first item, and whatever is cut goes
    whole, so the cut still decodes."""
    if example is None:
        return {}
    budget = [EXAMPLE_FIELDS]

    def nested(x) -> bool:
        return isinstance(x, (dict, list))

    def cut(v):
        if isinstance(v, list):
            return [cut(v[0])] if v and budget[0] > 0 else []
        if not isinstance(v, dict):
            budget[0] -= 1
            return v
        kept = {}
        for k, x in v.items():
            if not nested(x) and budget[0] > 0:
                budget[0] -= 1
                kept[k] = x
        for k, x in v.items():
            if nested(x) and budget[0] > 0:
                kept[k] = cut(x)
        return {k: kept[k] for k in v if k in kept}

    def leaves(v) -> int:
        if isinstance(v, dict):
            return sum(leaves(x) for x in v.values())
        if isinstance(v, list):
            return sum(leaves(x) for x in v)
        return 1

    clipped, total = cut(example), leaves(example)
    base = base_url.rstrip("/")
    return {"example_response": clipped,
            "example_more": f"cut to {leaves(clipped)} of {total} values; the whole example: "
                            f"{base}/catalog/examples/{endpoint_id}" if leaves(clipped) < total else None}


def with_route_cap(template: str, max_cost_usd) -> str:
    cap = f"--header 'X-Treg-Route-Max-Cost: {max_cost_usd if isinstance(max_cost_usd, (int, float)) else 1:g}'"
    return template.replace(" --data ", f" {cap} --data ", 1) if " --data " in template else f"{template} {cap}"


def requires(routing: dict | None) -> str | None:
    sets = ((routing or {}).get("contract") or {}).get("identity") or []
    names = [" + ".join(s) for s in sets if s]
    return None if not names else names[0] if len(names) == 1 else "one of " + " | ".join(names)


def answer_bar(routing: dict | None) -> dict:
    contract = (routing or {}).get("contract") or {}
    return {"stops_at": f"the first answer unless {contract['miss']}" if contract.get("miss") else None,
            "complete": ", ".join(contract["complete"]) + " (X-Treg-Route-Complete: 1 waits for all of them)"
            if contract.get("complete") else None}


def params_view(inp: dict) -> dict:
    """What to send, by location, required first. A nested object becomes dotted names; prose that
    three or more params share is said once, in `params_shared`."""
    rows = []

    def walk(items: dict, where: str, prefix: str) -> None:
        for name, spec in items.items():
            spec = spec if isinstance(spec, dict) else {}
            props = spec.get("properties")
            if isinstance(props, dict) and props:
                walk(props, where, f"{prefix}{name}.")
                continue
            rows.append({"in": where, "name": f"{prefix}{name}", "type": str(spec.get("type") or "") or None,
                         "required": bool(spec.get("required")), "_prose": (spec.get("note") or "").strip(),
                         "_spec": spec})

    for where, key in (("path", "pathParams"), ("query", "queryParams"), ("body", "body")):
        if isinstance(inp.get(key), dict):
            walk(inp[key], where, "")
    rows.sort(key=lambda r: not r["required"])
    examples = {r["name"]: json.dumps(r["_spec"]["example"], ensure_ascii=False, separators=(",", ":"))
                for r in rows if r["_spec"].get("example") not in (None, "")}
    counts: dict[str, list[str]] = {}
    for r in rows:
        if r["_prose"]:
            counts.setdefault(r["_prose"], []).append(r["name"])
    shared = {prose: names for prose, names in counts.items() if len(names) >= 3 and " " in prose}
    for r in rows:
        spec, parts = r.pop("_spec"), []
        prose = r.pop("_prose")
        if prose and prose not in shared:
            parts.append(prose)
        if isinstance(spec.get("enum"), list) and spec["enum"]:
            parts.append("one of: " + " | ".join(as_sent(v) for v in spec["enum"]))
        if "default" in spec:
            parts.append(f"default {as_sent(spec['default'])}")
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None or hi is not None:
            parts.append(f"range {lo if lo is not None else '…'}-{hi if hi is not None else '…'}")
        r["note"] = " · ".join(p.rstrip(".") if i else p for i, p in enumerate(parts) if p) or None
    return {"params_note": inp.get("note"),
            "params_shared": [{"params": ", ".join(names), "note": prose} for prose, names in shared.items()] or None,
            "params": toon.table(rows, ("in", "name", "type", "required", "note")) or None,
            "param_examples": examples or None}


def price_table_view(cost) -> dict:
    if not isinstance(cost, dict) or not isinstance(cost.get("table"), list) or not cost["table"]:
        return {}
    cur = cost.get("currency") or "USD"
    money = (lambda v: f"${v:g}") if cur == "USD" else (lambda v: f"{v:g} {cur}")
    rows = []
    for row in cost["table"]:
        if not isinstance(row, dict):
            continue
        price = money(float(row.get("value") or 0))
        if row.get("times"):
            price += f" × {str(row['times']).split('.', 1)[-1]}"
            if row.get("times_min") is not None:
                price += f" (from {row['times_min']})"
        rows.append({"when": " · ".join(f"{k.split('.', 1)[-1]}={v}" for k, v in (row.get("when") or {}).items()),
                     "price": price})
    fb = cost.get("fallback") or {}
    if isinstance(fb, dict) and fb.get("value") is not None:
        rows.append({"when": "fallback (ceiling)", "price": money(float(fb["value"]))})
    if cost.get("settle", "table") == "usage":
        usage = cost.get("usage") or {}
        reported = (", ".join(str(t.get("path")) for t in usage["terms"]) + " at their rates"
                    if usage.get("terms") else usage.get("path", "usage"))
        settle = (f"usage: the matched row is reserved; the provider's reported {reported} is what you pay, "
                  "which can exceed the reserve when the provider applies a minimum charge")
    else:
        settle = "table: the matched row is reserved at submission and charged when the task succeeds"
    return {"price_table": rows, "settle": settle}


def async_view(desc) -> dict | None:
    """How an async call is followed: the descriptor `--await` executes, for polling by hand."""
    if not isinstance(desc, dict) or not desc:
        return None
    poll, status, result = desc.get("poll") or {}, desc.get("status") or {}, desc.get("result") or {}
    every = f"every ~{desc.get('interval', 10)} s"
    if poll.get("endpoint"):
        poll_how = f"treg call {poll['endpoint']} -p {(poll.get('param') or {}).get('name')}=<task id>, {every}"
    elif poll.get("url_from"):
        poll_how = f"the URL in response field {poll['url_from']} (hosts: {', '.join(poll.get('url_hosts') or [])}), {every}"
    else:
        poll_how = None
    if result.get("path"):
        result_how = f"response field {result['path']}"
    elif result.get("fetch"):
        fp = result.get("fetch_param") or {}
        result_how = f"treg call {result['fetch']} -p {fp.get('name')}=<{fp.get('value_from')} from the finished task>"
    else:
        result_how = None
    return {"returns": "at once; the result arrives later (`treg call … --await` follows it for you)",
            "task_id": f"response field {desc.get('id_from')}", "poll": poll_how,
            "done_when": f"{status.get('path')} is one of {status.get('success')}",
            "failed_when": f"{status.get('failure')} (a failed task refunds the hold)",
            "result": result_how,
            "lifetime": f"{result['ttl_note']}; download promptly, treg never stores media" if result.get("ttl_note") else None}


def compare_view(e: dict, body: dict, limit: int | None, pinned: Callable[[str | None], str | None]) -> dict:
    """The other ways to do this job. A routed id gets its plan and, on request (`limit`), the
    providers it does not route to; any other id gets the job's providers, itself first, then the
    measured ones by success rate and price, then the unmeasured, `limit` rows at most."""
    routing = body.get("routing") or {}
    if routing.get("plan"):
        plan = [{"id": c["endpoint_id"], "price": plan_price(c),
                 "accepts": " | ".join("+".join(v) for v in (c.get("accepts") or [])) or None,
                 "works": pct(c.get("works")), "hit": pct(c.get("hit_rate")),
                 "skipped": c.get("skipped") or ("exhausted" if c.get("exhausted") else None)}
                for c in routing["plan"]]
        also = [{"id": a["endpoint_id"], "price": plan_price(a)} for a in routing.get("also") or []]
        per_call = any("~$" in (row["price"] or "") for row in plan)
        return {"routes_note": "the order a call tries, own keys first at call time; "
                               + ("~$/call is one call at the default size, what X-Treg-Route-Max-Cost is checked "
                                  "against, and limit changes it; " if per_call else "") + METRICS_NOTE,
                "routes": toon.table(plan, ("id", "price", "accepts", "works", "hit", "skipped")),
                "route_headers": routing.get("headers") or None,
                "also_total": len(also) if also and (limit is None or len(also) > limit) else None,
                "also": toon.table(also[:limit], ("id", "price")) or None if limit else None}
    sibs = body.get("siblings") or []
    if not sibs:
        return {"compare": f"no other provider does {e['capability']}"} if e.get("capability") else {}
    pin = pinned(e.get("capability"))
    others = sorted((s for s in sibs if not pin or s["provider"] == pin), key=compare_rank)
    # The routed id rides second, under any limit: it is the other way to do this job.
    routed = [s for s in others if s.get("kind") == "routed"]
    rows = [e, *routed, *(s for s in others if s.get("kind") != "routed")][:max(limit or 10, 1 + len(routed))]
    return {"compare_total": len(others) + 1 if len(others) + 1 > len(rows) else None,
            "routed": f"{routed[0]['id']} {ROUTED_NOTE}" if routed else None,
            "compare_note": COMPARE_NOTE, "compare": compare_rows(rows),
            "pinned": pinned_note(pin, e.get("capability"))}


def routed_price(body: dict) -> str:
    usd = [u for row in (body.get("routing") or {}).get("plan") or []
           if isinstance(u := (row.get("cost") or {}).get("usd", row.get("usd")), (int, float))]
    return f"variable, ${min(usd):.3g}–${max(usd):.3g} per hit" if usd else "variable"


def plan_price(row: dict) -> str | None:
    """The unit price, then what one call at the default size costs when that is another number."""
    cost, usd = row.get("cost"), row.get("usd")
    if not cost:
        return usd_or_none(usd)
    unit = cost_usd(cost)
    shown = cost.get("display_usd", cost.get("usd"))
    if usd is None or shown is None or cost.get("varies") or f"{usd:.3g}" == f"{shown:.3g}":
        return unit
    return f"{unit} (~${usd:.3g}/call)"


def compare_rows(endpoints: list[dict]) -> list[dict]:
    rows = []
    for x in endpoints:
        obs = x.get("observed") or {}
        rows.append({"id": x["id"], "price": cost_usd(x.get("cost")), "works": pct(obs.get("ok_rate")),
                     "calls": count(obs.get("samples")), "hit": pct(obs.get("hit_rate")), "p50": duration(obs.get("p50_ms")),
                     "last_ok_days": obs.get("last_ok_days"), "verified": x.get("verified")})
    return toon.table(rows, COMPARE_FIELDS)


def pinned_note(pinned: str | None, job_id: str | None) -> str | None:
    return f"your team pins this job to {pinned}; other providers are refused (admin: treg org unpin {job_id})" \
        if pinned else None


def compare_rank(s: dict) -> tuple:
    """Measured before unmeasured; then the higher success rate, to the percent; then the lower
    price, a varying one after every known price."""
    rate = (s.get("observed") or {}).get("ok_rate")
    cost = s.get("cost") or {}
    usd = cost.get("usd")
    known = isinstance(usd, (int, float)) and not cost.get("varies")
    return (rate is None, -round(rate or 0, 2), usd if known else float("inf"))


def tool_request(capability: str) -> str:
    """`treg catalog request`, once the request is logged."""
    return (f'logged: "{capability}"\n'
            "requests steer which provider gets added next - the most-asked-for tools land first\n")
