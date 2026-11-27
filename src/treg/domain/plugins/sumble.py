"""Sumble's plugins (catalog schema v2, section 5): the hold of the rows whose credits Sumble
composes from the request, and the attribute lists treg's key serves."""

from __future__ import annotations

from . import Request

# Sumble's documented bounds (docs.sumble.com/api): a record list takes up to 1,000 entries, and
# a filter search pages `limit` rows (default 10, at most 200) from `offset` (at most 10,000).
MAX_RECORDS = 1000
DEFAULT_LIMIT = 10
MAX_LIMIT = 200
MAX_OFFSET = 10000
# A tech stack answer has no count parameter and Sumble documents no cap on it.
TECH_STACK_HOLD = 100

# The attributes treg's key serves (they do not depend on the key owner's account, ICP or CRM),
# and the ones Sumble does not charge for. Every other selected attribute costs 1 credit a record.
SAFE_ATTRIBUTES = {
    "organizations": frozenset({
        "id", "slug", "name", "url", "employee_count", "industry", "jobs_count", "teams_count",
        "jobs_count_rollup", "jobs_count_no_rollup", "teams_count_rollup", "teams_count_no_rollup",
        "people_count_rollup", "people_count_no_rollup", "headquarters_country", "sumble_url",
        "parent_id", "subsidiary_ids", "tags", "status", "status_reason", "funding_total_raised",
        "funding_valuation", "funding_last_round_raised", "funding_last_round_type",
        "funding_last_round_date",
    }),
    "jobs": frozenset({
        "title", "description", "location", "posted_date", "organization", "technologies", "teams",
        "job_functions", "job_levels", "projects",
    }),
    "teams": frozenset({
        "organization", "breadcrumbs", "jobs_count", "technology_list", "job_function_list",
        "location_list", "first_activity", "last_activity",
    }),
}
FREE_ATTRIBUTES = {
    "organizations": frozenset({"id", "name", "slug", "url", "sumble_url", "account_status"}),
    "jobs": frozenset({"title"}),
    "teams": frozenset(),
}


def _records(body: dict, key: str) -> int:
    """The records one call returns at most: the listed records, else one filter page."""
    records = body.get(key)
    if isinstance(records, list):
        return len(records)
    limit = body.get("limit")
    return limit if type(limit) is int and limit > 0 else DEFAULT_LIMIT


def _credits(request: Request, key: str) -> int:
    """Sumble's per-record price: 1 base credit, 1 per paid attribute and 1 per requested metric."""
    body = request.body if isinstance(request.body, dict) else {}
    select = body.get("select") if isinstance(body.get("select"), dict) else {}
    attributes = select.get("attributes", [])
    paid = (sum(a not in FREE_ATTRIBUTES[key] for a in attributes) if isinstance(attributes, list)
            else len(SAFE_ATTRIBUTES[key]))
    entities = select.get("entities", [])
    metrics = sum(len(e["metrics"]) for e in entities
                  if isinstance(e, dict) and isinstance(e.get("metrics"), list)) \
        if isinstance(entities, list) else 0
    return _records(body, key) * (1 + paid + metrics)


def organization_credits(request: Request) -> int:
    """POST /organizations: each organization at its attribute and metric price."""
    return _credits(request, "organizations")


def job_credits(request: Request) -> int:
    """POST /jobs: each job at its attribute price."""
    return _credits(request, "jobs")


def team_credits(request: Request) -> int:
    """POST /teams: each team at its attribute price."""
    return _credits(request, "teams")


def tech_stack_hold(request: Request) -> int:
    """GET /organizations/{id}/techs: one credit per technology returned, held at a fixed count."""
    return TECH_STACK_HOLD


# Treg's key serves only the request shapes below: a selection Sumble bills outside the hold
# (related people, job posts, `all`, ICP and CRM fields, exploded categories) must never reach it.
_ENTITY_TYPES = {"technology", "job_function", "project", "technology_category"}
_TEAM_SORT = {"jobs_count", "first_activity", "last_activity"}
_OWN_KEY = " Use your own Sumble key for other documented options."


class _Refused(Exception):
    pass


def _require(ok: object, message: str) -> None:
    if not ok:
        raise _Refused(message + _OWN_KEY)


def _keys(value: object, allowed: set[str], label: str) -> None:
    _require(isinstance(value, dict) and not (set(value) - allowed),
             f"Platform {label} accepts only {', '.join(sorted(allowed))}.")


def _strings(value: object, label: str) -> None:
    _require(isinstance(value, list) and len(value) >= 1 and all(isinstance(x, str) and x for x in value),
             f"Platform {label} requires nonempty strings.")


def _checked(check) -> dict | None:
    try:
        check()
    except _Refused as refused:
        return {"message": str(refused)}
    return None


def _query(request: Request, allowed: set[str]) -> None:
    _require(set(request.query) <= allowed, "Unsupported platform query parameter.")


def _no_body(request: Request) -> None:
    _require(not request.body, "This operation does not accept a platform request body.")


