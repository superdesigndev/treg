"""What a judge reads of a reviewed call's answer: read, absent for good, or unreadable now (left for
a later run), against the real archive tables."""
import json

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import select

from treg import archive, archive_bodies
from treg.application import review_answers
from treg.config import get_settings
from treg.infra.db import session_maker
from treg.models import ArchiveKey, ArchiveSnapshot, CallRecord, CallReview
from test_review_body_pilot import PEOPLE, _add, _org, _record, _review


async def _reviewed(clients, key="k"):
    org = await _org(clients)
    await _add(_review(org, "c", "WRONG"), _record(org, "c", key))
    async with session_maker() as db:
        return (await db.execute(select(CallReview))).scalar_one()


async def _key(*snapshots, answer: bytes | None = None):
    """The call's archive key with these versions of one answer, all under the hash of `answer`
    (the first stored body, else PEOPLE), which the call row points at."""
    answer = answer or next((body for _, body, _ in snapshots if body is not None), PEOPLE.encode())
    content_hash = archive.content_hash(answer)
    async with session_maker() as db:
        key = ArchiveKey(key_hash="k", endpoint_id="apollo.people.search", req_method="POST", req_url="u")
        db.add(key)
        await db.flush()
        for version, body, storage in snapshots:
            db.add(ArchiveSnapshot(key_id=key.id, version=version, content_hash=content_hash, body=body,
                                   size_bytes=len(PEOPLE), body_storage=storage))
        for record in (await db.execute(select(CallRecord))).scalars():
            record.archive_content_hash = content_hash
        await db.commit()


async def test_an_older_version_that_kept_the_bytes_is_read(clients):
    # The newest version with this hash is hash-only; an older one kept the same bytes.
    review = await _reviewed(clients)
    await _key((1, PEOPLE.encode(), "db"), (2, None, None))
    answer = await review_answers.answer_of(review)
    assert answer.state == "read" and answer.found["response"]["body_text"] == PEOPLE


