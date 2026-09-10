"""Sumble platform request boundaries and estimates; BYOK never enters the guard.

Rates and selectable attributes live in the catalog. Responses remain provider-native.
Workspace queries and expanding selections need BYOK until their bounds are reviewed.
"""
from __future__ import annotations

from .types import ResolutionFailed


def _size(value, default: int) -> int:
    return value if type(value) is int and value > 0 else default


def estimate(rule: dict, document: dict) -> int:
    mode = rule["mode"]
    if mode == "single":
        return 1
    if mode == "results":
        return rule["reserve_results"]
    records = document.get(rule["records"])
    n = len(records) if isinstance(records, list) else _size(document.get("limit"), rule.get("default_limit", 1))
    if mode == "lookup":
        return (n + rule["block_size"] - 1) // rule["block_size"]
    select = document.get("select")
    select = select if isinstance(select, dict) else {}
    attrs = select.get("attributes", [])
    paid = (sum(a not in rule["free_attributes"] for a in attrs)
            if isinstance(attrs, list) else len(rule["safe_attributes"]))
    entities = select.get("entities", [])
    metrics = sum(len(e["metrics"]) for e in entities
                  if isinstance(e, dict) and isinstance(e.get("metrics"), list)) if isinstance(entities, list) else 0
    return n * (rule["base"] + paid * rule["attribute"] + metrics * rule["metric"])


def enforce(ep: dict, document: dict, query) -> None:
    def require(ok, message):
        if not ok:
            raise ResolutionFailed("catalog_parameter_invalid", status_code=400, detail={
                "error": "catalog_parameter_invalid", "endpoint_id": ep["id"],
                "message": message + " Use your own Sumble key for other documented options.",
            })

    def keys(value, allowed, label):
        require(isinstance(value, dict) and not (set(value) - set(allowed)),
                f"Platform {label} accepts only {', '.join(sorted(allowed))}.")

    def strings(value, label, maximum=1000):
        require(isinstance(value, list) and 1 <= len(value) <= maximum
                and all(isinstance(x, str) and x for x in value),
                f"Platform {label} requires 1–{maximum} nonempty strings.")

    allowed_query = {"organization_id"} if ep["id"].endswith(".techs") else {"query", "limit"} if ep["id"].endswith("documentation.search") else set()
    require(all(k in allowed_query for k, _ in query.multi_items()), "Unsupported platform query parameter.")
    require(ep.get("verified"), "This operation has not been verified for platform access.")
    rule = (ep.get("cost") or {}).get("sumble")
    if not rule:
        require(not document, "This operation does not accept a platform request body.")
        return
    mode = rule["mode"]
    if mode == "results":
        require(not document, "This operation does not accept a platform request body.")
        return
    if mode == "single":
        keys(document, {"query"}, "technology search")
        require(isinstance(document.get("query"), str) and document["query"], "Specify a technology query.")
        return
    record_key = rule["records"]
    if mode == "lookup":
        keys(document, {record_key}, "lookup")
        strings(document.get(record_key), record_key, rule["max_records"])
        return
    keys(document, {record_key, "filter", "select", "limit", "offset", "order_by_column", "order_by_direction"}, "request")
    records = document.get(record_key)
    filt = document.get("filter")
    require((records is not None) != (filt is not None), "Specify either a record list or an organization_ids filter.")
    if records is not None:
        require(isinstance(records, list) and 1 <= len(records) <= rule["max_records"], "Specify a nonempty record list within the documented limit.")
        for record in records:
            if record_key == "teams":
                require(type(record) is int and record > 0, "Team identifiers must be positive integers.")
            else:
                keys(record, {"id", "slug", "name", "url", "location"} if record_key == "organizations" else {"job_id"}, "record")
                require(bool(record), "Empty records are not supported.")
    else:
        require(record_key != "organizations", "Platform organization enrichment requires explicit organizations; advanced-query search needs BYOK.")
        keys(filt, {"organization_ids", "since"} if record_key == "teams" else {"organization_ids"}, "filter")
        ids = filt.get("organization_ids")
        require(isinstance(ids, list) and 1 <= len(ids) <= rule["max_records"] and all(type(x) is int and x > 0 for x in ids), "Specify positive organization_ids.")
    for field, default, low, high in [("limit", rule["default_limit"], 1, rule["max_limit"]), ("offset", 0, 0, 10000)]:
        value = document.get(field, default)
        require(type(value) is int and low <= value <= high, f"{field} must be an integer from {low} to {high}.")
    if "order_by_column" in document:
        require(record_key == "teams" and document["order_by_column"] in {"jobs_count", "first_activity", "last_activity"}, "Only account-independent team sorting is supported.")
    if "order_by_direction" in document:
        require(document["order_by_direction"] in {"ASC", "DESC"}, "Sort direction must be ASC or DESC.")
    select = document.get("select")
    keys(select, {"attributes", "entities"} if record_key == "organizations" else {"attributes"}, "select")
    attrs = select.get("attributes", [])
    require(isinstance(attrs, list) and all(isinstance(a, str) and a in rule["safe_attributes"] for a in attrs), "Select explicit account-independent attributes; all, ICP and CRM fields are not supported.")
    entities = select.get("entities", [])
    require(isinstance(entities, list), "entities must be an array.")
    for entity in entities:
        keys(entity, {"type", "term", "metrics", "granularity", "since"}, "entity")
        require(entity.get("type") in {"technology", "job_function", "project", "technology_category"}, "Only named technology, job function, project and aggregate category metrics are supported.")
        require(isinstance(entity.get("term"), str) and bool(entity["term"]), "Specify an entity term.")
        require(entity.get("granularity") == "aggregate" if entity.get("type") == "technology_category" else "granularity" not in entity, "Exploded categories are not supported on the platform key.")
        strings(entity.get("metrics"), "metrics")