def _compose(request: Request, record_key: str) -> None:
    _query(request, set())
    document = _body(request)
    _keys(document, {record_key, "filter", "select", "limit", "offset", "order_by_column",
                     "order_by_direction"}, "request")
    records, filt = document.get(record_key), document.get("filter")
    _require((records is not None) != (filt is not None), "Specify either a record list or an organization_ids filter.")
    if records is not None:
        _require(isinstance(records, list) and 1 <= len(records) <= MAX_RECORDS,
                 "Specify a nonempty record list within the documented limit.")
        for record in records:
            if record_key == "teams":
                _require(type(record) is int and record > 0, "Team identifiers must be positive integers.")
            else:
                _keys(record, {"id", "slug", "name", "url", "location"} if record_key == "organizations"
                      else {"job_id"}, "record")
                _require(bool(record), "Empty records are not supported.")
    else:
        _keys(filt, {"organization_ids", "since"} if record_key == "teams" else {"organization_ids"}, "filter")
        ids = filt.get("organization_ids")
        _require(isinstance(ids, list) and 1 <= len(ids) <= MAX_RECORDS
                 and all(type(x) is int and x > 0 for x in ids), "Specify positive organization_ids.")
    for name, default, low, high in [("limit", DEFAULT_LIMIT, 1, MAX_LIMIT), ("offset", 0, 0, MAX_OFFSET)]:
        value = document.get(name, default)
        _require(type(value) is int and low <= value <= high, f"{name} must be an integer from {low} to {high}.")
    if "order_by_column" in document:
        _require(record_key == "teams" and document["order_by_column"] in _TEAM_SORT,
                 "Only account-independent team sorting is supported.")
    if "order_by_direction" in document:
        _require(document["order_by_direction"] in {"ASC", "DESC"}, "Sort direction must be ASC or DESC.")
    select = document.get("select")
    _keys(select, {"attributes", "entities"} if record_key == "organizations" else {"attributes"}, "select")
    attrs = select.get("attributes", [])
    _require(isinstance(attrs, list) and all(isinstance(a, str) and a in SAFE_ATTRIBUTES[record_key] for a in attrs),
             "Select explicit account-independent attributes; all, ICP and CRM fields are not supported.")
    entities = select.get("entities", [])
    _require(isinstance(entities, list), "entities must be an array.")
    for entity in entities:
        _keys(entity, {"type", "term", "metrics", "granularity", "since"}, "entity")
        _require(entity.get("type") in _ENTITY_TYPES,
                 "Only named technology, job function, project and aggregate category metrics are supported.")
        _require(isinstance(entity.get("term"), str) and bool(entity["term"]), "Specify an entity term.")
        _require(entity.get("granularity") == "aggregate" if entity.get("type") == "technology_category"
                 else "granularity" not in entity, "Exploded categories are not supported on the platform key.")
        _require(isinstance(entity.get("metrics"), list) and 1 <= len(entity["metrics"]) <= 1000
                 and all(isinstance(x, str) and x for x in entity["metrics"]),
                 "Platform metrics requires 1–1000 nonempty strings.")


def _body(request: Request) -> dict:
    return request.body if isinstance(request.body, dict) else {}


def _lookup(request: Request, record_key: str) -> None:
    _query(request, set())
    _keys(_body(request), {record_key}, "lookup")
    _strings(_body(request).get(record_key), record_key)


def organizations_request(request: Request) -> dict | None:
    """POST /organizations on treg's key: a record list or an organization_ids filter, explicit
    account-independent attributes and named metrics."""
    return _checked(lambda: _compose(request, "organizations"))


def jobs_request(request: Request) -> dict | None:
    """POST /jobs on treg's key: job ids or an organization_ids filter, explicit attributes."""
    return _checked(lambda: _compose(request, "jobs"))


def teams_request(request: Request) -> dict | None:
    """POST /teams on treg's key: team ids or an organization_ids filter, explicit attributes and
    account-independent sorting."""
    return _checked(lambda: _compose(request, "teams"))


def technologies_lookup_request(request: Request) -> dict | None:
    """POST /technologies/lookup on treg's key: a nonempty `technologies` list only."""
    return _checked(lambda: _lookup(request, "technologies"))


def technology_categories_lookup_request(request: Request) -> dict | None:
    """POST /technology-categories/lookup on treg's key: a nonempty `technology_categories` list."""
    return _checked(lambda: _lookup(request, "technology_categories"))


def job_functions_lookup_request(request: Request) -> dict | None:
    """POST /job-functions/lookup on treg's key: a nonempty `titles` list only."""
    return _checked(lambda: _lookup(request, "titles"))


def projects_lookup_request(request: Request) -> dict | None:
    """POST /projects/lookup on treg's key: a nonempty `projects` list only."""
    return _checked(lambda: _lookup(request, "projects"))


def technologies_find_request(request: Request) -> dict | None:
    """POST /technologies/find on treg's key: one nonempty `query` and nothing else."""
    def check() -> None:
        _query(request, set())
        _keys(_body(request), {"query"}, "technology search")
        query = _body(request).get("query")
        _require(isinstance(query, str) and query, "Specify a technology query.")
    return _checked(check)


def organization_techs_request(request: Request) -> dict | None:
    """GET /organizations/{organization_id}/techs on treg's key: no other parameter, no body."""
    def check() -> None:
        _query(request, {"organization_id"})
        _no_body(request)
    return _checked(check)


def documentation_search_request(request: Request) -> dict | None:
    """GET /documentation/search on treg's key: `query` and `limit` only, no body."""
    def check() -> None:
        _query(request, {"query", "limit"})
        _no_body(request)
    return _checked(check)
