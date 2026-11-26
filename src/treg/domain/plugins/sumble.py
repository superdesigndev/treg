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
