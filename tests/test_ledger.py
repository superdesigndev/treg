"""The ledger — the one module that moves money, so these are the tests that have to hold.

Covered: the grant/reserve/settle/release/topup round-trips; the CONCURRENCY gate (N parallel
reserves against a balance that only affords K yield exactly K successes — the property the single
conditional UPDATE exists for); a settle below the reserve refunding the difference; promotional
credit burning before purchased; the stale-hold reaper; and the balance endpoint's auth gate.

The invariant asserted throughout is the one from domain/money:
    org.balance_micro == sum(block.remaining_micro) - sum(open hold.amount_micro)
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from conftest import make_upstream

from treg.domain import money as ledger
from treg.api import app
from treg.config import get_settings
from treg.infra.db import reset_db, session_maker
from treg.models import CreditBlock, Hold, LedgerEntry, Org


def _h(token: str) -> dict:
    return {"X-Treg-Token": token}


@pytest.fixture
async def c():
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        yield client
    await app.state.http.aclose()


async def _org(c: AsyncClient, email: str = "money@superdesign.dev") -> tuple[int, str]:
    """Register a user (which creates their org and fires the signup promo). Returns (org_id, token)."""
    r = await c.post("/users", json={"email": email})
    assert r.status_code == 200, r.text
    return r.json()["org_id"], r.json()["token"]


async def _assert_invariant(org_id: int) -> int:
    """balance == sum(remaining) - sum(open holds). Returns the balance."""
    async with session_maker() as db:
        balance = await ledger.balance_of(db, org_id)
        blocks = sum(b.remaining_micro for b in await ledger.blocks_of(db, org_id))
        holds = sum(h.amount_micro for h in await ledger.open_holds_of(db, org_id))
    assert balance == blocks - holds, f"balance {balance} != blocks {blocks} - holds {holds}"
    return balance


# ---- funding -----------------------------------------------------------------------------------
async def test_new_org_gets_the_signup_promo(c: AsyncClient):
    org_id, _ = await _org(c)
    async with session_maker() as db:
        blocks = await ledger.blocks_of(db, org_id)
        entries = await ledger.entries_of(db, org_id)
    promo = get_settings().promo_grant_micro
    assert [b.kind for b in blocks] == ["promotional"]
    assert blocks[0].amount_micro == blocks[0].remaining_micro == promo
    assert [(e.kind, e.amount_micro) for e in entries] == [("grant", promo)]
    assert await _assert_invariant(org_id) == promo


async def test_grant_is_idempotent_per_kind(c: AsyncClient):
    """The org-creation hook must be safe to call twice (retried signup, second door)."""
    org_id, _ = await _org(c)
    async with session_maker() as db:
        assert await ledger.grant(db, org_id) is None  # already has a promotional block
        blocks = await ledger.blocks_of(db, org_id)
    assert len(blocks) == 1
    assert await _assert_invariant(org_id) == get_settings().promo_grant_micro


async def test_topup_credits_purchased_and_is_idempotent_on_payment_ref(c: AsyncClient):
    """Stripe redelivers webhooks; a second call with the same payment ref must move no money."""
    org_id, _ = await _org(c)
    promo = get_settings().promo_grant_micro
    async with session_maker() as db:
        first = await ledger.topup(db, org_id, 5_000_000, "pi_test_1")
        await db.commit()
        again = await ledger.topup(db, org_id, 5_000_000, "pi_test_1")
        await db.commit()
    assert first.id == again.id and first.kind == "purchased"
    assert await _assert_invariant(org_id) == promo + 5_000_000
    async with session_maker() as db:
        topups = [e for e in await ledger.entries_of(db, org_id) if e.kind == "topup"]
    assert len(topups) == 1  # append-only journal, but only ONE entry for one payment


async def test_two_deliveries_of_one_payment_credit_once(c: AsyncClient):
    """The sequential test above passes even WITHOUT the unique index, because the second call sees
    the first one's committed row. The dangerous case is two deliveries in flight at the same time —
    Stripe delivers at least once, retries after the 500 the webhook deliberately returns, and prod
    runs more than one instance. Both would SELECT nothing and both would credit.

    THIS is the race proof for the caller-owned transaction (it runs against Postgres in CI): the
    loser's flush blocks on the winner's uncommitted unique key until the winner's PROMPT commit,
    and the loser's savepoint rollback preserves its own caller's transaction, so its re-SELECT
    returns the winner's block instead of a 500.

    Each task stages an UNRELATED row before calling topup, and every one of those rows must land:
    that is what proves "preserves its own caller's transaction" - a loser recovered with a session
    rollback instead of the savepoint would answer correctly and silently discard its caller's
    staged work.

    Each task needs its OWN session: two coroutines sharing one AsyncSession is a different bug.
    """
    from treg.models import AdConversion

    org_id, _ = await _org(c)
    promo = get_settings().promo_grant_micro

    async def _deliver(n: int):
        async with session_maker() as db:
            # Staged BEFORE topup, committed by the same caller commit: the losers' savepoint
            # rollbacks must leave this pending row alive.
            db.add(AdConversion(org_id=org_id, action=f"race-probe-{n}"))
            block = await ledger.topup(db, org_id, 5_000_000, "pi_concurrent")
            await db.commit()
            return block

    blocks = await asyncio.gather(*(_deliver(n) for n in range(3)), return_exceptions=True)
    assert not [b for b in blocks if isinstance(b, Exception)], blocks   # none may error out
    assert len({b.id for b in blocks}) == 1                              # all three: the same block

    assert await _assert_invariant(org_id) == promo + 5_000_000          # credited ONCE
    async with session_maker() as db:
        assert len([e for e in await ledger.entries_of(db, org_id) if e.kind == "topup"]) == 1
        staged = (await db.execute(select(AdConversion).where(
            AdConversion.org_id == org_id))).scalars().all()
    # BOTH losers' staged work survived their IntegrityError savepoint rollbacks, not just the winner's.
    assert sorted(row.action for row in staged) == [f"race-probe-{n}" for n in range(3)]


async def test_topup_stages_only_and_the_caller_owns_durability(c: AsyncClient):
    """The durability probe that distinguishes caller ownership: nothing a topup stages is durable
    until the CALLER commits, and the caller's rollback discards all of it. This is the test that
    fails against an internally-committing topup, where block, balance and entry are durable the
    moment the call returns."""
    org_id, _ = await _org(c)
    promo = get_settings().promo_grant_micro

    async with session_maker() as db:
        block = await ledger.topup(db, org_id, 5_000_000, "pi_staged_only")
        assert block is not None
        # Before this session commits, a SECOND session must see none of it: no block, no balance
        # change, no ledger entry.
        async with session_maker() as probe:
            assert (await probe.execute(select(CreditBlock).where(
                CreditBlock.stripe_payment_intent == "pi_staged_only"))).scalars().first() is None
            assert await ledger.balance_of(probe, org_id) == promo
            assert [e for e in await ledger.entries_of(probe, org_id) if e.kind == "topup"] == []
        await db.rollback()

    async with session_maker() as db:  # after the rollback, all three remain absent
        assert (await db.execute(select(CreditBlock).where(
            CreditBlock.stripe_payment_intent == "pi_staged_only"))).scalars().first() is None
        assert await ledger.balance_of(db, org_id) == promo
        assert [e for e in await ledger.entries_of(db, org_id) if e.kind == "topup"] == []
    assert await _assert_invariant(org_id) == promo


async def test_grant_stages_only_and_the_caller_owns_durability(c: AsyncClient):
    """Same probe for grant. The E2E promo test intercepts a commit, but an internally-committing
    grant would satisfy it too (the intercepted commit would just be grant's own) - visibility from
    a second session before and after the caller's commit is what proves who owns the transaction."""
    async with session_maker() as db:
        org = Org(name="grant-staged", slug="grant-staged")
        db.add(org)
        await db.commit()
        await db.refresh(org)
        org_id = org.id

    promo = get_settings().promo_grant_micro
    async with session_maker() as db:
        block = await ledger.grant(db, org_id)
        assert block is not None
        # Staged, not durable: a second session sees no block and no balance change yet.
        async with session_maker() as probe:
            assert await ledger.blocks_of(probe, org_id) == []
            assert await ledger.balance_of(probe, org_id) == 0
            assert await ledger.entries_of(probe, org_id) == []
        await db.commit()

    async with session_maker() as db:  # the caller's commit is what lands it
        assert [b.kind for b in await ledger.blocks_of(db, org_id)] == ["promotional"]
        assert [e.kind for e in await ledger.entries_of(db, org_id)] == ["grant"]
    assert await _assert_invariant(org_id) == promo


@pytest.mark.parametrize("amount,ref", [(0, "pi_x"), (-1, "pi_x"), (1_000, "")])
async def test_topup_rejects_nonsense(c: AsyncClient, amount, ref):
    org_id, _ = await _org(c)
    async with session_maker() as db:
        with pytest.raises(ValueError):
            await ledger.topup(db, org_id, amount, ref)


async def test_concurrent_sweeps_pay_a_referral_once(c: AsyncClient):
    """Two instances sweep the same due referral at once. The claim UPDATE (qualified -> paid,
    committed before any credit moves) is what arbitrates, so exactly one sweep pays and each side
    is credited exactly once. Runs against Postgres in CI, where the race is real.
    """
    from datetime import timedelta

    from treg.domain import referrals
    from treg.models import Referral

    r = await c.post("/users", json={"email": "referrer@superdesign.dev"})
    referrer_org, referrer_user = r.json()["org_id"], r.json()["id"]
    r = await c.post("/users", json={"email": "referee@superdesign.dev"})
    referred_org, referred_user = r.json()["org_id"], r.json()["id"]
    promo = get_settings().promo_grant_micro

    async with session_maker() as db:
        db.add(Referral(code="ref-sweep-race", referrer_user_id=referrer_user,
                        referred_user_id=referred_user, referred_org_id=referred_org,
                        status="qualified",
                        qualified_at=referrals.hold_cutoff() - timedelta(days=1)))
        await db.commit()

    async def one_sweep() -> int:
        async with session_maker() as db:
            return await referrals.sweep(db)

    results = await asyncio.gather(one_sweep(), one_sweep(), return_exceptions=True)
    assert not [x for x in results if isinstance(x, Exception)], results
    assert sum(results) == 1  # exactly one sweep won the claim and paid

    async with session_maker() as db:
        row = (await db.execute(select(Referral))).scalars().first()
        assert row.status == "paid"
        assert row.referrer_block_id and row.referred_block_id
        referral_grants = [
            e for org in (referrer_org, referred_org)
            for e in await ledger.entries_of(db, org)
            if e.kind == "grant" and e.meta.get("block_kind") == "referral"
        ]
    assert len(referral_grants) == 2  # referee + referrer, each paid exactly once
    s = get_settings()
    assert await _assert_invariant(referrer_org) == promo + s.referral_referrer_micro
    assert await _assert_invariant(referred_org) == promo + s.referral_referred_micro


async def test_sweep_grants_and_stamps_commit_together(c: AsyncClient, monkeypatch):
    """Failure injection for the claim-then-grant saga: the REFERRER grant raises after the claim
    commit, with the referee grant already STAGED. The new invariant is that the grants and the
    block-id stamps commit together, so after the failure NEITHER a grant's ledger entry/block NOR
    a stamp may exist. (The row staying claimed - paid with null block ids - is the deliberate
    err-toward-paying-once design and is asserted as such, not changed.) The old non-atomic shape,
    where each grant committed itself, leaves the referee's money durable with no stamp."""
    from datetime import timedelta

    from treg.domain import referrals
    from treg.models import Referral

    r = await c.post("/users", json={"email": "atomic-ref@superdesign.dev"})
    referrer_org, referrer_user = r.json()["org_id"], r.json()["id"]
    r = await c.post("/users", json={"email": "atomic-referee@superdesign.dev"})
    referred_org, referred_user = r.json()["org_id"], r.json()["id"]
    promo = get_settings().promo_grant_micro

    async with session_maker() as db:
        db.add(Referral(code="ref-atomic", referrer_user_id=referrer_user,
                        referred_user_id=referred_user, referred_org_id=referred_org,
                        status="qualified",
                        qualified_at=referrals.hold_cutoff() - timedelta(days=1)))
        await db.commit()

    real_grant = ledger.grant

    async def referrer_grant_boom(db, org_id, *a, **kw):
        if (kw.get("meta") or {}).get("side") == "referrer":
            raise RuntimeError("crashed after the claim commit, mid-payout")
        return await real_grant(db, org_id, *a, **kw)  # the referee grant stages normally

    monkeypatch.setattr(ledger, "grant", referrer_grant_boom)
    async with session_maker() as db:
        assert await referrals.sweep(db) == 0  # the failure is contained, and nothing counts as paid

    async with session_maker() as db:
        row = (await db.execute(select(Referral))).scalars().first()
        assert row.status == "paid"  # the claim commit stands: visible, and errs toward paying once
        assert row.referred_block_id is None and row.referrer_block_id is None  # no stamp...
        for org_id in (referrer_org, referred_org):  # ...and no grant. They land or vanish together.
            assert [e for e in await ledger.entries_of(db, org_id)
                    if e.kind == "grant" and e.meta.get("block_kind") == "referral"] == []
            assert [b for b in await ledger.blocks_of(db, org_id) if b.kind == "referral"] == []
    assert await _assert_invariant(referrer_org) == promo
    assert await _assert_invariant(referred_org) == promo


async def test_referee_instant_grant_failure_after_staging_never_raises(
        c: AsyncClient, monkeypatch, caplog):
    """`_grant_referee`'s recovery rollback expires the Referral row, so its warning log must read
    primitives copied beforehand - `row.id` off the expired row is implicit async I/O
    (MissingGreenlet), which broke the never-raises contract exactly when it mattered."""
    import logging as _logging

    from treg.domain import referrals
    from treg.models import Referral

    r = await c.post("/users", json={"email": "ref-boom-a@superdesign.dev"})
    referrer_user = r.json()["id"]
    r = await c.post("/users", json={"email": "ref-boom-b@superdesign.dev"})
    referred_org, referred_user = r.json()["org_id"], r.json()["id"]
    async with session_maker() as db:
        db.add(Referral(code="ref-instant-boom", referrer_user_id=referrer_user,
                        referred_user_id=referred_user, referred_org_id=referred_org,
                        status="qualified", qualified_at=referrals._now()))
        await db.commit()

    real_grant = ledger.grant

    async def grant_then_boom(db, org_id, *a, **kw):
        await real_grant(db, org_id, *a, **kw)  # SQL has staged, so the recovery rollback expires rows
        raise RuntimeError("grant broke after staging")

    monkeypatch.setattr(ledger, "grant", grant_then_boom)
    # Alembic's fileConfig (disable_existing_loggers=True) elsewhere in the suite disables the
    # "treg" logger; re-enable it so caplog sees the warning regardless of test order.
    monkeypatch.setattr(_logging.getLogger("treg"), "disabled", False)
    with caplog.at_level(_logging.WARNING, logger="treg"):
        async with session_maker() as db:
            row = (await db.execute(select(Referral))).scalars().first()
            await referrals._grant_referee(db, row)  # must swallow, never raise
    assert "instant referee grant failed" in caplog.text

    async with session_maker() as db:  # nothing landed: no stamp, no money
        row = (await db.execute(select(Referral))).scalars().first()
        assert row.referred_block_id is None
        assert [e for e in await ledger.entries_of(db, referred_org)
                if e.meta.get("block_kind") == "referral"] == []


async def test_sweep_grant_failure_after_staging_never_raises(c: AsyncClient, monkeypatch, caplog):
    """The sweep's recovery path, same contract: a grant that fails after staging rolls the session
    back, which expires EVERY due row - so the ids the warning logs must have been copied out before
    the loop, or the second failure raises MissingGreenlet out of a webhook or a page load."""
    import logging as _logging
    from datetime import timedelta

    from treg.domain import referrals
    from treg.models import Referral

    r = await c.post("/users", json={"email": "sweep-boom-ref@superdesign.dev"})
    referrer_user = r.json()["id"]
    rows = []
    for i in range(2):  # TWO due rows: the second iteration's log is the one a loop-local copy misses
        r = await c.post("/users", json={"email": f"sweep-boom-{i}@superdesign.dev"})
        rows.append((r.json()["org_id"], r.json()["id"]))
    async with session_maker() as db:
        for i, (org_id, user_id) in enumerate(rows):
            db.add(Referral(code=f"ref-sweep-boom-{i}", referrer_user_id=referrer_user,
                            referred_user_id=user_id, referred_org_id=org_id, status="qualified",
                            qualified_at=referrals.hold_cutoff() - timedelta(days=1)))
        await db.commit()

    real_grant = ledger.grant

    async def grant_then_boom(db, org_id, *a, **kw):
        await real_grant(db, org_id, *a, **kw)
        raise RuntimeError("grant broke after staging")

    monkeypatch.setattr(ledger, "grant", grant_then_boom)
    monkeypatch.setattr(_logging.getLogger("treg"), "disabled", False)  # see the referee test
    with caplog.at_level(_logging.WARNING, logger="treg"):
        async with session_maker() as db:
            paid = await referrals.sweep(db)  # must swallow both failures, never raise
    assert paid == 0
    assert caplog.text.count("payout failed, will retry") == 2

    async with session_maker() as db:  # neither payout landed any money or stamps
        for row in (await db.execute(select(Referral))).scalars().all():
            assert row.referred_block_id is None and row.referrer_block_id is None
        for org_id, _ in rows:
            assert [e for e in await ledger.entries_of(db, org_id)
                    if e.meta.get("block_kind") == "referral"] == []


async def test_referrals_page_survives_a_sweep_rollback(c: AsyncClient, monkeypatch, caplog):
    """The referrals-page journey logs around ensure_code and sweep; both logs must use the
    `user_id` primitive the journey already holds, because a rollback inside either call expires
    the user row and `user.id` in the handler would 500 the page it protects."""
    import logging as _logging

    from treg.domain import referrals
    from treg.application import referrals as referrals_app

    r = await c.post("/users", json={"email": "page-boom@superdesign.dev"})
    user_id = r.json()["id"]

    async def sweep_boom(db, **kw):
        await db.execute(select(1))  # the real sweep's SELECT begins the transaction the rollback ends
        await db.rollback()  # what the domain recovery path does; it expires every tracked object
        raise RuntimeError("sweep broke")

    monkeypatch.setattr(referrals, "sweep", sweep_boom)
    monkeypatch.setattr(_logging.getLogger("treg"), "disabled", False)  # see the referee test
    with caplog.at_level(_logging.WARNING, logger="treg"):
        summary = await referrals_app.get_referral_summary(user_id)  # must not raise
    assert "referral sweep failed for user" in caplog.text
    assert summary["code"]  # the page still renders
    """The one-transaction property, in both directions: a failed commit loses the grant AND the
    queued ad conversion (and does not raise - the never-500-the-signup contract), and the retry
    makes both durable in one commit."""
    from sqlalchemy.ext.asyncio import AsyncSession as SAAsyncSession

    from treg import adsconv
    from treg.application import signup
    from treg.models import AdConversion

    monkeypatch.setattr(adsconv, "enabled", lambda: True)
    async with session_maker() as db:
        org = Org(name="promo-atomic", slug="promo-atomic", ad_gclid="CLICK_SIGNUP")
        db.add(org)
        await db.commit()
        await db.refresh(org)
        org_id = org.id

    real_commit = SAAsyncSession.commit
    state = {"failed": False}

    async def failing_commit(self):
        if not state["failed"]:
            state["failed"] = True
            raise RuntimeError("simulated commit failure")
        return await real_commit(self)

    monkeypatch.setattr(SAAsyncSession, "commit", failing_commit)
    async with session_maker() as db:
        await signup._grant_signup_promo(db, await db.get(Org, org_id))  # must not raise
    assert state["failed"], "the promo commit was never attempted"

    async with session_maker() as db:  # neither half survived the failed commit
        assert [e for e in await ledger.entries_of(db, org_id) if e.kind == "grant"] == []
        assert (await db.execute(select(AdConversion).where(
            AdConversion.org_id == org_id))).scalars().all() == []
    assert await _assert_invariant(org_id) == 0

    async with session_maker() as db:  # the retry lands BOTH, in one commit
        await signup._grant_signup_promo(db, await db.get(Org, org_id))
    async with session_maker() as db:
        assert [e.kind for e in await ledger.entries_of(db, org_id)] == ["grant"]
        assert len((await db.execute(select(AdConversion).where(
            AdConversion.org_id == org_id))).scalars().all()) == 1
    assert await _assert_invariant(org_id) == get_settings().promo_grant_micro


async def test_a_grant_failure_cannot_fail_signup(c: AsyncClient, monkeypatch):
    """The promo is a nicety: a broken grant must cost the team its $1, never their signup."""
    async def boom(*a, **kw):
        raise RuntimeError("grant broke")

    monkeypatch.setattr(ledger, "grant", boom)
    r = await c.post("/users", json={"email": "promo-fails@superdesign.dev"})
    assert r.status_code == 200, r.text
    org_id = r.json()["org_id"]
    async with session_maker() as db:
        assert await db.get(Org, org_id) is not None
        assert await ledger.balance_of(db, org_id) == 0
        assert await ledger.blocks_of(db, org_id) == []


async def test_a_grant_failure_after_staging_still_returns_the_signup(c: AsyncClient, monkeypatch):
    """The sharper variant of the test above: the grant fails AFTER its SQL has staged, so the
    recovery rollback expires every object the session tracks. Both signup doors must still answer
    with the fields they promised, and the referral must still be attributed - the never-500-the-
    signup contract does not stop at objects that now need a reload."""
    from treg.routers.signup_cookies import REFERRAL_COOKIE
    from treg.models import Referral

    _, ann_token = await _org(c, "ref-ann@superdesign.dev")
    code = (await c.post("/referrals/code", headers=_h(ann_token))).json()["code"]

    real_grant = ledger.grant

    async def grant_then_boom(db, org_id, *a, **kw):
        await real_grant(db, org_id, *a, **kw)  # SQL has run, so the rollback below expires objects
        raise RuntimeError("grant broke after staging")

    monkeypatch.setattr(ledger, "grant", grant_then_boom)

    r = await c.post("/users", json={"email": "promo-fails-late@superdesign.dev"},
                     headers={"Cookie": f"{REFERRAL_COOKIE}={code}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == "promo-fails-late@superdesign.dev"
    assert body["id"] and body["org"] and body["org_id"] and body["token"]
    org_id = body["org_id"]

    r2 = await c.post("/orgs", json={"name": "second-team"}, headers=_h(body["token"]))
    assert r2.status_code == 200, r2.text
    assert r2.json()["org"] and r2.json()["org_id"] and r2.json()["name"] == "second-team"

    async with session_maker() as db:
        assert await ledger.balance_of(db, org_id) == 0  # the credit is lost, never the signup
        referred = (await db.execute(select(Referral).where(
            Referral.referred_org_id == org_id))).scalars().all()
    assert len(referred) == 1  # the failed grant did not cost the referral attribution either


# ---- the call path -----------------------------------------------------------------------------
async def test_reserve_settle_round_trip(c: AsyncClient):
    org_id, _ = await _org(c)
    promo = get_settings().promo_grant_micro
    async with session_maker() as db:
        call_id = await ledger.reserve(db, org_id, "tikhub.tiktok.video.comments", 900)
    assert await _assert_invariant(org_id) == promo - 900  # held, not yet spent
    async with session_maker() as db:
        holds = await ledger.open_holds_of(db, org_id)
        assert [h.id for h in holds] == [call_id]
        consumed = await ledger.settle(db, call_id, 900)
    assert consumed == 900
    assert await _assert_invariant(org_id) == promo - 900
    async with session_maker() as db:
        assert await ledger.open_holds_of(db, org_id) == []
        kinds = [e.kind for e in await ledger.entries_of(db, org_id)]
    assert kinds == ["settle", "reserve", "grant"]  # newest first, one entry per movement


async def test_settle_less_than_reserved_refunds_the_difference(c: AsyncClient):
    """A per_result endpoint reserves for the page size and settles to what actually came back."""
    org_id, _ = await _org(c)
    promo = get_settings().promo_grant_micro
    async with session_maker() as db:
        call_id = await ledger.reserve(db, org_id, "e.1", 10_000)
        consumed = await ledger.settle(db, call_id, 2_500)
    assert consumed == 2_500
    assert await _assert_invariant(org_id) == promo - 2_500
    async with session_maker() as db:
        settle = [e for e in await ledger.entries_of(db, org_id) if e.kind == "settle"][0]
    assert settle.meta["refunded_micro"] == 7_500
    assert settle.meta["reserved_micro"] == 10_000 and settle.meta["observed_micro"] == 2_500


async def test_settle_without_an_observed_cost_charges_the_estimate(c: AsyncClient):
    org_id, _ = await _org(c)
    promo = get_settings().promo_grant_micro
    async with session_maker() as db:
        call_id = await ledger.reserve(db, org_id, "e.1", 4_000)
        assert await ledger.settle(db, call_id) == 4_000
    assert await _assert_invariant(org_id) == promo - 4_000


async def test_release_returns_the_hold_in_full(c: AsyncClient):
    """Provider 5xx / network error: not billable, so the org is made whole."""
    org_id, _ = await _org(c)
    promo = get_settings().promo_grant_micro
    async with session_maker() as db:
        call_id = await ledger.reserve(db, org_id, "e.1", 6_000)
        assert await ledger.release(db, call_id, reason="upstream_5xx") == 6_000
    assert await _assert_invariant(org_id) == promo
    async with session_maker() as db:
        entry = [e for e in await ledger.entries_of(db, org_id) if e.kind == "release"][0]
        assert await ledger.open_holds_of(db, org_id) == []
    assert entry.amount_micro == 6_000 and entry.meta["reason"] == "upstream_5xx"


async def test_settle_and_release_are_no_ops_once_the_hold_is_gone(c: AsyncClient):
    """A double settle (retry, or a race with the reaper) must not charge twice."""
    org_id, _ = await _org(c)
    promo = get_settings().promo_grant_micro
    async with session_maker() as db:
        call_id = await ledger.reserve(db, org_id, "e.1", 1_000)
        assert await ledger.settle(db, call_id, 1_000) == 1_000
        assert await ledger.settle(db, call_id, 1_000) == 0
        assert await ledger.release(db, call_id) == 0
    assert await _assert_invariant(org_id) == promo - 1_000


async def test_reserve_refuses_when_the_balance_is_short(c: AsyncClient):
    org_id, _ = await _org(c)
    promo = get_settings().promo_grant_micro
    async with session_maker() as db:
        with pytest.raises(ledger.InsufficientBalance) as exc:
            await ledger.reserve(db, org_id, "e.1", promo + 1)
    assert exc.value.balance_micro == promo and exc.value.required_micro == promo + 1
    assert await _assert_invariant(org_id) == promo  # nothing moved, no hold left behind
    async with session_maker() as db:
        assert await ledger.open_holds_of(db, org_id) == []


async def test_reserve_drains_to_exactly_zero_then_refuses(c: AsyncClient):
    org_id, _ = await _org(c)
    promo = get_settings().promo_grant_micro
    async with session_maker() as db:
        call_id = await ledger.reserve(db, org_id, "e.1", promo)
        assert await ledger.balance_of(db, org_id) == 0
        with pytest.raises(ledger.InsufficientBalance):
            await ledger.reserve(db, org_id, "e.1", 1)
        await ledger.settle(db, call_id, promo)
    assert await _assert_invariant(org_id) == 0


# ---- concurrency: the reason reserve is a single conditional UPDATE ------------------------------
async def test_n_parallel_reserves_yield_exactly_k_successes(c: AsyncClient):
    """The gate. 20 agents call at once against a balance that affords 5 → exactly 5 pass.

    Each task uses its OWN session, so the race is real (a shared session would serialize in the
    client and prove nothing). Nothing may oversell, and nothing may be left stranded.
    """
    org_id, _ = await _org(c)
    est, affordable = 40_000, 5
    async with session_maker() as db:  # trim the promo to exactly K calls' worth
        await ledger.settle(db, await ledger.reserve(
            db, org_id, "trim", get_settings().promo_grant_micro - est * affordable))
    assert await _assert_invariant(org_id) == est * affordable

    async def one():
        async with session_maker() as db:
            try:
                return await ledger.reserve(db, org_id, "e.hot", est)
            except ledger.InsufficientBalance:
                return None

    results = await asyncio.gather(*(one() for _ in range(20)))
    won = [r for r in results if r]
    assert len(won) == affordable, f"expected {affordable} winners, got {len(won)}"
    assert await _assert_invariant(org_id) == 0  # every winner's money is in a hold
    async with session_maker() as db:
        assert sorted(h.id for h in await ledger.open_holds_of(db, org_id)) == sorted(won)
        for call_id in won:
            await ledger.settle(db, call_id, est)
    assert await _assert_invariant(org_id) == 0


# ---- block consumption order --------------------------------------------------------------------
async def test_promotional_burns_before_purchased_then_oldest_purchased(c: AsyncClient):
    """Promo is a marketing expense and non-refundable; purchased is a refundable liability. Spend
    the non-refundable pool first, then the oldest purchase."""
    org_id, _ = await _org(c)
    promo = get_settings().promo_grant_micro
    async with session_maker() as db:
        old = await ledger.topup(db, org_id, 2_000_000, "pi_old")
        new = await ledger.topup(db, org_id, 2_000_000, "pi_new")
        await db.commit()
    async with session_maker() as db:  # make the age order unambiguous regardless of clock resolution
        row = await db.get(CreditBlock, new.id)
        row.created_at = row.created_at.replace(year=row.created_at.year + 1)
        db.add(row)
        await db.commit()

    async def spend(amount: int) -> None:
        async with session_maker() as db:
            await ledger.settle(db, await ledger.reserve(db, org_id, "e.1", amount), amount)

    await spend(promo - 1)          # inside the promo block
    async with session_maker() as db:
        by_id = {b.id: b for b in await ledger.blocks_of(db, org_id)}
    assert by_id[old.id].remaining_micro == 2_000_000 and by_id[new.id].remaining_micro == 2_000_000

    await spend(1 + 500_000)        # finishes the promo, then bites the OLDEST purchased block
    async with session_maker() as db:
        by_id = {b.id: b for b in await ledger.blocks_of(db, org_id)}
        promo_block = [b for b in by_id.values() if b.kind == "promotional"][0]
    assert promo_block.remaining_micro == 0
    assert by_id[old.id].remaining_micro == 1_500_000
    assert by_id[new.id].remaining_micro == 2_000_000  # untouched — newest purchase spends last
    assert await _assert_invariant(org_id) == 3_500_000


# ---- reaper ------------------------------------------------------------------------------------
async def test_reaper_releases_stale_holds_and_leaves_fresh_ones(c: AsyncClient):
    from datetime import timedelta

    org_id, _ = await _org(c)
    promo = get_settings().promo_grant_micro
    async with session_maker() as db:
        stale = await ledger.reserve(db, org_id, "e.stale", 7_000)
        fresh = await ledger.reserve(db, org_id, "e.fresh", 3_000)
    async with session_maker() as db:  # age the first hold past call_timeout_s + hold_grace_s
        row = await db.get(Hold, stale)
        row.created_at = row.created_at - timedelta(seconds=ledger.hold_ttl_s() + 5)
        db.add(row)
        await db.commit()
        assert await ledger.reap_stale_holds(db, org_id=org_id) == 1
        assert [h.id for h in await ledger.open_holds_of(db, org_id)] == [fresh]
    assert await _assert_invariant(org_id) == promo - 3_000
    async with session_maker() as db:
        entry = [e for e in await ledger.entries_of(db, org_id) if e.kind == "release"][0]
    assert entry.call_id == stale and entry.meta["reason"] == "stale_hold_reaped"


async def test_reserve_reaps_stale_holds_first(c: AsyncClient):
    """Lazy reaping: a crash-stranded hold must not make a funded org look broke on its next call."""
    from datetime import timedelta

    org_id, _ = await _org(c)
    promo = get_settings().promo_grant_micro
    async with session_maker() as db:
        stranded = await ledger.reserve(db, org_id, "e.crashed", promo)  # takes the whole balance
        row = await db.get(Hold, stranded)
        row.created_at = row.created_at - timedelta(seconds=ledger.hold_ttl_s() + 5)
        db.add(row)
        await db.commit()
    async with session_maker() as db:
        call_id = await ledger.reserve(db, org_id, "e.next", 1_000)  # would 402 without the reap
        assert await ledger.balance_of(db, org_id) == promo - 1_000
        assert [h.id for h in await ledger.open_holds_of(db, org_id)] == [call_id]
    assert await _assert_invariant(org_id) == promo - 1_000


async def test_reaped_refund_survives_a_following_application_owned_402(c: AsyncClient):
    """Lazy reap is its own committed phase, not part of the new reservation transaction."""
    from datetime import timedelta

    org_id, _ = await _org(c)
    promo = get_settings().promo_grant_micro
    failed_call_id = "reserve-after-reap-402"
    async with session_maker() as db:
        stranded = await ledger.reserve(db, org_id, "e.crashed", promo)
        row = await db.get(Hold, stranded)
        row.created_at = row.created_at - timedelta(seconds=ledger.hold_ttl_s() + 5)
        db.add(row)
        await db.commit()

    async with session_maker() as db:
        with pytest.raises(ledger.InsufficientBalance):
            await ledger.reserve_in_transaction(
                db, org_id, "e.too-expensive", promo + 1, call_id=failed_call_id)
        await db.rollback()

    assert await _assert_invariant(org_id) == promo
    async with session_maker() as db:
        assert await ledger.open_holds_of(db, org_id) == []
        entries = await ledger.entries_of(db, org_id)
    releases = [entry for entry in entries if entry.kind == "release"]
    assert len(releases) == 1
    assert releases[0].call_id == stranded
    assert releases[0].meta["reason"] == "stale_hold_reaped"
    assert not [entry for entry in entries if entry.call_id == failed_call_id]


# ---- margin ------------------------------------------------------------------------------------
async def test_margin_is_applied_at_reserve_and_settle(c: AsyncClient, monkeypatch):
    """Margin lives in the ledger (not the call sites) and is recorded on every entry, so a later
    rate change can't rewrite what a past call cost."""
    org_id, _ = await _org(c)
    promo = get_settings().promo_grant_micro
    get_settings.cache_clear()
    monkeypatch.setenv("TREG_PLATFORM_MARGIN", "0.5")
    try:
        assert ledger.with_margin(1_000) == 1_500
        assert ledger.with_margin(3) == 5  # rounds UP: never charge less than the call cost us
        async with session_maker() as db:
            call_id = await ledger.reserve(db, org_id, "e.1", 1_000)
            assert await ledger.balance_of(db, org_id) == promo - 1_500
            assert await ledger.settle(db, call_id, 1_000) == 1_500
            entry = [e for e in await ledger.entries_of(db, org_id) if e.kind == "settle"][0]
        assert entry.meta["margin"] == 0.5 and entry.meta["observed_micro"] == 1_000
    finally:
        monkeypatch.delenv("TREG_PLATFORM_MARGIN")
        get_settings.cache_clear()
    assert await _assert_invariant(org_id) == promo - 1_500


# ---- the ledger is append-only ------------------------------------------------------------------
async def test_every_movement_writes_exactly_one_entry(c: AsyncClient):
    org_id, _ = await _org(c)
    async with session_maker() as db:
        await ledger.topup(db, org_id, 1_000_000, "pi_1")
        await db.commit()
        await ledger.settle(db, await ledger.reserve(db, org_id, "e.1", 500), 500)
        await ledger.release(db, await ledger.reserve(db, org_id, "e.2", 500))
        rows = (await db.execute(select(LedgerEntry).where(LedgerEntry.org_id == org_id))).scalars().all()
    assert sorted(r.kind for r in rows) == [
        "grant", "release", "reserve", "reserve", "settle", "topup"]
    # signs are from the ORG's point of view: credit positive, spend negative
    assert all((r.amount_micro > 0) == (r.kind in ("grant", "topup", "release")) for r in rows)


# ---- API ---------------------------------------------------------------------------------------
async def test_balance_endpoint_reports_blocks_holds_and_entries(c: AsyncClient):
    org_id, token = await _org(c)
    promo = get_settings().promo_grant_micro
    async with session_maker() as db:
        call_id = await ledger.reserve(db, org_id, "tikhub.x.y", 12_000)
    r = await c.get(f"/orgs/{org_id}/balance", headers=_h(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["balance_micro"] == promo - 12_000
    assert body["balance_usd"] == pytest.approx((promo - 12_000) / 1_000_000)
    assert [b["kind"] for b in body["blocks"]] == ["promotional"]
    assert body["holds"] == [{"call_id": call_id, "endpoint_id": "tikhub.x.y",
                              "amount_micro": 12_000, "created_at": body["holds"][0]["created_at"]}]
    assert [e["kind"] for e in body["entries"]["items"]] == ["reserve", "grant"]


async def test_balance_endpoint_paginates_entries(c: AsyncClient):
    org_id, token = await _org(c)
    for i in range(4):
        async with session_maker() as db:
            await ledger.settle(db, await ledger.reserve(db, org_id, f"e.{i}", 100), 100)
    r = await c.get(f"/orgs/{org_id}/balance", params={"limit": 3}, headers=_h(token))
    page1 = r.json()["entries"]["items"]
    r2 = await c.get(f"/orgs/{org_id}/balance", params={"limit": 3, "offset": 3}, headers=_h(token))
    page2 = r2.json()["entries"]["items"]
    assert len(page1) == 3 and len(page2) == 3
    assert not ({e["id"] for e in page1} & {e["id"] for e in page2})  # no overlap
    # 9 entries: the grant + a reserve/settle pair per call. Newest first, so the grant is last.
    last = (await c.get(f"/orgs/{org_id}/balance", params={"limit": 3, "offset": 6},
                        headers=_h(token))).json()["entries"]["items"]
    assert last[-1]["kind"] == "grant"  # oldest entry is the signup promo


async def test_balance_shows_the_wallet_to_members_and_the_history_to_admins(c: AsyncClient):
    """POLICY CHANGE (2026-08, from Jason's report): this route used to be admin-only, which meant a
    machine identity could not read the balance it was spending — while every 402 already handed it
    `balance_micro`, and both llms.txt and skill.md tell an agent to run `treg balance` after a call.
    Refusing the number here while shipping it in an error was incoherent.

    So the split is by WHAT, not by who: the wallet (figure + in-flight holds) is every member's; the
    funding detail (which blocks were bought, when, what is left of each, and the ledger) is the org's
    purchase history and stays admin+. Cross-org and unauthenticated access are unchanged."""
    org_id, owner = await _org(c, "owner@superdesign.dev")
    r = await c.post(f"/orgs/{org_id}/invites", json={"email": "m@superdesign.dev", "role": "member"},
                     headers=_h(owner))
    code = r.json()["code"]
    member = (await c.post("/invites/accept",
                           json={"code": code, "email": "m@superdesign.dev"})).json()["token"]

    seen = await c.get(f"/orgs/{org_id}/balance", headers=_h(member))
    assert seen.status_code == 200
    assert "balance_micro" in seen.json()                     # the wallet: yes
    assert seen.json()["blocks"] == []                        # the purchase history: no
    assert seen.json()["entries"]["items"] == []

    assert (await c.get(f"/orgs/{org_id}/balance", headers=_h(owner))).json()["blocks"]

    other_org, other = await _org(c, "stranger@superdesign.dev")
    assert (await c.get(f"/orgs/{org_id}/balance", headers=_h(other))).status_code == 403
    assert (await c.get(f"/orgs/{other_org}/balance", headers=_h(other))).status_code == 200
    assert (await c.get(f"/orgs/{org_id}/balance")).status_code == 401  # unauthenticated


# ---- CLI ---------------------------------------------------------------------------------------
def test_cli_balance_renders_credit_holds_and_ledger(monkeypatch, capsys):
    """`treg balance` is a rendered table, so it must show sub-cent amounts as money (a call costs
    ~$0.0006 — rounding to $0.00 would read as free) and keep held money separate from spent."""
    from treg import cli

    body = {
        "org_id": 1, "balance_micro": 987_400, "balance_usd": 0.9874,
        "blocks": [{"id": "b1", "kind": "promotional", "amount_micro": 1_000_000,
                    "remaining_micro": 999_400, "created_at": "2026-07-30T10:00:00"}],
        "holds": [{"call_id": "c1", "endpoint_id": "tikhub.x.y", "amount_micro": 12_000,
                   "created_at": "2026-07-30T10:05:00"}],
        "entries": {"limit": 20, "offset": 0, "items": [
            {"id": "e2", "kind": "settle", "amount_micro": -600, "endpoint_id": "tikhub.x.y",
             "meta": {}, "created_at": "2026-07-30T10:01:00"},
            {"id": "e1", "kind": "grant", "amount_micro": 1_000_000, "endpoint_id": None,
             "meta": {}, "created_at": "2026-07-30T10:00:00"},
        ]},
    }

    class _Resp:
        status_code = 200
        text = ""
        def json(self): return body

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, path, **kw): return _Resp()

    monkeypatch.setattr(cli, "_client", lambda cfg, **kw: _C())
    monkeypatch.setattr(cli, "_active_org_id", lambda cfg, c: 1)
    cli.cmd_balance(cli.build_parser().parse_args(["balance"]), {})
    out = capsys.readouterr().out
    assert "$0.9874" in out                    # the balance, to the sub-cent
    assert "promotional" in out and "in flight" in out   # credit source + held-not-spent
    assert "$0.0120" in out                    # the hold, sub-cent precision preserved
    assert "settle" in out and "-$0.0006" in out and "tikhub.x.y" in out  # sign outside the $
    assert "grant" in out and "$1.00" in out


async def test_demo_orgs_get_no_promo_credit(c: AsyncClient):
    """A published demo token must not be able to spend real money."""
    org_id, _ = await _org(c)
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        org.public_demo = True
        db.add(org)
        await db.commit()
        await ledger._add_balance(db, org_id, -org.balance_micro)  # zero it as a demo org would be
        await db.commit()
        # the hook is what enforces this — a demo org that somehow reaches it gets nothing
        from treg.application.signup import _grant_signup_promo
        await _grant_signup_promo(db, await db.get(Org, org_id))
        assert await ledger.balance_of(db, org_id) == 0
