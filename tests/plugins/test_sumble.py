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

