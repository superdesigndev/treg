"""Call events: off by default, never in the call's way, pseudonymous, and joinable."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from httpx import AsyncClient

from treg import call_events as sink
from treg.application import asynctasks as async_task_app
from treg.application import call_events as ev
from treg.application.call import route as call_route
from treg.application.call import service as call_service
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store
from treg.domain.catalog.routing import contracts as C

from test_bounceban_async import (  # noqa: F401
    EMAIL, STATUS, VERIFY, VERIFYING, Upstream, _finished, _tasks, bounceban_on, fast_polls)
from test_marketplace_call import platform_on  # noqa: F401
from test_routing import ROUTED, _relay_by_provider, enrichment_on  # noqa: F401

SECRET = "test-hmac-secret"


@pytest.fixture
def events_on(monkeypatch):
    """Events on, with the POST captured instead of sent."""
    monkeypatch.setenv("TREG_CALL_EVENTS_URL", "https://stream.example.invalid")
    monkeypatch.setenv("TREG_CALL_EVENTS_HMAC_SECRET", SECRET)
    get_settings.cache_clear()
    sink.reset()
    sent: list[dict] = []

    async def capture(batch, **_):
        sent.extend(json.loads(b"[" + b",".join(batch) + b"]"))
        sink.counters["sent"] += len(batch)

    monkeypatch.setattr(sink, "_post", capture)
    yield sent
    sink.reset()
    get_settings.cache_clear()


async def _flushed(sent: list[dict]) -> list[dict]:
    await sink.drain()
    return sent


# ---- the sink ----------------------------------------------------------------------------------

async def test_off_by_default_and_a_call_queues_nothing(clients: AsyncClient, enrichment_on, monkeypatch):
    get_settings.cache_clear()
    sink.reset()
    assert get_settings().call_events_url == "" and not sink.enabled()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": "a@sample.example", "score": 96}})]}, []))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "A Person", "domain": "sample.example"})
    assert r.status_code == 200, r.text
    assert len(sink._queue) == 0 and sink.counters["queued"] == 0


async def test_overflow_drops_and_counts_without_blocking(events_on, monkeypatch):
    monkeypatch.setattr(sink, "_MAX_PENDING", 3)
    started = time.perf_counter()
    for i in range(50):
        sink.emit({"call_ref": f"c{i}"})
    assert time.perf_counter() - started < 0.05  # synchronous, no I/O
    assert len(sink._queue) == 3 and sink.counters["dropped_overflow"] == 47
    events = await _flushed(events_on)
    assert [e["call_ref"] for e in events] == ["c0", "c1", "c2"]


async def test_a_body_budget_keeps_the_event_and_drops_its_builder(events_on, monkeypatch):
    monkeypatch.setattr(sink, "_MAX_RETAINED_BYTES", 10)
    sink.emit({"call_ref": "big"}, lambda: {"extra": 1}, retained_bytes=11)
    sink.emit({"call_ref": "small"}, lambda: {"extra": 2}, retained_bytes=5)
    events = await _flushed(events_on)
    assert events == [{"call_ref": "big"}, {"call_ref": "small", "extra": 2}]
    assert sink.counters["unbuilt"] == 1 and sink._retained == 0


async def test_a_failing_builder_still_sends_the_generic_columns(events_on):
    def broken():
        raise RuntimeError("bad body")
    sink.emit({"call_ref": "x", "status_code": 200}, broken)
    assert await _flushed(events_on) == [{"call_ref": "x", "status_code": 200}]
    assert sink.counters["build_failed"] == 1


async def test_a_serialized_batch_carries_no_nulls(events_on):
    # A null in a `json` column is stored as the string 'null'; an omitted one reads as NULL.
    sink.emit({"call_ref": "x", "hit": None, "answer_values": None},
              lambda: {"input_meta": None, "verdict": "valid"})
    batch = sink._next_batch()
    assert [json.loads(line) for line in batch] == [{"call_ref": "x", "verdict": "valid"}]
    assert all(b"null" not in line for line in batch)


async def test_a_failed_post_drops_the_batch_and_never_raises(monkeypatch):
    monkeypatch.setenv("TREG_CALL_EVENTS_URL", "https://stream.example.invalid")
    monkeypatch.setenv("TREG_CALL_EVENTS_HMAC_SECRET", SECRET)
    monkeypatch.setenv("TREG_CALL_EVENTS_TOKEN", "stream-token")
    get_settings.cache_clear()
    sink.reset()
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.headers.get("authorization"), json.loads(request.content)))
        return httpx.Response(503)

    real = httpx.AsyncClient
    monkeypatch.setattr(sink.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    try:
        sink.emit({"call_ref": "a"})
        sink.emit({"call_ref": "b"})
        await sink.drain()
        assert seen == [("Bearer stream-token", [{"call_ref": "a"}, {"call_ref": "b"}])]
        assert sink.counters["dropped_failed"] == 2 and sink.counters["sent"] == 0
    finally:
        sink.reset()
        get_settings.cache_clear()


@pytest.fixture
def scripted_stream(monkeypatch):
    """Events on, the real `_post`, and a stream that answers from a script (a status code, or
    an exception to raise). Retries wait no time."""
    monkeypatch.setenv("TREG_CALL_EVENTS_URL", "https://stream.example.invalid")
    monkeypatch.setenv("TREG_CALL_EVENTS_HMAC_SECRET", SECRET)
    monkeypatch.setattr(sink, "_RETRY_DELAYS_S", (0.0, 0.0))
    get_settings.cache_clear()
    sink.reset()
    script: list = []
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        step = script.pop(0) if script else 200
        if isinstance(step, Exception):
            raise step
        return httpx.Response(step)

    real = httpx.AsyncClient
    monkeypatch.setattr(sink.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    yield script, seen
    sink.reset()
    get_settings.cache_clear()


async def test_a_transient_failure_is_retried_until_it_lands(scripted_stream):
    script, seen = scripted_stream
    script.extend([503, httpx.ReadTimeout("slow")])   # then 200
    sink.emit({"call_ref": "a"})
    await sink._flush_all()
    assert seen == [[{"call_ref": "a"}]] * 3
    assert sink.counters["sent"] == 1 and sink.counters["retried"] == 2
    assert sink.counters["dropped_failed"] == 0


async def test_a_refusal_is_not_retried(scripted_stream):
    script, seen = scripted_stream
    script.append(400)
    sink.emit({"call_ref": "a"})
    await sink._flush_all()
    assert len(seen) == 1 and sink.counters["dropped_failed"] == 1 and sink.counters["retried"] == 0


async def test_an_outage_costs_one_try_per_batch_until_a_batch_lands(scripted_stream, monkeypatch):
    script, seen = scripted_stream
    monkeypatch.setattr(sink, "_BATCH_MAX", 1)
    script.extend([503, 503, 503, 503, 503])          # batch a: 3 tries; b: 1; c: 1
    for ref in "abc":
        sink.emit({"call_ref": ref})
    await sink._flush_all()
    assert [b[0]["call_ref"] for b in seen] == ["a", "a", "a", "b", "c"]
    assert sink.counters["dropped_failed"] == 3
    sink.emit({"call_ref": "d"})                      # the stream is back
    script.extend([503])                              # d: one try (still marked unhealthy), dropped
    sink.emit({"call_ref": "e"})                      # e lands and restores retries
    await sink._flush_all()
    assert sink.counters["sent"] == 1
    script.extend([503])
    sink.emit({"call_ref": "f"})                      # f gets its retries again
    await sink._flush_all()
    assert sink.counters["sent"] == 2 and [b[0]["call_ref"] for b in seen][-2:] == ["f", "f"]


async def test_drain_never_retries(scripted_stream):
    script, seen = scripted_stream
    script.append(503)
    sink.emit({"call_ref": "a"})
    await sink.drain()
    assert len(seen) == 1 and sink.counters["dropped_failed"] == 1


async def test_counters_reach_the_log(scripted_stream, caplog, monkeypatch):
    # A freshly booted host: monotonic time is still smaller than the interval.
    monkeypatch.setattr(sink.time, "monotonic", lambda: 5.0)
    sink.emit({"call_ref": "a"})
    with caplog.at_level("INFO", logger="treg.call_events"):
        await sink._flush_all()
    assert "call events stats:" in caplog.text and "sent=1" in caplog.text
    caplog.clear()
    with caplog.at_level("INFO", logger="treg.call_events"):
        sink.emit({"call_ref": "b"})
        await sink._flush_all()                       # within the interval: no second line
    assert "call events stats:" not in caplog.text


async def test_the_flusher_sends_on_its_own(events_on, monkeypatch):
    monkeypatch.setattr(sink, "_FLUSH_INTERVAL_S", 0.01)
    sink.emit({"call_ref": "auto"})
    for _ in range(100):
        if events_on:
            break
        await asyncio.sleep(0.01)
    assert events_on == [{"call_ref": "auto"}]


# ---- the columns ---------------------------------------------------------------------------------

def test_one_spelling_per_subject():
    assert ev.normalize("email", " A.Person@Sample.EXAMPLE ") == "a.person@sample.example"
    assert ev.normalize("domain", "https://www.Sample.example/about") == "sample.example"
    assert (ev.normalize("linkedin_url", "https://www.linkedin.com/in/APerson/")
            == ev.normalize("linkedin_handle", "aperson") == "aperson")
    assert ev.normalize("full_name", "  A   Person ") == "a person"
    assert ev.domain_class("gmail.com") == "webmail"
    assert ev.domain_class("cs.mit.edu") == "edu" and ev.domain_class("ox.ac.uk") == "edu"
    assert ev.domain_class("nasa.gov") == "gov" and ev.domain_class("sample.example") == "company"


def test_derived_names_give_one_input_id():
    contract = catalog_store.load().contracts["people.email.find"]
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("TREG_CALL_EVENTS_HMAC_SECRET", SECRET)
        get_settings.cache_clear()
        a, _ = ev.input_columns(contract, {"full_name": "A Person", "domain": "sample.example"})
        b, _ = ev.input_columns(contract, {"first_name": "a", "last_name": "PERSON",
                                           "domain": "www.sample.example"})
    get_settings.cache_clear()
    assert a["input_id"] == b["input_id"] and a["input_type"] == "domain+full_name"
    assert a["input_meta"] == {"domain_class": "company", "tld": "example"}


def test_ref_parts():
    assert ev.ref_parts("abc") == (None, None, None)
    assert ev.ref_parts("abc:r2") == ("abc", "r2", 2)
    assert ev.ref_parts("abc:v") == ("abc", "v", None)
    assert ev.ref_parts("abc:r0:p3") == ("abc:r0", "p3", None)


def test_an_unknown_analytics_mode_fails_the_contract():
    with pytest.raises(ValueError, match="analytics"):
        C.parse_contracts({"contracts": {"x.find": {
            "identity": [{"email": "str"}], "output": {"email": {"type": "str", "analytics": "raw"}}}}})
    with pytest.raises(ValueError, match="analytics"):
        C.parse_contracts({"contracts": {"x.find": {
            "identity": [{"email": "str"}], "filters": {"q": {"type": "str", "analytics": "key"}},
            "output": {"email": {"type": "str"}}}}})


def test_the_public_contract_does_not_carry_the_event_mapping():
    import inspect
    from treg.routers import catalog as catalog_router
    assert '!= "analytics"' in inspect.getsource(catalog_router)


# ---- through the call path -------------------------------------------------------------------------

def _no_pii(events: list[dict], *values: str) -> None:
    text = json.dumps(events).lower()
    for v in values:
        assert v.lower() not in text, v


async def test_find_then_verify_join_on_the_email_hash(clients: AsyncClient, enrichment_on,
                                                       events_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": "A.Person@Sample.example", "score": 96,
                                   "verification": {"status": "accept_all"}}}),
                   (200, {"data": {"email": "a.person@sample.example", "score": 92,
                                   "verification": {"status": "valid"}}}),
                   (200, {"data": {"email": {"status": "accept_all", "result": "risky",
                                             "score": 50, "accept_all": True}}})]}, []))
    routed = await clients.post(f"/call/{ROUTED}", json={"first_name": "A", "last_name": "Person",
                                                         "domain": "sample.example"})
    assert routed.status_code == 200, routed.text
    parent_ref = routed.headers["x-treg-call-id"]
    direct = await clients.get("/call/tomba.people.email.find",
                               params={"full_name": "A Person", "domain": "sample.example"})
    assert direct.status_code == 200, direct.text
    checked = await clients.get("/call/tomba.people.email.verify",
                                params={"email": "a.person@SAMPLE.example"})
    assert checked.status_code == 200, checked.text
    events = await _flushed(events_on)
    _no_pii(events, "sample.example", "a person", "a.person", "person@")

    by_ref = {e["call_ref"]: e for e in events}
    parent, child = by_ref[parent_ref], by_ref[f"{parent_ref}:r0"]
    assert set(parent) <= set(ev.COLUMNS) and None not in parent.values()
    assert {"schema_version", "event_id", "event_time", "event_kind"} <= set(parent)
    assert parent["endpoint_id"] == ROUTED and parent["credential_tier"] == "routed"
    assert parent["served_by"] == "tomba.people.email.find" and parent["tried_count"] == 1
    assert parent["hit"] is True and parent.get("step") is None
    assert child["parent_ref"] == parent_ref and child["step"] == "r0"
    assert child["waterfall_position"] == 0 and child["hit"] is True
    assert child["verdict"] == "unverified" and child["capability"] == "people.email.find"
    assert len(child["adapter_fingerprint"]) == 12
    assert child["answer_values"] == {"confidence": "0.96", "verified": "false"}
    assert child["answer_flags"] == {"is_role": False, "free_mail": False, "domain_match": True}

    [verify] = [e for e in events if e["endpoint_id"] == "tomba.people.email.verify"]
    [found] = [e for e in events if e["endpoint_id"] == "tomba.people.email.find"
               and e["call_ref"] == direct.headers["x-treg-call-id"]]
    # The routed parent, its child and the direct call asked one question.
    assert parent["input_id"] == child["input_id"] == found["input_id"]
    assert parent["input_type"] == "domain+full_name"
    # A find's answer joins the verify that checked it.
    email_key = verify["input_keys"]["email"]
    assert parent["answer_keys"]["email"] == child["answer_keys"]["email"] == email_key
    assert found["answer_keys"]["email"] == email_key
    assert verify["input_type"] == "email"
    assert verify["input_meta"] == {"domain_class": "company", "tld": "example"}
    assert verify["answer_values"] == {"valid": "false", "status": "accept_all", "score": "50"}
    assert verify["verdict"] == "catch_all" and verify.get("answer_keys") is None


async def test_an_opted_out_team_sends_no_answer(clients: AsyncClient, enrichment_on, events_on,
                                                 monkeypatch):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    r = await clients.patch(f"/orgs/{org_id}/settings", json={"archive": False})
    assert r.status_code == 200, r.text
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": "a@sample.example", "score": 96}})]}, []))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "A Person", "domain": "sample.example"})
    assert r.status_code == 200, r.text
    events = await _flushed(events_on)
    assert len(events) == 2
    for e in events:
        assert e["input_id"] and e["hit"] is True
        assert not {"answer_keys", "answer_values", "answer_flags"} & set(e)


async def test_a_refused_catalog_call_is_one_event(clients: AsyncClient, events_on):
    r = await clients.get("/call/tomba.people.email.verify", params={"email": "x@sample.example"})
    assert r.status_code >= 400
    [e] = await _flushed(events_on)
    assert e["endpoint_id"] == "tomba.people.email.verify" and e["refused_by"]
    assert e["status_code"] == r.status_code and e.get("input_id") is None


# ---- asynchronous endpoints --------------------------------------------------------------------------

async def test_a_polled_answer_is_one_terminal_event_on_the_submission_ref(
        clients: AsyncClient, monkeypatch, bounceban_on, events_on):
    monkeypatch.setattr(call_service, "relay", Upstream(VERIFYING, [VERIFYING, _finished("deliverable")]))
    submitted = await clients.get(f"/call/{VERIFY}", params={"email": EMAIL})
    call_ref = submitted.headers["x-treg-call-id"]
    for _ in range(3):  # still verifying, done, and a late reread
        assert (await clients.get(f"/call/{STATUS}", params={"id": "bb-task-1"})).status_code == 200
    events = await _flushed(events_on)
    mine = [e for e in events if e["call_ref"] == call_ref]
    assert [e["event_kind"] for e in mine] == ["async_submit", "async_terminal"]
    submit, terminal = mine
    assert submit.get("hit") is None and submit.get("answer_values") is None
    assert submit["input_keys"] == {"email": ev.keyed(EMAIL.lower())}
    assert terminal["hit"] is True and terminal["verdict"] == "valid"
    assert terminal["async_outcome"] == "success" and terminal["endpoint_id"] == VERIFY
    assert terminal["answer_values"]["status"] == "deliverable"
    [task] = await _tasks()
    assert terminal["cost_charged_micro"] == task.settled_micro > 0
    assert submit.get("cost_charged_micro") is None   # the submission's charge is not settled yet
    # The polls are calls of their own, never a second terminal event.
    assert sum(e["event_kind"] == "async_terminal" for e in events) == 1
    _no_pii(events, EMAIL)


async def test_the_worker_emits_the_terminal_event_of_a_routed_child(
        clients: AsyncClient, monkeypatch, bounceban_on, fast_polls, events_on):
    monkeypatch.setattr(call_service, "relay", Upstream(VERIFYING))
    monkeypatch.setattr(call_route, "ROUTED_ASYNC_WAIT_SECONDS", 0.05)
    result = await clients.post("/call/treg.people.email.verify",
                                headers={"X-Treg-Route-Prefer": "bounceban"}, json={"email": EMAIL})
    assert result.status_code == 202, result.text
    parent_ref = result.headers["x-treg-call-id"]
    [task] = await _tasks()
    from treg.infra.db import session_maker
    from treg.models import AsyncTaskRecord
    async with session_maker() as db:
        row = await db.get(AsyncTaskRecord, task.call_id)
        row.next_check_at = row.created_at
        await db.commit()

    async def finished_poll(row, client):
        return 200, json.dumps(_finished("deliverable")).encode()

    monkeypatch.setattr(async_task_app, "_poll", finished_poll)
    assert (await async_task_app.settle_due()).settled == 1
    assert (await async_task_app.settle_due()).claimed == 0
    events = await _flushed(events_on)
    [terminal] = [e for e in events if e["event_kind"] == "async_terminal"]
    assert terminal["call_ref"] == task.call_id == f"{parent_ref}:r0"
    assert terminal["parent_ref"] == parent_ref and terminal["hit"] is True
    assert terminal["answer_values"]["status"] == "deliverable"
    [parent] = [e for e in events if e["call_ref"] == parent_ref]
    assert parent["status_code"] == 202 and parent.get("hit") is None
    assert parent["served_by"] == VERIFY and parent.get("cost_charged_micro") is None
    submit = next(e for e in events if e["call_ref"] == task.call_id and e["event_kind"] == "async_submit")
    assert submit["input_id"] == parent["input_id"]


async def test_a_routed_identity_counts_against_the_body_budget(
        clients: AsyncClient, enrichment_on, events_on, monkeypatch):
    monkeypatch.setattr(sink, "_MAX_RETAINED_BYTES", 1024)
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": "a@sample.example", "score": 96}})]}, []))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "A Person", "domain": "sample.example",
                                                    "padding": "x" * 8192})
    assert r.status_code == 200, r.text
    assert sink.counters["unbuilt"] >= 1
    events = await _flushed(events_on)
    [parent] = [e for e in events if e["call_ref"] == r.headers["x-treg-call-id"]]
    assert "input_id" not in parent   # generic columns only
    assert parent["served_by"] == "tomba.people.email.find"


async def test_first_and_last_name_sent_directly_join_the_full_name_question(
        clients: AsyncClient, enrichment_on, events_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": "a@sample.example", "score": 96}}),
                   (200, {"data": {"email": "a@sample.example", "score": 96}})]}, []))
    split = await clients.get("/call/tomba.people.email.find",
                              params={"first_name": "A", "last_name": "Person", "domain": "sample.example"})
    joined = await clients.get("/call/tomba.people.email.find",
                               params={"full_name": "a person", "domain": "Sample.example"})
    assert split.status_code == joined.status_code == 200
    events = {e["call_ref"]: e for e in await _flushed(events_on)}
    a, b = events[split.headers["x-treg-call-id"]], events[joined.headers["x-treg-call-id"]]
    assert a["input_id"] and a["input_id"] == b["input_id"]
    assert a["input_type"] == "domain+full_name"


async def test_an_unreadable_opt_out_never_stops_a_release(
        clients: AsyncClient, monkeypatch, bounceban_on, events_on, caplog):
    from datetime import timedelta
    from treg.infra.db import session_maker
    from treg.models import AsyncTaskRecord
    monkeypatch.setattr(call_service, "relay", Upstream(VERIFYING))
    submitted = await clients.get(f"/call/{VERIFY}", params={"email": EMAIL})
    call_ref = submitted.headers["x-treg-call-id"]
    async with session_maker() as db:   # past its window: the worker's next look releases it
        row = await db.get(AsyncTaskRecord, call_ref)
        row.created_at = row.created_at - timedelta(days=2)
        row.next_check_at = row.created_at
        await db.commit()

    # The opt-out read itself fails: the event loses its answers, the release still runs.
    class _Unmapped:
        pass
    monkeypatch.setattr(async_task_app, "Org", _Unmapped)   # db.get on an unmapped class raises
    with caplog.at_level("WARNING", logger="treg.asynctasks"):
        await async_task_app.settle_due()
    assert "could not read the archive opt-out" in caplog.text
    [task] = await _tasks()
    assert task.status == "timed_out" and task.settled_micro == 0
    events = await _flushed(events_on)
    [terminal] = [e for e in events if e["event_kind"] == "async_terminal"]
    assert terminal["call_ref"] == call_ref and terminal["async_outcome"] == "timed_out"
    assert terminal["cost_charged_micro"] == 0 and terminal.get("answer_values") is None
