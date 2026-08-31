"""Tag validation and budget policy shared by call and governance surfaces."""

import re
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...caller_metadata import TAG_DEFAULT, _MAX_BUDGET_DIMS, _META_KEY_RE
from ...config import get_settings
from ...models import Org, TagBudget
from ..identity.access import Caller


_META_MAX_KEYS = 5
_META_MAX_VALUE = 128
# Tag VALUES become storage keys (the idempotency scope, a TagBudget row, a TagSpend row), so the
# charset is an allowlist rather than a length check. See the collision note in `_parse_call_meta`.
_META_VALUE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,%d}$" % _META_MAX_VALUE)
# The dimension that scopes idempotency and defaults reports, for a team that never declared one.
DEFAULT_PRIMARY_DIM = "customer"
_MAX_TAG_VALUES = 10_000    # distinct values per dimension per org, bounded at WRITE (see _tag_budget)


class BudgetPolicyError(Exception):
    """A tag or budget refusal translated by the calling interface."""

    def __init__(self, status_code: int, detail: str | dict) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class TagBudgetResult:
    row: TagBudget | None
    created: bool


def _validate_tag_pair(key: str, value: str, *, where: str = "tag") -> tuple[str, str]:
    """One `dim=val` pair, validated the SAME way wherever it enters treg. THE only rule.

    Both doors have to agree, because a tag value becomes a storage key — the idempotency scope, a
    `TagBudget` row, a `TagSpend` row. `pinned_tags` arrives as JSON on the agent-mint endpoint and
    never passes the header parser, so validating only there would leave the identical hole open one
    route over (it did, until this function existed). `_parse_call_meta` therefore delegates here
    rather than repeating the checks: two copies of a storage-key rule is two chances to drift.

    `where` only names the source in the message ("X-Treg-Meta value" vs "tag value"); the rules
    themselves are identical by construction, which is the entire point.
    """
    key = (key or "").strip().lower()
    value = (value or "").strip()
    if not _META_KEY_RE.match(key):
        raise BudgetPolicyError(
            422, f"{key!r} is not a valid tag key — 1-32 chars of [a-z0-9_]")
    if not value or len(value) > _META_MAX_VALUE:
        raise BudgetPolicyError(
            422, f"{where} value for {key!r} must be 1-{_META_MAX_VALUE} characters")
    if "@" in value:
        # The ledger is append-only, so a tag written today cannot be erased later. An email here
        # is a permanent record of a person, which is not a thing we can undo on request.
        raise BudgetPolicyError(422, (
            f"{where} value for {key!r} looks like an email — use an opaque id: these tags are "
            f"written to an append-only ledger and cannot be deleted afterwards"))
    if not _META_VALUE_RE.match(value):
        # An ALLOWLIST, not a blocklist, and the reason is `_scoped_idempotency_key`: the primary
        # value is joined to the caller's Idempotency-Key with \x1f, so a value permitted to
        # contain that separator lets `customer="A", key="B\x1fC"` collide with
        # `customer="A\x1fB", key="C"` — one of a builder's users reading another's cached
        # response. Do not narrow this to "reject \x1f": the header parser is not a security
        # boundary we control, and the next separator would reopen it.
        raise BudgetPolicyError(422, (
            f"{where} value for {key!r} may only contain letters, digits and . _ - : "
            f"(these ids are used as storage keys)"))
    return key, value


def _primary_dim_of(caller: Caller | None) -> str:
    """The tag key that scopes idempotency for this team. Per-org so a builder whose billing unit is a
    workspace is not forced to call it "customer"."""
    if caller is None:
        return DEFAULT_PRIMARY_DIM
    return (getattr(caller.org, "primary_dim", "") or DEFAULT_PRIMARY_DIM)


def _budget_dims_of(org: Org) -> list[str]:
    """The keys this team may set budgets on — declared, because each one costs an indexed lookup on
    every call and a row per value. Bounded at `_MAX_BUDGET_DIMS`."""
    declared = getattr(org, "budget_dims", None)
    if not declared:
        return [getattr(org, "primary_dim", "") or DEFAULT_PRIMARY_DIM]
    return [str(d) for d in declared][:_MAX_BUDGET_DIMS]


def _effective_daily_cap(org: Org) -> int:
    """This team's ceiling on daily tier-4 spend: the LOWER of what they set and what we allow.

    Two masters, which is why it is two numbers. The team's own figure protects them from a runaway
    agent draining a balance that auto-top-up keeps refilling. The platform ceiling protects US from a
    catalog mispricing, and only we can raise it — so onboarding a high-volume builder is a
    conversation rather than an env-var edit that lifts the blast-radius rail for every team at once.

    0 means "never set one", which follows the deployment default rather than freezing the team at
    whatever that default happened to be the day they signed up.
    """
    ceiling = get_settings().platform_daily_cap_micro
    own = int(getattr(org, "daily_cap_micro", 0) or 0)
    return min(own, ceiling) if own > 0 else ceiling


async def _tag_budget(db: AsyncSession, org_id: int, dim: str, val: str,
                      create: bool = False) -> TagBudgetResult:
    """This team's budget row for one tag value, creating it on first sighting when asked.

    Auto-created, so a builder never pre-registers a user before their first call can carry an id.
    The row also BOUNDS cardinality: the count runs only on the miss path, so steady state stays one
    indexed lookup. Bounding has to happen at the write — a limit checked when a report is run is
    checked after the rows already exist.
    """
    row = (await db.execute(select(TagBudget).where(
        TagBudget.org_id == org_id, TagBudget.dim == dim, TagBudget.val == val))).scalar_one_or_none()
    if row is not None or not create:
        return TagBudgetResult(row=row, created=False)
    seen = (await db.execute(select(func.count()).select_from(TagBudget).where(
        TagBudget.org_id == org_id, TagBudget.dim == dim))).scalar() or 0
    if seen >= _MAX_TAG_VALUES:
        raise BudgetPolicyError(429, {
            "error": "tag_cardinality_exceeded", "dim": dim,
            "message": (f"this team has already used {seen} distinct {dim!r} values, the limit. A tag "
                        f"that changes every call (a session or request id) is not a budget "
                        f"dimension — tag by the unit you bill."),
        })
    row = TagBudget(org_id=org_id, dim=dim, val=val, auto=True)
    db.add(row)
    return TagBudgetResult(row=row, created=True)
