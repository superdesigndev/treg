"""Framework-neutral call application contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Literal, Protocol

if TYPE_CHECKING:
    from ...models import Tool


Blame = Literal["caller", "treg", "upstream", "org_connection"]

_BLAME_BY_KIND: dict[str, Blame] = {
    "metadata_invalid": "caller",
    "metadata_pin_mismatch": "caller",
    "idempotency_mismatch": "caller",
    "idempotency_in_progress": "treg",
    "invalid_target": "caller",
    "tool_access_denied": "caller",
    "target_not_found": "caller",
    "target_ambiguous": "caller",
    "catalog_retired": "caller",
    "catalog_parameter_invalid": "caller",
    "async_resource_not_owned": "caller",
    "capability_pinned": "caller",
    "policy_denied": "caller",
    "daily_cap_reached": "caller",
    "public_demo_rate_limited": "caller",
    "trial_allowance_unavailable": "treg",
    "trial_allowance_reached": "caller",
    "platform_cap_unavailable": "treg",
    "platform_daily_cap_reached": "caller",
    "tag_budget_unavailable": "treg",
    "tag_cardinality_exceeded": "caller",
    "tag_blocked": "caller",
    "tag_call_cap_reached": "caller",
    "tag_spend_cap_reached": "caller",
    "insufficient_balance": "caller",
    # treg's OWN vendor account for the provider is out (balance/quota) — a 503 the caller cannot
    # fix, answered before any hold exists, with the same-capability alternatives named.
    "provider_capacity": "treg",
    # Routed endpoints (treg.<capability>): the caller's identity fits no provider, or the
    # ceiling they set is below the cheapest candidate, or every candidate failed.
    "route_no_candidate": "caller",
    "route_max_cost": "caller",
    "route_failed": "upstream",
    "route_caller_fault": "caller",
    "injection_failed": "treg",
    "ssrf_refused": "treg",
    "connect_failed": "upstream",
    "read_timeout": "upstream",
    "stream_interrupted": "upstream",
    "refresh_failed": "org_connection",
    "credential_missing": "org_connection",
    "authorization_required": "org_connection",
    "method_mismatch": "caller",
}


class CallFailure(Exception):
    """A call failure translated once by the HTTP adapter."""

    def __init__(
        self,
        kind: str,
        *,
        status_code: int,
        detail: str | dict,
    ) -> None:
        super().__init__(str(detail))
        self.kind = kind
        self.blame = _BLAME_BY_KIND[kind]
        self.status_code = status_code
        self.detail = detail


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


@dataclass(frozen=True)
class CallerSnapshot:
    membership: MembershipSnapshot
    user: UserSnapshot
    org: OrgSnapshot

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
                first_call_at=org.first_call_at,
            ),
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
    cost_micro: int | None = None


@dataclass(frozen=True)
class UpstreamRequest:
    method: str
    raw_headers: tuple[tuple[bytes, bytes], ...]
    query_items: tuple[tuple[str, str], ...]
    body_stream: Callable[[], AsyncIterator[bytes]]
    has_body: bool


@dataclass(frozen=True)
class IdempotentReplay:
    body: bytes
    status_code: int
    media_type: str
    charged_micro: int
    call_ref: str


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
