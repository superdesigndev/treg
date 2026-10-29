"""The replay corpus export (docs/context/architecture/replay.md, "The production corpus"), on
synthetic call rows and archived answers only."""

from __future__ import annotations

import argparse
import asyncio
import json
import stat
from collections import Counter
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import DBAPIError

from tests.fake_object_store import MemoryObjectStore
from treg import archive, bootstrap, worker
from treg.application.call.resolve import QueryValues, price_call
from treg.application.replay import engine, export, records
from treg.config import get_settings
from treg.domain.catalog import store
from treg.infra import db
from treg.infra.db import session_maker
from treg.models import ArchiveKey, ArchiveSnapshot, AsyncTaskRecord, CallRecord, Org
from treg.timeutil import utcnow_naive as now

HUNTER = "hunter.people.email.find"
APOLLO = "apollo.companies.jobs"
CRAWL = "crawl4ai.web.search"
CRAWL_JOB = "crawl4ai.web.crawl"
EXPECTING = "contactout.people.search"
FOUND = b'{"data":{"email":"ann@acme.test","score":97}}'
EMPTY = b'{"data":{"email":null}}'
KEY = "hunter-platform-secret-value"
_orgs = Counter()


@pytest.fixture(autouse=True)
def platform_keys(monkeypatch):
    for provider in ("hunter", "apollo", "crawl4ai", "contactout"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{provider.upper()}", f"{provider}-platform-key-value")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def team(clients):
    return await _org()


async def _org(*, opted_out: bool = False) -> int:
    _orgs["n"] += 1
    async with session_maker() as s:
        org = Org(name="Example team", slug=f"example-team-{_orgs['n']}",
                  archive_opt_out_at=now() if opted_out else None)
        s.add(org)
        await s.commit()
        return org.id


def _url(endpoint_id: str, query: str) -> str:
    ep = store.load().by_id[endpoint_id]
    base = {"hunter": "https://api.hunter.io/v2", "apollo": "https://api.apollo.io/api/v1",
            "crawl4ai": "https://api.crawl4ai.com", "contactout": "https://api.contactout.com"}
    path = ep["path"].replace("{organization_id}", "org%2F42")
    return f"{base[ep['provider']]}{path}" + (f"?{query}" if query else "")


async def _call(org_id, endpoint_id=HUNTER, *, query="domain=acme.test&full_name=Ann", body=FOUND,
                status=200, hit=True, charged=24_000, observed=None, question=None, ago=60,
                tier="platform", cached=False, refused=None, archived=True, kept=True,
                error_request=None, error_response=None, call_ref="call-ref-1",
                method="GET") -> int:
    url = _url(endpoint_id, query)
    async with session_maker() as s:
        row = CallRecord(
            org_id=org_id, user_email="member@example.test", tool_name=endpoint_id, method=method,
            path=url.split("?")[0], status_code=status, endpoint_id=endpoint_id,
            provider=endpoint_id.split(".")[0], credential_tier=tier, cost_estimated_micro=24_000,
            cost_observed_micro=observed, cost_charged_micro=charged, hit=hit, cached=cached,
            refused_by=refused, upstream_ms=None if refused else 120, call_ref=call_ref,
            api_key_name="ci-key", api_key_prefix="treg_live_abcd", params_hash=question or url,
            error_request=error_request, error_response=error_response,
            created_at=now() - timedelta(minutes=ago))
        s.add(row)
        await s.flush()
        if archived and 200 <= status < 300:
            key_hash = archive.cache_key("GET", endpoint_id, url) + (question or "")
            key = (await s.execute(select(ArchiveKey).where(
                ArchiveKey.key_hash == key_hash))).scalar_one_or_none()
            if key is None:
                key = ArchiveKey(key_hash=key_hash, endpoint_id=endpoint_id, req_method="GET",
                                 req_url=url, req_headers={"accept": "application/json"})
                s.add(key)
                await s.flush()
            version = 1 + len((await s.execute(select(ArchiveSnapshot.id).where(
                ArchiveSnapshot.key_id == key.id))).all())
            digest = archive.content_hash(body)
            s.add(ArchiveSnapshot(key_id=key.id, version=version, content_hash=digest,
                                  size_bytes=len(body),
                                  media_type="application/json", body=body if kept else None,
                                  body_storage="db" if kept else None))
            row.archive_key_hash, row.archive_content_hash = key_hash, digest
        await s.commit()
        return row.id


async def _export(tmp_path: Path, endpoints, **kwargs) -> tuple[list[dict], dict, dict]:
    out, manifest = tmp_path / "corpus.jsonl", tmp_path / "manifest.jsonl"
    summary = await export.export(endpoints, out, manifest, session_factory=session_maker, **kwargs)
    corpus = list(records.read(out)) if out.stat().st_size else []
    index = {line["rid"]: line for line in map(json.loads, manifest.read_text().splitlines())}
    return corpus, index, summary


def _ids(corpus, index, cls=None) -> list[int]:
    return [index[r["rid"]]["callrecord_id"] for r in corpus if cls is None or r["class"] == cls]


# ---- selection and classes ---------------------------------------------------------------------

async def test_classes_follow_the_metered_platform_rows_of_the_window(team, tmp_path):
    hit = await _call(team)
    miss = await _call(team, body=EMPTY, hit=False, charged=0, question="empty")
    failed = await _call(team, status=429, hit=None, charged=0, archived=False,
                         error_request="?domain=acme.test", error_response='{"error":"rate"}')
    await _call(team, cached=True, question="cached")
    await _call(team, tier="credential", question="own-key")
    await _call(team, tier="platform-overflow", question="overflow")
    await _call(team, refused="balance", question="refused")
    await _call(team, ago=60 * 24 * 40, question="old")
    await _call(team, status=500, hit=None, charged=0, archived=False, ago=60 * 24 * 15,
                error_response='{"error":"past its retention"}')
    await _call(team, charged=None, question="unmetered")
    corpus, index, summary = await _export(tmp_path, [HUNTER])
    assert {r["class"]: index[r["rid"]]["callrecord_id"] for r in corpus} == {
        "hit": hit, "miss": miss, "non2xx": failed}
    assert summary["records"] == 3
    assert {cls: v["exported"] for cls, v in summary["endpoints"][HUNTER].items()} == {
        "hit": 1, "miss": 1, "non2xx": 1}
    assert {r["source"] for r in corpus} == {"archive", "error_evidence"}


async def test_up_to_n_per_class_distinct_questions_first(team, tmp_path):
    repeats = [await _call(team, question="a", ago=ago) for ago in (1, 2, 3)]
    others = [await _call(team, question=q, ago=ago) for q, ago in (("b", 4), ("c", 5), ("d", 6))]
    corpus, index, summary = await _export(tmp_path, [HUNTER], per_class=3)
    assert _ids(corpus, index) == [repeats[0], others[0], others[1]]
    assert summary["endpoints"][HUNTER]["hit"]["distinct"] == 3

    (tmp_path / "more").mkdir()
    corpus, index, _ = await _export(tmp_path / "more", [HUNTER], per_class=5)
    assert _ids(corpus, index) == [repeats[0], *others, repeats[1]]


def test_endpoint_selection_names_and_providers():
    picked = export.select_endpoints([HUNTER], ["apollo"])
    assert HUNTER in picked and APOLLO in picked
    assert all(e == HUNTER or e.startswith("apollo.") for e in picked)
    with pytest.raises(ValueError, match="not in this checkout's catalog"):
        export.select_endpoints(["nobody.nothing"])
    with pytest.raises(ValueError, match="no priced endpoint"):
        export.select_endpoints([], ["no-such-provider"])


# ---- fidelity ----------------------------------------------------------------------------------

async def test_the_caller_query_is_exact_or_reconstructed_from_the_path_template(team, tmp_path):
    await _call(team)
    await _call(team, APOLLO, query="page=2", body=b'{"organization_job_postings":[]}')
    corpus, _, _ = await _export(tmp_path, [HUNTER, APOLLO])
    by_endpoint = {r["endpoint_id"]: r for r in corpus}
    hunter = by_endpoint[HUNTER]["request"]
    assert hunter["fidelity"] == "exact" and hunter["path_params"] == {}
    assert hunter["query_items"] == [["domain", "acme.test"], ["full_name", "Ann"]]
    assert hunter["accept"] == {"accept": "application/json"}
    apollo = by_endpoint[APOLLO]["request"]
    assert apollo["fidelity"] == "reconstructed"
    assert apollo["path_params"] == {"organization_id": "org/42"}
    assert apollo["query_items"] == [["page", "2"], ["organization_id", "org/42"]]
    for record in corpus:
        line = engine.replay(record)
        assert "error" not in line["reserve"] and line["reserve"]["estimate_micro"] > 0


async def test_a_header_billed_answer_carries_a_synthesized_credit_header(team, tmp_path):
    await _call(team, CRAWL, query="q=treg", body=b'{"results":[{"url":"https://a.test"}]}',
                observed=150, charged=158)
    (record,), _, _ = await _export(tmp_path, [CRAWL])
    assert record["response"]["headers"] == {"x-c4-cost": repr(150 / 1_000_000 / 0.001)}
    assert record["response"]["headers_fidelity"] == "synthesized"
    assert engine.replay(record)["settle"]["observed_micro"] == 150


async def test_failure_evidence_is_a_flagged_snippet_with_its_inlined_headers(team, tmp_path):
    await _call(team, status=429, hit=None, charged=0, archived=False, method="POST",
                error_request='?domain=acme.test&full_name=Ann Lee {"x": 1}',
                error_response='[retry-after=5 www-authenticate=Bearer realm="api" '
                               'x-request-id=req-1] {"error":"rate limited"}')
    (record,), _, _ = await _export(tmp_path, [HUNTER])
    assert record["request"]["fidelity"] == "snippet"
    assert record["request"]["query_items"] == [["domain", "acme.test"], ["full_name", "Ann Lee"]]
    assert records.decode(record["request"]["body_b64"]) == b'{"x": 1}'
    assert record["response"]["fidelity"] == "snippet_redacted_truncated"
    assert record["response"]["headers"] == {
        "retry-after": "5", "www-authenticate": 'Bearer realm="api"', "x-request-id": "req-1"}
    assert records.decode(record["response"]["body_b64"]) == b'{"error":"rate limited"}'
    assert engine.replay(record)["settle"]["billable"] is False


async def test_a_failed_query_keeps_its_spaces_and_lists_each_path_parameter_once(team, tmp_path):
    await _call(team, status=429, hit=None, charged=0, archived=False,
                error_request="?domain=acme.test&full_name=Ann Lee {x}", error_response="{}")
    await _call(team, APOLLO, status=500, hit=None, charged=0, archived=False,
                error_request="?organization_id=org/42&page=2", error_response="{}")
    corpus, _, _ = await _export(tmp_path, [HUNTER, APOLLO])
    by_endpoint = {r["endpoint_id"]: r["request"] for r in corpus}
    assert by_endpoint[HUNTER]["query_items"] == [
        ["domain", "acme.test"], ["full_name", "Ann Lee {x}"]]
    assert by_endpoint[HUNTER]["body_b64"] == ""
    assert by_endpoint[APOLLO]["query_items"] == [["organization_id", "org/42"], ["page", "2"]]


async def test_an_endpoint_without_a_configured_credential_exports_nothing(team, tmp_path,
                                                                           monkeypatch):
    monkeypatch.delenv("TREG_PLATFORM_KEY_HUNTER")
    get_settings.cache_clear()
    await _call(team)
    corpus, _, summary = await _export(tmp_path, [HUNTER])
    assert corpus == []
    assert summary["endpoints"][HUNTER] == {"error": "credential_renderings_unavailable"}
    get_settings.cache_clear()


async def test_two_xx_answers_production_never_kept_are_counted_never_invented(team, tmp_path):
    await _call(team, EXPECTING, query="", hit=False, charged=0, archived=False)
    await _call(team, query="domain=b.test", archived=False, question="shed")
    await _call(team, query="domain=c.test", kept=False, question="hash-only")
    corpus, _, summary = await _export(tmp_path, [EXPECTING, HUNTER])
    assert corpus == []
    assert summary["endpoints"][EXPECTING]["miss"]["dropped"] == {"expect_unavailable": 1}
    assert summary["endpoints"][HUNTER]["hit"]["dropped"] == {"no_body": 1, "not_archived": 1}


async def test_an_async_task_exports_its_terminal_answer_and_frozen_basis(team, tmp_path):
    ep = store.load().by_id[CRAWL_JOB]
    basis = price_call(ep, QueryValues(()), b'{"url":"https://a.test"}', set()).settlement_basis
    done = b'{"status":"done","usage":{"cost":0.15}}'
    async with session_maker() as s:
        for call_id, status, org_id in (("job-1", "settled", team), ("job-2", "released", team),
                                        ("job-3", "settled", await _org(opted_out=True))):
            s.add(AsyncTaskRecord(call_id=call_id, org_id=org_id, provider="crawl4ai",
                                  endpoint_id=CRAWL_JOB, reserved_micro=75_150,
                                  descriptor=ep["async"], settlement_basis=basis,
                                  next_check_at=now(), status=status, settled_micro=150))
            key = ArchiveKey(key_hash=archive.cache_key("GET", CRAWL_JOB, f"treg://asynctasks/{call_id}",
                                                        b"", {}), endpoint_id=CRAWL_JOB)
            if call_id != "job-2":
                s.add(key)
                await s.flush()
                s.add(ArchiveSnapshot(key_id=key.id, content_hash=archive.content_hash(done),
                                      body=done, body_storage="db", size_bytes=len(done)))
        await s.commit()
    submission = await _call(team, CRAWL_JOB, query="", archived=False, call_ref="job-1",
                             question="submission")
    corpus, index, summary = await _export(tmp_path, [CRAWL_JOB])
    (record,) = [r for r in corpus if r["class"] == "async_terminal"]
    assert index[record["rid"]] == {"rid": record["rid"], "callrecord_id": submission,
                                    "async_call_id": "job-1"}
    assert record["async"]["settlement_basis"] == basis and record["async"]["status"] == "settled"
    assert record["request"]["fidelity"] == "unavailable"
    assert summary["endpoints"][CRAWL_JOB]["async_terminal"]["dropped"] == {"no_terminal_evidence": 1}
    line = engine.replay(record)
    assert line["async"]["outcome"] == "success" and line["async"]["settled_micro"] == 150


def test_a_table_basis_gives_back_the_request_it_priced():
    ep = store.load().by_id[CRAWL_JOB]
    asked = {"queryParams": {"a": ["1", "2"]}, "pathParams": {"id": 7}, "body": {"maxPages": 2}}
    task = AsyncTaskRecord(call_id="t", org_id=1, provider="crawl4ai", endpoint_id=CRAWL_JOB,
                           reserved_micro=1, descriptor={}, next_check_at=now(), created_at=now(),
                           settlement_basis={"amount": {"kind": "table", "request": asked}})
    record, why = export._terminal_record(ep, task, 200, b"{}", [])
    assert why is None and record["request"]["fidelity"] == "reconstructed"
    assert record["request"]["query_items"] == [["a", "1"], ["a", "2"], ["id", "7"]]
    assert record["request"]["path_params"] == {"id": "7"}
    assert json.loads(records.decode(record["request"]["body_b64"])) == {"maxPages": 2}


async def test_a_class_query_that_times_out_is_retried_on_half_its_window(team, tmp_path):
    windows = []

    class Canceled(Exception):
        sqlstate = "57014"

    class Session:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def execute(self, statement):
            windows.append(statement)
            raise DBAPIError("SELECT", {}, Canceled())

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    run = export._Run(now(), 8, 20, 1, Session, None, None)
    rows, window = await export._bounded(run, lambda since: (run.until - since).days)
    assert (rows, window) == ([], None) and windows == [8, 4, 2, 1]
    (tmp_path / "x").mkdir()
    await _call(team)
    _, _, summary = await _export(tmp_path / "x", [HUNTER], days=8)
    assert summary["endpoints"][HUNTER]["hit"]["window_days"] == 8


# ---- privacy -----------------------------------------------------------------------------------

async def test_opted_out_and_deleted_teams_are_never_exported(team, tmp_path):
    kept = await _call(team)
    await _call(await _org(opted_out=True), question="opted-out")
    await _call(None, question="team-gone")
    await _call(await _org(opted_out=True), status=500, hit=None, charged=0, archived=False,
                error_response='{"error":"down"}')
    corpus, index, _ = await _export(tmp_path, [HUNTER])
    assert _ids(corpus, index) == [kept]


async def test_a_record_that_echoes_treg_credential_is_dropped(team, tmp_path, monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_HUNTER", KEY)
    get_settings.cache_clear()
    kept = await _call(team)
    await _call(team, body=b'{"echo":"X-API-KEY: ' + KEY.upper().encode() + b'"}', question="b")
    await _call(team, query="domain=" + KEY.replace("-", "%2D"), question="c")
    await _call(team, status=401, hit=None, charged=0, archived=False,
                error_response='{"received":"' + KEY + '"}')
    corpus, index, summary = await _export(tmp_path, [HUNTER])
    assert _ids(corpus, index) == [kept]
    assert summary["endpoints"][HUNTER]["hit"]["dropped"] == {"credential_echo": 2}
    assert summary["endpoints"][HUNTER]["non2xx"]["dropped"] == {"credential_echo": 1}
    get_settings.cache_clear()


async def test_records_strip_identity_and_the_files_are_private(team, tmp_path):
    await _call(team, call_ref="call-ref-secret")
    await _call(team, status=500, hit=None, charged=0, archived=False, call_ref="call-ref-secret",
                error_response='{"error":"down"}')
    corpus, _, summary = await _export(tmp_path, [HUNTER])
    text = (tmp_path / "corpus.jsonl").read_text()
    for value in ("member@example.test", "call-ref-secret", "ci-key", "treg_live_abcd",
                  "example-team", "org_id", "user_email"):
        assert value not in text
    assert all(set(r) == {"rid", "endpoint_id", "class", "captured_at", "source", "request",
                          "response", "recorded", "async"} for r in corpus)
    for name in ("corpus.jsonl", "manifest.jsonl"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600
    assert "ann@acme.test" not in json.dumps(summary)


async def test_existing_files_are_never_overwritten_and_a_failed_run_leaves_nothing(
        team, tmp_path, monkeypatch):
    await _call(team)
    (tmp_path / "manifest.jsonl").write_text("keep")
    with pytest.raises(FileExistsError):
        await _export(tmp_path, [HUNTER])
    assert not (tmp_path / "corpus.jsonl").exists()
    assert (tmp_path / "manifest.jsonl").read_text() == "keep"

    async def broken(*args):
        raise RuntimeError("store down")
    monkeypatch.setattr(export, "_answers", broken)
    with pytest.raises(RuntimeError):
        await export.export([HUNTER], tmp_path / "a.jsonl", tmp_path / "b.jsonl",
                            session_factory=session_maker)
    assert not (tmp_path / "a.jsonl").exists() and not (tmp_path / "b.jsonl").exists()


async def test_no_database_connection_is_held_while_bodies_are_read(team, tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "archive_body_read_result", "r2-first")
    monkeypatch.setattr(settings, "archive_body_read_terminal", "r2-first")
    objects = MemoryObjectStore()
    objects.objects[archive.content_hash(FOUND)] = FOUND
    held = Counter()

    def checkout(connection, record, proxy):
        record.info["owner"] = asyncio.current_task()
        held[record.info["owner"]] += 1

    def checkin(connection, record):
        held[record.info.pop("owner", None)] -= 1

    def check_io():
        assert held[asyncio.current_task()] == 0, "object I/O holds a DB connection"

    objects.check_io = check_io
    engines = {engine_.sync_engine for engine_ in db._engines}
    for engine_ in engines:
        event.listen(engine_, "checkout", checkout)
        event.listen(engine_, "checkin", checkin)
    bootstrap.configure_archive_object_store(objects)
    try:
        await _call(team, kept=False)
        (record,), _, _ = await _export(tmp_path, [HUNTER])
    finally:
        bootstrap.configure_archive_object_store(None)
        for engine_ in engines:
            event.remove(engine_, "checkout", checkout)
            event.remove(engine_, "checkin", checkin)
    assert objects.get_calls == 1 and records.decode(record["response"]["body_b64"]) == FOUND


async def test_the_worker_command_refuses_unknown_names_and_prints_counts(team, tmp_path, capsys):
    await _call(team)
    args = argparse.Namespace(endpoints="nobody.nothing", provider=None, per_class=20, days=30,
                              statement_timeout_s=20, out=tmp_path / "c", manifest=tmp_path / "m")
    assert await worker._admin_export_replay_corpus(args) == 2
    assert not (tmp_path / "c").exists()
    capsys.readouterr()
    args.endpoints = HUNTER
    assert await worker._admin_export_replay_corpus(args) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["records"] == 1 and printed["endpoints"][HUNTER]["hit"]["exported"] == 1
    assert "ann@acme.test" not in json.dumps(printed)
