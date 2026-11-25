"""What a judge reads of a reviewed call's answer: read, absent for good, or unreadable now (left for
a later run), against the real archive tables."""
import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import select

from treg import archive, archive_bodies
from treg.application import review_answers
from treg.config import get_settings
from treg.infra.db import session_maker
from treg.models import ArchiveKey, ArchiveSnapshot, CallReview
from test_review_body_pilot import PEOPLE, _add, _org, _record, _review


async def _reviewed(clients, key="k"):
    org = await _org(clients)
    await _add(_review(org, "c", "WRONG"), _record(org, "c", key))
    async with session_maker() as db:
        return (await db.execute(select(CallReview))).scalar_one()


async def _key(*snapshots):
    async with session_maker() as db:
        key = ArchiveKey(key_hash="k", endpoint_id="apollo.people.search", req_method="POST", req_url="u")
        db.add(key)
        await db.flush()
        for version, body, storage in snapshots:
            db.add(ArchiveSnapshot(key_id=key.id, version=version, content_hash="c-k", body=body,
                                   size_bytes=len(PEOPLE), body_storage=storage))
        await db.commit()


async def test_an_older_version_that_kept_the_bytes_is_read(clients):
    # The newest version with this hash is hash-only; an older one kept the same bytes.
    review = await _reviewed(clients)
    await _key((1, PEOPLE.encode(), "db"), (2, None, None))
    answer = await review_answers.answer_of(review)
    assert answer.state == "read" and answer.found["response"]["body_text"] == PEOPLE


async def test_a_hash_only_answer_is_absent_for_good(clients):
    review = await _reviewed(clients)
    await _key((1, None, None))
    assert (await review_answers.answer_of(review)).state == "absent"


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
    monkeypatch.setattr(archive_bodies, "has_store", lambda: False)
    assert (await review_answers.answer_of(review)).state == "unreadable"


async def test_a_database_disconnect_waits_and_a_corrupt_read_does_not(clients, monkeypatch):
    review = await _reviewed(clients)

    async def disconnect(*args, **kwargs):
        raise OperationalError("select", {}, ConnectionError("server closed the connection"))
    monkeypatch.setattr(archive, "resolve_result", disconnect)
    assert (await review_answers.answer_of(review)).state == "unreadable"

    async def corrupt(*args, **kwargs):
        raise ValueError("Error -3 while decompressing data")
    monkeypatch.setattr(archive, "resolve_result", corrupt)
    answer = await review_answers.answer_of(review)
    assert (answer.state, answer.error) == ("absent", True)


async def test_a_table_caller_is_judged_from_the_reason(clients, monkeypatch):
    # /table reshapes the answer and the call row does not say which surface was used.
    review = await _reviewed(clients)
    await _key((1, PEOPLE.encode(), "db"))
    monkeypatch.setattr(get_settings(), "table_enabled", True, raising=False)
    assert (await review_answers.answer_of(review)).state == "absent"


@pytest.mark.parametrize("body", ['{"a":1}'.encode("utf-16le")])
async def test_text_in_another_encoding_is_absent(clients, body):
    review = await _reviewed(clients)
    await _key((1, body, "db"))
    assert (await review_answers.answer_of(review)).state == "absent"
