"""Caller metadata intake for proxied calls."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from ...infra.db import session_maker
from ...domain.governance import budgets as budget_policy
from ...domain.identity.access import Caller
from ...caller_metadata import _META_KEY_RE
from .idempotency import (
    _claim_idempotent,
    _idem_display,
    _idempotency_key,
    _replay_idempotent,
    _request_fingerprint,
    _scoped_idempotency_key,
)
from .types import IdempotencyFailed, IdempotentReplay, IntakeFailed


# ---- caller tags (X-Treg-Meta) -----------------------------------------------------------------
# A builder reselling treg through one token stamps their OWN ids on each call —
# `X-Treg-Meta: customer=cust_8123, workspace=ws_9` — so they can attribute, budget and invoice their
# users. Deliberately a HEADER and not a tool argument: a model asked to pass an id drops it somewhere
# in a chain, and a figure you cannot reconcile is worse than no figure. The builder's backend already
# sets Authorization on this request; this is the same call site.
META_HEADER = "x-treg-meta"
_META_MAX_HEADER = 512


@dataclass(frozen=True)
class CallMeta:
    """The parsed tag bag for one call. Built ONCE per request (see call_tool) and read by everyone —
    idempotency scope, budgets, the ledger and the audit row. A second parse site would be a second
    chance to disagree about who pays."""

    tags: dict[str, str]
    primary_dim: str = budget_policy.DEFAULT_PRIMARY_DIM

    @property
    def primary_val(self) -> str:
        return self.tags.get(self.primary_dim, "")


_NO_META = CallMeta(tags={})


def _tag_telemetry(meta: CallMeta) -> dict:
    """The tag columns of an audit row, built the one way — the refusal path and the success path
    both write them and had drifted apart once already.

    `budget_dim` stays blank unless the PRIMARY dimension actually carries a value: a call tagged
    only on some other key must not claim a primary it never had, or a report grouped by the indexed
    column would attribute it to the empty value.
    """
    return {"budget_dim": meta.primary_dim if meta.primary_val else "",
            "budget_val": meta.primary_val,
            "tags": dict(meta.tags) or None}


def _parse_call_meta(raw_header: str | None, caller: Caller | None = None) -> CallMeta:
    """`X-Treg-Meta: k=v, k=v` → a validated bag. No header means today's behaviour exactly.

    REFUSES rather than repairs. A tag that is silently dropped or truncated is usage that leaves the
    builder's invoice without anyone noticing, and a truncated id can merge two of their users into one
    line — so an oversized value is a 422, never a `[:128]`.

    A PINNED token (Membership.pinned_tags) wins over the header for the dimensions it names: a token
    handed to one customer's machine must not be able to bill another customer. Naming a different
    value for a pinned dimension is a 403 rather than a silent override — a builder debugging their
    integration needs to see the disagreement, not discover it in a month of misattributed invoices.
    """
    pinned = (caller.membership.pinned_tags if caller is not None else None) or {}
    raw = (raw_header or "").strip()
    if not raw:
        # An unpinned caller with no header is untagged; a pinned one still attributes to its pin, so
        # a builder can hand out a scoped token and never touch the header at all.
        return CallMeta(tags=dict(pinned), primary_dim=budget_policy._primary_dim_of(caller)) if pinned else _NO_META
    if len(raw.encode()) > _META_MAX_HEADER:
        raise IntakeFailed(
            "metadata_invalid", status_code=422,
            detail=f"X-Treg-Meta is limited to {_META_MAX_HEADER} bytes")
    tags: dict[str, str] = {}
    for segment in raw.split(","):
        raw_key, sep, raw_value = segment.partition("=")
        if not sep or not _META_KEY_RE.match(raw_key.strip().lower()):
            # The SHAPE of the segment, which only this parser can report — everything past here is
            # the shared storage-key rule.
            raise IntakeFailed(
                "metadata_invalid", status_code=422,
                detail=(f"X-Treg-Meta must be `key=value` pairs; keys are 1-32 chars of [a-z0-9_] "
                        f"(got {segment.strip()!r})"))
        try:
            key, value = budget_policy._validate_tag_pair(
                raw_key, raw_value, where="X-Treg-Meta")
        except budget_policy.BudgetPolicyError as exc:
            raise IntakeFailed(
                "metadata_invalid", status_code=exc.status_code,
                detail=exc.detail) from exc
        if key in tags:
            raise IntakeFailed(
                "metadata_invalid", status_code=422,
                detail=f"X-Treg-Meta names {key!r} twice")
        tags[key] = value
    if len(tags) > budget_policy._META_MAX_KEYS:
        raise IntakeFailed(
            "metadata_invalid", status_code=422,
            detail=(f"X-Treg-Meta is limited to {budget_policy._META_MAX_KEYS} keys "
                    f"(got {len(tags)})"))
    for dim, pinned_val in pinned.items():
        if tags.get(dim, pinned_val) != pinned_val:
            raise IntakeFailed(
                "metadata_pin_mismatch", status_code=403,
                detail=(f"this token is pinned to {dim}={pinned_val!r} "
                        f"and cannot bill {tags[dim]!r}"))
        tags[dim] = pinned_val
    return CallMeta(tags=tags, primary_dim=budget_policy._primary_dim_of(caller))


@dataclass(frozen=True)
class IntakeResult:
    idempotency_key: str
    fingerprint: str
    replay: IdempotentReplay | None
    claim: tuple[int, str] | None


async def prepare_call_intake(
    *,
    meta: CallMeta,
    idempotency_header: str | None,
    method: str,
    rest: str,
    raw_query: str,
    read_body: Callable[[], Awaitable[bytes]],
    caller: Caller,
    enforce_tag_budgets: Callable[[Caller, CallMeta, AsyncSession], Awaitable[None]],
) -> IntakeResult:
    """Run the pre-resolve gates in their frozen order using bounded transactions."""
    # Blocked status and the per-tag call count, BEFORE the replay below: a blocked user must neither
    # take an idempotency lock nor be handed an answer this team cached before they were blocked.
    async with session_maker() as db:
        await enforce_tag_budgets(caller, meta, db)
        await db.commit()

    # A retry the caller has labelled: answer it from what we already returned, before resolving
    # anything or reaching a provider. Nothing happens without the header, so a caller who sends none
    # sees exactly today's behaviour.
    # Scoped by the primary tag: two of a builder's users WILL both send `retry-1`, and without
    # this the second would be served the first's stored response.
    key = _scoped_idempotency_key(_idempotency_key(idempotency_header), meta)
    if not key:
        return IntakeResult("", "", None, None)

    body = await read_body()
    fingerprint = _request_fingerprint(method, rest, body, raw_query)
    async with session_maker() as db:
        replay = await _replay_idempotent(key, fingerprint, caller, db)
        if replay is not None:
            return IntakeResult(key, fingerprint, replay, None)
        # Claim it now, before anything reaches a provider. Two retries can arrive together and both
        # miss the lookup above; the unique constraint is what makes the loser wait instead of making
        # a second upstream call. A check-then-act in Python would leave exactly the window this
        # feature exists to close — the same reasoning as the conditional UPDATE in ledger.reserve.
        if not await _claim_idempotent(key, fingerprint, rest, caller, db):
            raise IdempotencyFailed(
                "idempotency_in_progress", status_code=409,
                detail=(f"a call with Idempotency-Key {_idem_display(key)!r} "
                        "is already in progress — retry shortly"))
    return IntakeResult(key, fingerprint, None, (caller.membership.id, key))
