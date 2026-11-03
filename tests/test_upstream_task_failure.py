"""A provider that reports a failure INSIDE a 2xx answer (its own `expect` success rule fails, e.g.
a DataForSEO task status other than 20000) did not answer the question. The charge still settles on
what the provider reported; what changes is that treg never calls the answer found: not in the hit
verdict, not in the archive, not to the router, and the caller is told."""
import copy
import json
from pathlib import Path

import pytest

from treg import archive
from treg.application.call import service as call_service
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store
from treg.domain.catalog.results import classify, fails_expect
from test_routing import _relay_by_provider

EP = "dataforseo.google.serp.organic"


def _example(eid: str) -> dict:
    root = Path(catalog_store.__file__).parents[2] / "catalog" / "examples"
    return json.loads((root / f"{eid}.json").read_text())


def _task_failed(doc: dict, code: int = 40101, message: str = "Internal SE Server Error.") -> dict:
    """The same envelope with its only task failed: HTTP 200, envelope 20000, a priced task, no result."""
    doc = copy.deepcopy(doc)
    doc["tasks_error"] = 1
    task = doc["tasks"][0]
    task.update(status_code=code, status_message=message, result_count=0, result=None)
    return doc


FOUND = _example(EP)
FAILED = _task_failed(FOUND)


def test_a_failed_expect_rule_is_never_a_found_answer():
    found = classify(EP, 200, json.dumps(FOUND).encode())
    assert found.state == "found" and found.hit is True
    failed = classify(EP, 200, json.dumps(FAILED).encode())
    assert failed.state == "error" and failed.reason == "expect_failed"
    assert failed.hit is None, "a failed task says nothing about whether the thing exists"
    assert fails_expect(EP, json.dumps(FAILED).encode()) == "tasks.0.status_code=40101"
    assert fails_expect(EP, json.dumps(FOUND).encode()) is None
    assert fails_expect(EP, b"not json") is None, "an unreadable body is not judged"



def test_a_value_the_expect_rule_lists_as_a_miss_is_an_answer(monkeypatch):
    ep = catalog_store.load().by_id[EP]
    monkeypatch.setitem(ep, "expect", {**ep["expect"], "misses": [40101]})
    failed = json.dumps(FAILED).encode()
    assert fails_expect(EP, failed) is None
    assert classify(EP, 200, failed).reason != "expect_failed", "the adapter judges a declared miss"
    other = json.dumps(_task_failed(FOUND, code=50000)).encode()
    assert fails_expect(EP, other) == "tasks.0.status_code=50000", "an unlisted value still fails"

@pytest.fixture
def dfs_platform(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_DATAFORSEO", "login:password")
    monkeypatch.setenv("TREG_PLATFORM_KEY_ANYAPI", "PLATFORM-ANYAPI")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "dataforseo,anyapi")
    get_settings.cache_clear()
    s = get_settings()
    monkeypatch.setattr(s, "archive_mode", "serve")
    monkeypatch.setattr(s, "archive_serve_endpoints", EP)
    monkeypatch.setattr(s, "archive_serve_percent", 100)
    yield
    get_settings.cache_clear()


async def test_a_task_failure_is_charged_as_reported_flagged_and_never_replayed(clients, dfs_platform, monkeypatch):
    seen = []
    body = [{"keyword": "coffee", "location_code": 2840, "language_code": "en"}]
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"dataforseo": [(200, FAILED), (200, FOUND)]}, seen))
    first = await clients.post(f"/call/{EP}", json=body)
    await archive.drain()
    assert first.status_code == 200 and first.json() == FAILED, "the body is relayed untouched"
    assert first.headers["x-treg-cost-micro"] == "2000", "settles on what the provider reported"
    assert first.headers["x-treg-upstream-failed"] == "tasks.0.status_code=40101"
    # The same question again: the stored failure is not an answer, so the provider is asked.
    second = await clients.post(f"/call/{EP}", json=body)
    await archive.drain()
    assert len(seen) == 2 and second.headers.get("x-treg-cache") != "hit"
    assert second.json() == FOUND and "x-treg-upstream-failed" not in second.headers
    # A found answer is still archived and served as before.
    third = await clients.post(f"/call/{EP}", json=body)
    assert len(seen) == 2 and third.headers.get("x-treg-cache") == "hit" and third.json() == FOUND


async def test_the_router_treats_a_task_failure_as_a_miss_and_asks_the_next_provider(clients, dfs_platform, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({
        "dataforseo": [(200, FAILED), (200, FAILED)],
        "*": [(200, {"output": {"data": {"results": [{"title": "t", "link": "https://example.com"}]}}})],
    }, seen))
    r = await clients.post("/call/treg.google.serp.organic", json={"q": "coffee"},
                           headers={"X-Treg-Route-Prefer": "dataforseo,anyapi"})
    assert r.status_code == 200, r.text
    tried = r.json()["_treg"]["tried"]
    # Both DataForSEO organic routes fail the same way; each is a miss that settled on its reported
    # cost, and the waterfall goes on to the next provider exactly as it does after any miss.
    assert [s[0] for s in seen] == ["dataforseo", "dataforseo", "*"]
    for attempt in tried[:2]:
        assert attempt["outcome"] == "miss" and "tasks.0.status_code=40101" in attempt["detail"]
        assert attempt["charged_micro"] == 2000
    assert r.json()["_treg"]["provider"] == "anyapi"


async def test_mcp_says_the_provider_reported_a_failure(clients, dfs_platform, monkeypatch):
    from test_mcp import _call_tool, mcp_session

    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"dataforseo": [(200, FAILED)]}, []))
    token = clients.headers["X-Treg-Token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {"endpoint_id": EP, "params": [{"keyword": "coffee"}]}, token=token)
    assert out["status"] == 200 and out["body"] == FAILED and out["cost_usd"] == 0.002
    assert out["upstream_failed"] == "tasks.0.status_code=40101"
    assert "reported this request as failed" in out["hint"]


@pytest.mark.parametrize("json_mode", [False, True])
def test_treg_call_says_the_provider_reported_a_failure(monkeypatch, capsys, json_mode):
    from types import SimpleNamespace

    import httpx

    from treg import cli

    response = httpx.Response(200, json=FAILED, headers={
        "X-Treg-Cost-Micro": "2000", "X-Treg-Call-Id": "abc", "X-Treg-Upstream-Failed": "tasks.0.status_code=40101"})

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, *args, **kwargs):
            return response

    monkeypatch.setattr(cli, "_client", lambda cfg, **k: FakeClient())
    monkeypatch.setattr(cli, "_JSON_OVERRIDE", json_mode)
    cli.cmd_call(SimpleNamespace(target=EP, path="", method="POST", query=[], data=None, file=None,
                                 content_type=None, header=[], upload=[]), {})
    out = capsys.readouterr()
    if json_mode:
        assert json.loads(out.out)["_treg"]["upstream_failed"] == "tasks.0.status_code=40101"
    else:
        assert json.loads(out.out) == FAILED, "stdout stays the exact body"
        assert "reported this request as failed (tasks.0.status_code=40101)" in out.err
