import pytest

from treg.domain.plugins import Request, sumble


def _req(body):
    return Request(method="POST", query={}, body=body)


@pytest.mark.parametrize(("body", "credits"), [
    ({"organizations": [{"url": "a.com"}] * 2, "select": {"attributes": ["id", "name", "industry", "employee_count"]}}, 6),
    ({"organizations": [{"id": 1}], "select": {"entities": [{"type": "technology", "term": "python",
                                                             "metrics": ["job_post_count", "people_count"]}]}}, 3),
    ({"organizations": [{"id": 1}], "select": {"attributes": ["id", "industry"],
                                               "entities": [{"metrics": ["a"]}, {"metrics": "all"}, "x"]}}, 3),
    ({"organizations": [{"id": 1}], "select": {"attributes": "all"}}, 27),
    ({"organizations": [], "select": {"attributes": ["industry"]}}, 0),
    ({"filter": {"organization_ids": [1]}, "select": {"attributes": ["industry"]}}, 20),
    ({"filter": {"organization_ids": [1]}, "limit": 3, "select": {"attributes": []}}, 3),
    ({"filter": {"organization_ids": [1]}, "limit": True, "select": {}}, 10),
    ({"select": "x"}, 10),
    (None, 10),
])
def test_organization_credits_hold_each_record_at_its_attribute_and_metric_price(body, credits):
    assert sumble.organization_credits(_req(body)) == credits


@pytest.mark.parametrize(("body", "credits"), [
    ({"filter": {"organization_ids": [1]}, "limit": 2, "select": {"attributes": ["title", "location", "posted_date"]}}, 6),
    ({"jobs": [{"job_id": 1}], "select": {"attributes": ["title"]}}, 1),
    ({"jobs": [{"job_id": 1}] * 200, "select": {"attributes": sorted(sumble.SAFE_ATTRIBUTES["jobs"])}}, 2000),
    ({"filter": {"organization_ids": [1]}, "limit": 0, "select": {"attributes": ["location"]}}, 20),
])
def test_job_credits_leave_the_title_free(body, credits):
    assert sumble.job_credits(_req(body)) == credits


@pytest.mark.parametrize(("body", "credits"), [
    ({"teams": [1, 2], "select": {"attributes": ["jobs_count", "breadcrumbs"]}}, 6),
    ({"filter": {"organization_ids": [1]}, "limit": 1, "select": {"attributes": ["jobs_count"]}}, 2),
    ({"filter": {"organization_ids": [1]}, "select": {"attributes": []}}, 10),
])
def test_team_credits_charge_every_attribute(body, credits):
    assert sumble.team_credits(_req(body)) == credits


@pytest.mark.parametrize("body", [None, {}, {"anything": 1}])
def test_tech_stack_hold_is_one_fixed_count(body):
    assert sumble.tech_stack_hold(_req(body)) == sumble.TECH_STACK_HOLD



def _q(query=None, body=None):
    return Request(method="POST", query=query or {}, body=body if body is not None else {})


@pytest.mark.parametrize(("check", "request_", "ok"), [
    (sumble.organizations_request, _q(body={"organizations": [{"url": "a.com"}], "select": {"attributes": ["industry"]}}), True),
    (sumble.organizations_request, _q(body={"filter": {"organization_ids": [1]}, "select": {
        "entities": [{"type": "technology", "term": "python", "metrics": ["job_post_count"]}]}}), True),
    (sumble.organizations_request, _q(body={"organizations": [{"url": "a.com"}], "select": {"attributes": "all"}}), False),
    (sumble.organizations_request, _q(body={"organizations": [{"url": "a.com"}], "filter": {"organization_ids": [1]},
                                            "select": {}}), False),
    (sumble.organizations_request, _q(body={"organizations": [{"url": "a.com"}], "select": {
        "entities": [{"type": "technology_category", "term": "db", "metrics": ["x"]}]}}), False),
    (sumble.organizations_request, _q({"x": "1"}, {"organizations": [{"id": 1}], "select": {}}), False),
    (sumble.organizations_request, _q(body={}), False),
    (sumble.jobs_request, _q(body={"jobs": [{"job_id": 1}], "select": {"attributes": ["title"]}}), True),
    (sumble.jobs_request, _q(body={"jobs": [{"job_id": 1}], "limit": 201, "select": {}}), False),
    (sumble.teams_request, _q(body={"teams": [7], "order_by_column": "jobs_count", "select": {}}), True),
    (sumble.teams_request, _q(body={"teams": [0], "select": {}}), False),
    (sumble.technologies_lookup_request, _q(body={"technologies": ["python"]}), True),
    (sumble.technologies_lookup_request, _q(body={"technologies": []}), False),
    (sumble.technology_categories_lookup_request, _q(body={"technology_categories": ["db"]}), True),
    (sumble.technology_categories_lookup_request, _q(body={"technologies": ["db"]}), False),
    (sumble.job_functions_lookup_request, _q(body={"titles": ["engineer"]}), True),
    (sumble.projects_lookup_request, _q(body={"projects": ["k8s"]}), True),
    (sumble.projects_lookup_request, _q(), False),
    (sumble.technologies_find_request, _q(body={"query": "postgres"}), True),
    (sumble.technologies_find_request, _q(body={"query": ""}), False),
    (sumble.organization_techs_request, _q({"organization_id": "1"}), True),
    (sumble.organization_techs_request, _q({"organization_id": "1"}, {"a": 1}), False),
    (sumble.documentation_search_request, _q({"query": "api", "limit": "5"}), True),
    (sumble.documentation_search_request, _q({"query": "api", "page": "2"}), False),
])
def test_platform_checks_serve_only_account_independent_requests(check, request_, ok):
    reason = check(request_)
    assert (reason is None) is ok
    if not ok:
        assert reason["message"].endswith("Use your own Sumble key for other documented options.")
