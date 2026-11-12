"""Framework-neutral call application contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Literal, Protocol

if TYPE_CHECKING:
    from ...models import Tool


Blame = Literal["caller", "treg", "upstream", "org_connection"]

FAILURE_KINDS: dict[str, tuple[Blame, str]] = {
    "metadata_invalid": ("caller", "caller_input"),
    "metadata_pin_mismatch": ("caller", "caller_input"),
    "idempotency_mismatch": ("caller", "caller_input"),
    "idempotency_in_progress": ("treg", "treg_capacity"),
    "invalid_target": ("caller", "caller_input"),
    "tool_access_denied": ("caller", "caller_policy"),
    "target_not_found": ("caller", "not_found"),
    "hub_not_runnable": ("treg", "treg_error"),
    "hub_input_invalid": ("caller", "caller_input"),
    "hub_run_failed": ("upstream", "upstream_error"),
    # A hub run stopped by the caller's price ceiling or a crash in treg (a `hub_run_failed` whose
    # detail names it).
    "hub_run_max_cost": ("caller", "caller_balance_or_cap"),
    "hub_run_crashed": ("treg", "treg_error"),
    "hub_busy": ("caller", "treg_policy"),
    "hub_price_unaffordable": ("caller", "caller_balance_or_cap"),
    "unknown_endpoint": ("caller", "not_found"),
    "target_ambiguous": ("caller", "caller_input"),
    "catalog_retired": ("caller", "treg_policy"),
    "catalog_parameter_invalid": ("caller", "caller_input"),
    "async_resource_not_owned": ("caller", "treg_policy"),
    "provider_resource_not_owned": ("caller", "treg_policy"),
    "provider_resource_state_failed": ("treg", "treg_error"),
    "capability_pinned": ("caller", "treg_policy"),
    "policy_denied": ("caller", "caller_policy"),
    # An installed policy extension refused treg's money for this call (`call_admission`).
    "admission_denied": ("caller", "treg_policy"),
    "daily_cap_reached": ("caller", "caller_balance_or_cap"),
    "org_endpoint_quota_exceeded": ("caller", "treg_policy"),
    "public_demo_rate_limited": ("caller", "treg_policy"),
    "trial_allowance_unavailable": ("treg", "treg_capacity"),
    "trial_allowance_reached": ("caller", "caller_balance_or_cap"),
    "platform_cap_unavailable": ("treg", "treg_capacity"),
    "money_admission_timeout": ("treg", "treg_capacity"),
    "catalog_price_invalid": ("treg", "treg_error"),
    "platform_daily_cap_reached": ("caller", "caller_balance_or_cap"),
    "tag_budget_unavailable": ("treg", "treg_capacity"),
    "tag_cardinality_exceeded": ("caller", "caller_input"),
    "tag_blocked": ("caller", "caller_policy"),
    "tag_call_cap_reached": ("caller", "caller_balance_or_cap"),
    "tag_spend_cap_reached": ("caller", "caller_balance_or_cap"),
    "insufficient_balance": ("caller", "caller_balance_or_cap"),
    "free_credit_excludes_video": ("caller", "caller_balance_or_cap"),
    # treg's OWN vendor account for the provider is out (balance/quota) — a 503 the caller cannot
    # fix, answered before any hold exists, with the same-capability alternatives named.
    "provider_capacity": ("treg", "treg_capacity"),
    # This deployment paused the provider (TREG_PAUSED_PROVIDERS): refused before any hold.
    "provider_paused": ("treg", "treg_policy"),
    # Fair share: the provider on treg's key is fully used and this team already holds calls on it
    # (domain/capacity/fair_share.py). Not charged; a routed job moves to its next provider.
    "share_busy": ("caller", "treg_policy"),
    # treg's account for the provider is at its declared rate (domain/capacity/rate_observe.py):
    # refused before it was sent, not charged; a routed job moves to its next provider.
    "provider_rate_limited": ("treg", "treg_capacity"),
    # Routed endpoints (treg.<capability>): the caller's identity fits no provider, or the
    # ceiling they set is below the cheapest candidate, or every candidate failed.
    "route_no_candidate": ("caller", "caller_input"),
    "route_max_cost": ("caller", "caller_balance_or_cap"),
    "route_failed": ("upstream", "upstream_error"),
    "route_caller_fault": ("caller", "caller_input"),
    "route_not_found": ("caller", "not_found"),       # the requested page or record does not exist at its source
    "injection_failed": ("treg", "treg_error"),
    "platform_account_retired": ("treg", "our_provider_account"),
    "platform_account_split": ("treg", "our_provider_account"),
    "platform_account_unavailable": ("treg", "our_provider_account"),
    "ssrf_refused": ("treg", "treg_error"),
    "connect_failed": ("upstream", "upstream_error"),
    "read_timeout": ("upstream", "timeout"),
    "stream_interrupted": ("upstream", "upstream_error"),
    "response_buffer_limit": ("treg", "treg_error"),
    "refresh_failed": ("org_connection", "caller_provider_account"),
    "credential_missing": ("org_connection", "caller_provider_account"),
    "authorization_required": ("org_connection", "caller_provider_account"),
    "method_mismatch": ("caller", "caller_input"),
    "db_pool": ("treg", "treg_capacity"),
    "unexpected_exception": ("treg", "treg_error"),
    "org_concurrency_limit": ("caller", "treg_policy"),
    "org_rate_limit": ("caller", "treg_policy"),
    "org_traffic_paused": ("caller", "treg_policy"),
}


class CallFailure(Exception):
    """A call failure translated once by the HTTP adapter.

    `parameter` and `rule` name the request field a refusal is about and the rule it broke, for the
    `call_error` log line only (call_errors.py); the caller sees `detail` alone."""

    def __init__(
        self,
        kind: str,
        *,
        status_code: int,
        detail: str | dict,
        retry_after_s: int | None = None,
        parameter: str | None = None,
        rule: str | None = None,
    ) -> None:
        super().__init__(str(detail))
        self.kind = kind
        self.blame = FAILURE_KINDS[kind][0]
        self.status_code = status_code
        self.detail = detail
        self.retry_after_s = retry_after_s
        named = detail.get("parameter") if isinstance(detail, dict) else None
        self.parameter = parameter if parameter is not None else (
            named if isinstance(named, str) else None)
        self.rule = rule


class IntakeFailed(CallFailure):
    """Caller metadata cannot enter the call pipeline."""


class IdempotencyFailed(CallFailure):
    """An idempotency label conflicts with its stored use or active owner."""


class ResolutionFailed(CallFailure):
    """The requested tool or marketplace target cannot be resolved."""


class AuthorizationFailed(CallFailure):
    """A resolved call target is refused before any money is reserved."""


class ReservationFailed(CallFailure):
    """A metered call is refused before its reservation commits."""


class GatewayFailed(CallFailure):
    """The provider did not produce a complete response or treg refused the relay."""


class RequestBody(Protocol):
    def stream(self) -> AsyncIterator[bytes]: ...

    async def read(self) -> bytes: ...


class EmptyBody:
    """The body of a call treg makes itself with nothing to send: a provider listing, a task poll."""

    async def _nothing(self) -> AsyncIterator[bytes]:
        return
        yield b""

    def stream(self) -> AsyncIterator[bytes]:
        return self._nothing()

    async def read(self) -> bytes:
        return b""


@dataclass(frozen=True)
class UserSnapshot:
    id: int | None
    email: str


@dataclass(frozen=True)
class MembershipSnapshot:
    id: int | None
    user_id: int
    org_id: int
    role: str
    daily_call_cap: int
    tool_access: list | None
    project_access: list | None
    pinned_tags: dict | None


@dataclass(frozen=True)
class OrgSnapshot:
    id: int | None
    slug: str
    demo: bool
    public_demo: bool
    platform_overflow_disabled: bool
    budget_dims: list | None
    primary_dim: str
    daily_cap_micro: int
    autotopup_enabled: bool
    autotopup_consented_at: Any
    autotopup_threshold_micro: int
    autotopup_amount_micro: int
    autotopup_monthly_cap_micro: int
    first_call_at: Any
    autotopup_max_per_hour: int = 0
    archive_opt_out_at: Any = None   # set = the team opted out of the archive (archive.md)
    created_at: Any = None


@dataclass(frozen=True)
class CallerSnapshot:
    membership: MembershipSnapshot
    user: UserSnapshot
    org: OrgSnapshot
    api_key_id: int | None = None
    api_key_name: str | None = None
    api_key_prefix: str | None = None
    api_key_generation: int | None = None

    @property
    def org_id(self) -> int:
        return self.membership.org_id

    @property
    def email(self) -> str:
        return self.user.email

    @property
    def role(self) -> str:
        return self.membership.role

    @classmethod
    def capture(cls, caller: Any) -> "CallerSnapshot":
        membership = caller.membership
        org = caller.org
        key = getattr(caller, "api_key", None)
        return cls(
            membership=MembershipSnapshot(
                id=membership.id,
                user_id=membership.user_id,
                org_id=membership.org_id,
                role=membership.role,
                daily_call_cap=membership.daily_call_cap,
                tool_access=(list(membership.tool_access)
                             if membership.tool_access is not None else None),
                project_access=(list(membership.project_access)
                                if membership.project_access is not None else None),
                pinned_tags=(dict(membership.pinned_tags)
                             if membership.pinned_tags is not None else None),
            ),
            user=UserSnapshot(id=caller.user.id, email=caller.user.email),
            org=OrgSnapshot(
                id=org.id,
                slug=org.slug,
                demo=org.demo,
                public_demo=org.public_demo,
                platform_overflow_disabled=bool(getattr(org, "platform_overflow_disabled", False)),
                budget_dims=list(org.budget_dims) if org.budget_dims is not None else None,
                primary_dim=org.primary_dim,
                daily_cap_micro=org.daily_cap_micro,
                autotopup_enabled=org.autotopup_enabled,
                autotopup_consented_at=org.autotopup_consented_at,
                autotopup_threshold_micro=org.autotopup_threshold_micro,
                autotopup_amount_micro=org.autotopup_amount_micro,
                autotopup_monthly_cap_micro=org.autotopup_monthly_cap_micro,
                autotopup_max_per_hour=int(getattr(org, 'autotopup_max_per_hour', 0) or 0),
                archive_opt_out_at=getattr(org, 'archive_opt_out_at', None),
                first_call_at=org.first_call_at,
                created_at=getattr(org, "created_at", None),
            ),
            api_key_id=key.id if key else None,
            api_key_name=key.name if key else None,
            api_key_prefix=key.safe_prefix if key else None,
            api_key_generation=key.default_generation if key else None,
        )


@dataclass(frozen=True)
class CallInput:
    method: str
    raw_rest: str
    raw_headers: tuple[tuple[bytes, bytes], ...]
    query_items: tuple[tuple[str, str], ...]
    raw_query: str
    body: RequestBody
    caller: CallerSnapshot
    client_ip: str
    # The reviewed /catalog/call surface accepts only a catalog id — team tools never shadow it.
    catalog_only: bool = False
    # Set on every child call of a hub run: a child never resolves to a hub tool (no nested
    # runs, no undisclosed second price).
    child_of: str | None = None


class FinalizationState(Enum):
    NONE = "none"
    PENDING = "pending"
    OPEN = "open"
    FINALIZING = "finalizing"
    FINALIZED = "finalized"


@dataclass
class CallContext:
    input: CallInput
    call_ref: str
    meta: Any
    idempotency: tuple[int, str] | None = None
    target: Any = None
    marketplace: Any = None
    credentials: dict[int, Any] | None = None
    finalization: FinalizationState = FinalizationState.NONE
    audited: bool = False
    cached: bool = False
    cost_micro: int | None = None
    # Set by a routed parent on each child: the child leaves its hold OPEN here instead of settling,
    # and the parent charges or releases every one at the end (route.py `_close_deferred`).
    deferred_settles: list | None = None
    quota_admissions: set[tuple[int, str]] = field(default_factory=set)
    quota_pending: tuple[tuple[int, str], ...] = ()
    # Scalar snapshots for root request logs; never resolve a target just for telemetry.
    tool_name: str | None = None
    endpoint_id: str | None = None
    replay_of: str | None = None
    provider: str | None = None
    credential_tier: str | None = None
    refused_by: str | None = None
    # A routed parent's outcome for its call event (route.py `_summarize`): identity, output,
    # served_by, tried_count, hit, verdict, check_verdict. Never read by money or relay.
    route_summary: dict | None = None
    # The request left for the provider (the relay's `ON_SEND`, past every local check), or its
    # archived answer was served.
    # A routed parent attributes its order only to children that did (route.py `_note_call`).
    reached_provider: bool = False


@dataclass(frozen=True)
class UpstreamRequest:
    method: str
    raw_headers: tuple[tuple[bytes, bytes], ...]
    query_items: tuple[tuple[str, str], ...]
    body_stream: Callable[[], AsyncIterator[bytes]]
    has_body: bool
    # JSON credential bindings are the one body rewrite the relay permits. HTTP callers expose a
    # cached read so that provider explicitly opting into that shape can inject without consuming
    # the stream twice; ordinary header/query providers leave this unset and keep streaming.
    body_read: Callable[[], Awaitable[bytes]] | None = None


@dataclass(frozen=True)
class IdempotentReplay:
    body: bytes
    status_code: int
    media_type: str
    charged_micro: int
    call_ref: str
    # `(key_hash, content_hash)` when the row dropped its own copy: the bytes are read by
    # `idempotency.resolve_archived_replay` AFTER the request's session closes (non-negotiable 3).
    archive: tuple[str, str] | None = None


@dataclass(frozen=True)
class ResolvedTarget:
    tool: Tool
    upstream: str


@dataclass
class UpstreamResponse:
    status: int
    raw_headers: tuple[tuple[bytes, bytes], ...]
    body_stream: AsyncIterator[bytes]
    close: Callable[[], Awaitable[None]]