async def test_an_answer_quoting_treg_key_reaches_the_judge_masked(clients, monkeypatch):
    """A copy kept before the archive masked answers may still quote treg's key; the judge is a
    model outside treg, so it reads the answer masked like every other reader of the archive."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_APOLLO", "PLATFORM-APOLLO-KEY-9f2c71d0")
    get_settings.cache_clear()
    try:
        review = await _reviewed(clients)
        async with session_maker() as db:
            (await db.execute(select(CallRecord))).scalar_one().provider = "apollo"
            await db.commit()
        quoting = json.dumps({"people": [], "next": "https://api.apollo.io/v1/people?api_key=PLATFORM-APOLLO-KEY-9f2c71d0"})
        await _key((1, quoting.encode(), "db"))
        answer = await review_answers.answer_of(review)
        assert answer.state == "read"
        assert "PLATFORM-APOLLO-KEY" not in answer.found["response"]["body_text"]
        assert "api_key=***" in answer.found["response"]["body_text"]
    finally:
        get_settings.cache_clear()


async def test_no_bytes_now_is_tried_again_not_final(clients):
    # Kept as a hash only, or not written yet (the archive records after the answer): neither ends
    # the upgrade at once; the judge backs off and keeps the reason codes after its last try.
    review = await _reviewed(clients)
    assert (await review_answers.answer_of(review)).state == "unreadable"   # no key yet
    await _key((1, None, None))
    assert (await review_answers.answer_of(review)).state == "unreadable"   # hash only


async def test_a_failed_object_read_waits_even_for_a_legacy_row(clients, monkeypatch):
    # A legacy row whose database copy was pruned after a backfill to object storage: a failed
    # read (not a missing object) is not a gone answer.
    review = await _reviewed(clients)
    await _key((1, None, None))

    async def read(pointer, path, *, diagnostics=None, session_factory=None):
        diagnostics.update(cache_body_fallback_reason="timeout")
        return None
    monkeypatch.setattr(archive_bodies, "read", read)
    assert (await review_answers.answer_of(review)).state == "unreadable"


async def test_object_storage_in_use_but_not_configured_waits(clients, monkeypatch):
    review = await _reviewed(clients)
    await _key((1, None, "r2"))
    monkeypatch.setattr(archive_bodies, "uses_r2", lambda: True)
    assert (await review_answers.answer_of(review)).state == "unreadable"


async def test_a_database_disconnect_or_a_corrupt_read_is_tried_again(clients, monkeypatch):
    # Neither is a gone answer; the judge backs off and gives up after a few tries.
    review = await _reviewed(clients)

    async def disconnect(*args, **kwargs):
        raise OperationalError("select", {}, ConnectionError("server closed the connection"))
    monkeypatch.setattr(archive, "resolve_result", disconnect)
    assert (await review_answers.answer_of(review)).state == "unreadable"

    async def corrupt(*args, **kwargs):
        raise ValueError("Error -3 while decompressing data")
    monkeypatch.setattr(archive, "resolve_result", corrupt)
    assert (await review_answers.answer_of(review)).state == "unreadable"


async def test_a_call_through_table_is_judged_from_the_reason(clients):
    # /table reshapes the answer; its call row says so.
    review = await _reviewed(clients)
    await _key((1, PEOPLE.encode(), "db"))
    async with session_maker() as db:
        record = (await db.execute(select(CallRecord))).scalar_one()
        record.surface = "table"
        await db.commit()
    assert (await review_answers.answer_of(review)).state == "absent"


async def test_a_call_from_before_surfaces_were_recorded_can_be_kept_out(clients, monkeypatch):
    from datetime import datetime, timedelta, timezone

    review = await _reviewed(clients)
    await _key((1, PEOPLE.encode(), "db"))
    assert (await review_answers.answer_of(review)).state == "read"
    monkeypatch.setattr(get_settings(), "review_answer_surface_since", datetime.now() + timedelta(days=1),
                        raising=False)
    assert (await review_answers.answer_of(review)).state == "absent"
    # A timezone-qualified setting (`...Z`) compares with the naive UTC call rows too.
    aware = datetime.now(timezone.utc)
    monkeypatch.setattr(get_settings(), "review_answer_surface_since", aware - timedelta(days=1), raising=False)
    assert (await review_answers.answer_of(review)).state == "read"
    monkeypatch.setattr(get_settings(), "review_answer_surface_since", aware + timedelta(days=1), raising=False)
    assert (await review_answers.answer_of(review)).state == "absent"


async def test_a_missing_object_falls_back_to_a_database_copy_and_otherwise_is_gone(clients, monkeypatch):
    # The same bytes kept in the database by an older version and in object storage by a newer one:
    # an object read that finds it missing moves on to the database copy.
    review = await _reviewed(clients)
    await _key((1, PEOPLE.encode(), "db"), (2, None, "r2"))
    real = archive_bodies.read

    async def read(pointer, path, *, diagnostics=None, session_factory=None):
        if pointer.storage == "r2":
            diagnostics.update(cache_body_fallback_reason="not_found")
            return None
        return await real(pointer, path, diagnostics=diagnostics, session_factory=session_factory)
    monkeypatch.setattr(archive_bodies, "read", read)
    assert (await review_answers.answer_of(review)).state == "read"


@pytest.mark.parametrize("body", ['{"a":1}'.encode("utf-16le")])
async def test_text_in_another_encoding_is_absent(clients, body):
    review = await _reviewed(clients)
    await _key((1, body, "db"))
    assert (await review_answers.answer_of(review)).state == "absent"


async def test_the_view_shows_only_what_this_delivery_is_known_to_have_had(clients):
    review = await _reviewed(clients)
    await _key((1, PEOPLE.encode(), "db"))
    found = (await review_answers.answer_of(review)).found
    item = review_answers.view(0, review, found, "people.search", 500)
    assert set(item["response"]) == {"status_code", "body"}


async def test_a_database_copy_that_no_longer_matches_its_hash_waits(clients):
    # Valid JSON changed in place under the same hash: never shown to the judge.
    review = await _reviewed(clients)
    damaged = PEOPLE.replace("a", "b", 1)
    assert damaged != PEOPLE
    await _key((1, damaged.encode(), "db"), answer=PEOPLE.encode())
    assert (await review_answers.answer_of(review)).state == "unreadable"


async def test_the_judge_reads_the_answer_the_caller_got_byte_for_byte(clients):
    # Within the budget the body is the delivered text; numbers are never rounded or overflowed.
    review = await _reviewed(clients)
    delivered = '{"price": 123.1234567890123456789, "big": 1e400, "n": 12345678901234567890123}'
    await _key((1, delivered.encode(), "db"))
    found = (await review_answers.answer_of(review)).found
    item = review_answers.view(0, review, found, "people.search", 4000)
    assert item["response"]["body"] == delivered


def test_a_shortened_answer_keeps_the_numbers_it_shows_as_written():
    import json

    from treg.domain.feedback import body_view

    text = json.dumps({"items": [{"price": "x"} for _ in range(50)]}).replace('"x"', "123.1234567890123456789")
    out = body_view.shrink(text.replace("123.1234567890123456789", "1e400", 1), 300)
    assert len(out) <= 300 and out.count("1e400") == 1 and "123.1234567890123456789" in out
    assert "Infinity" not in out and "123.12345678901235" not in out


def test_the_request_shows_its_query_sorted_since_this_call_s_order_is_not_on_file():
    # The archive keys a question by its sorted query: the URL it kept is the first call's.
    from treg.domain.feedback import review_codes

    assert (review_answers.request_url("https://v/r?products=200&products=100&a=1")
            == "https://v/r?a=1&products=100&products=200")
    assert review_answers.request_url("https://v/r") == "https://v/r"
    assert "order the agent sent" in review_codes.body_code_question(0)["instructions"]


@pytest.mark.parametrize("budget", [4000, 120])
def test_json_after_a_byte_order_mark_is_json(budget):
    from treg.domain.feedback import body_view

    text = "﻿" + json.dumps({"note": "price < 5 or > 50", "items": [{"n": i} for i in range(30)]})
    out = body_view.shrink(text, budget)
    if len(text) <= budget:
        assert out == text
    else:
        assert len(out) <= budget and json.loads(out)["note"] == "price < 5 or > 50"


def test_a_request_body_is_shown_only_when_it_is_provably_this_call_s():
    # A fractional number or a repeated key can share an archive key with another body.
    assert review_answers.request_body('{"min": 1234567890123456.7}') is None
    assert review_answers.request_body('{"a": 1, "a": 2}') is None
    # The first call's plain object shares its key with a later body repeating a key.
    assert review_answers.request_body('{"a": 2}') is None
    # A byte order mark does not hide a rounded number: the digest reads JSON after it.
    assert review_answers.request_body('\ufeff{"min": 1234567890123456.7}') is None
    assert review_answers.request_body('\ufeff[1, 2]') == '\ufeff[1, 2]'
    assert review_answers.request_body('[1, "x < y", null]') == '[1, "x < y", null]'
    assert review_answers.request_body("q=1") == "q=1"
    assert review_answers.request_body(None) is None


def test_every_judge_is_told_what_the_request_can_and_cannot_say():
    from treg.application import review_body_audit
    from treg.domain.feedback import review_codes

    assert review_codes.REQUEST_NOTE in review_codes.body_code_question(0)["instructions"]
    item = {"usefulness": "partly", "reason": "r", "capability": "c",
            "request": {"method": "GET", "url": "u", "body": None}, "response": {"status_code": 200, "body": ""}}
    assert review_codes.REQUEST_NOTE in review_body_audit._prompt(item, ("WRONG", None), ("EMPTY", None))
