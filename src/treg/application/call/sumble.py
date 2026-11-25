"""Sumble request boundaries on treg's key; a team's own key never enters the guard.

The rows declare their prices, holds and the declared body fields treg's key refuses. This guard
keeps what a row cannot declare: the fields and values treg's key serves at all, because a
selection Sumble bills outside the hold (related people, job posts, `all`, ICP and CRM fields,
exploded categories) must never reach the platform key.
"""
from __future__ import annotations

from treg.domain.plugins import sumble as rules

from .types import ResolutionFailed

# Each served row's request shape: a composed record request (its record list key), a list
# lookup (its input list key), one technology query, or no body at all.
_COMPOSE = {"sumble.organizations": "organizations", "sumble.jobs": "jobs", "sumble.teams": "teams"}
_LOOKUP = {
    "sumble.technologies.lookup": "technologies",
    "sumble.technologies.categories.lookup": "technology_categories",
    "sumble.job_functions.lookup": "titles",
    "sumble.projects.lookup": "projects",
}
_SINGLE = {"sumble.technologies.find"}
_QUERY = {"sumble.organizations.techs": {"organization_id"}, "sumble.documentation.search": {"query", "limit"}}
_ENTITY_TYPES = {"technology", "job_function", "project", "technology_category"}
_TEAM_SORT = {"jobs_count", "first_activity", "last_activity"}


def enforce(ep: dict, document: dict, query) -> None:
    def require(ok, message):
        if not ok:
            raise ResolutionFailed("catalog_parameter_invalid", status_code=400, rule="platform", detail={
                "error": "catalog_parameter_invalid", "endpoint_id": ep["id"],
                "message": message + " Use your own Sumble key for other documented options.",
            })

    def keys(value, allowed, label):
        require(isinstance(value, dict) and not (set(value) - set(allowed)),
                f"Platform {label} accepts only {', '.join(sorted(allowed))}.")

    def strings(value, label):
        require(isinstance(value, list) and len(value) >= 1
                and all(isinstance(x, str) and x for x in value),
                f"Platform {label} requires nonempty strings.")

    endpoint_id = ep["id"]
    allowed_query = _QUERY.get(endpoint_id, set())
    require(all(k in allowed_query for k, _ in query.multi_items()), "Unsupported platform query parameter.")
    require(ep.get("verified"), "This operation has not been verified for platform access.")
    if endpoint_id in _SINGLE:
        keys(document, {"query"}, "technology search")
        require(isinstance(document.get("query"), str) and document["query"], "Specify a technology query.")
        return
    if endpoint_id in _LOOKUP:
        record_key = _LOOKUP[endpoint_id]
        keys(document, {record_key}, "lookup")
        strings(document.get(record_key), record_key)
        return
    record_key = _COMPOSE.get(endpoint_id)
    if record_key is None:
        require(not document, "This operation does not accept a platform request body.")
        return
    keys(document, {record_key, "filter", "select", "limit", "offset", "order_by_column", "order_by_direction"}, "request")
    records = document.get(record_key)
    filt = document.get("filter")
    require((records is not None) != (filt is not None), "Specify either a record list or an organization_ids filter.")
    if records is not None:
        require(isinstance(records, list) and 1 <= len(records) <= rules.MAX_RECORDS, "Specify a nonempty record list within the documented limit.")
        for record in records:
            if record_key == "teams":
                require(type(record) is int and record > 0, "Team identifiers must be positive integers.")
            else:
                keys(record, {"id", "slug", "name", "url", "location"} if record_key == "organizations" else {"job_id"}, "record")
                require(bool(record), "Empty records are not supported.")
    else:
        keys(filt, {"organization_ids", "since"} if record_key == "teams" else {"organization_ids"}, "filter")
        ids = filt.get("organization_ids")
        require(isinstance(ids, list) and 1 <= len(ids) <= rules.MAX_RECORDS and all(type(x) is int and x > 0 for x in ids), "Specify positive organization_ids.")
    for field, default, low, high in [("limit", rules.DEFAULT_LIMIT, 1, rules.MAX_LIMIT), ("offset", 0, 0, rules.MAX_OFFSET)]:
        value = document.get(field, default)
        require(type(value) is int and low <= value <= high, f"{field} must be an integer from {low} to {high}.")
    if "order_by_column" in document:
        require(record_key == "teams" and document["order_by_column"] in _TEAM_SORT, "Only account-independent team sorting is supported.")
    if "order_by_direction" in document:
        require(document["order_by_direction"] in {"ASC", "DESC"}, "Sort direction must be ASC or DESC.")
    select = document.get("select")
    keys(select, {"attributes", "entities"} if record_key == "organizations" else {"attributes"}, "select")
    attrs = select.get("attributes", [])
    require(isinstance(attrs, list) and all(isinstance(a, str) and a in rules.SAFE_ATTRIBUTES[record_key] for a in attrs), "Select explicit account-independent attributes; all, ICP and CRM fields are not supported.")
    entities = select.get("entities", [])
    require(isinstance(entities, list), "entities must be an array.")
    for entity in entities:
        keys(entity, {"type", "term", "metrics", "granularity", "since"}, "entity")
        require(entity.get("type") in _ENTITY_TYPES, "Only named technology, job function, project and aggregate category metrics are supported.")
        require(isinstance(entity.get("term"), str) and bool(entity["term"]), "Specify an entity term.")
        require(entity.get("granularity") == "aggregate" if entity.get("type") == "technology_category" else "granularity" not in entity, "Exploded categories are not supported on the platform key.")
        require(isinstance(entity.get("metrics"), list) and 1 <= len(entity["metrics"]) <= 1000
                and all(isinstance(x, str) and x for x in entity["metrics"]),
                "Platform metrics requires 1–1000 nonempty strings.")
