"""Executable contract for the Stage 4 call application boundary."""

import ast
import importlib
from dataclasses import dataclass, fields
from inspect import signature
from typing import Awaitable, Callable, Literal, Protocol

import pytest

from treg.application.call import authorize, evidence, idempotency, intake, reserve, resolve, service, settle
from treg.application.call.types import (
    AuthorizationFailed,
    CallContext,
    CallFailure,
    CallInput,
    IdempotencyFailed,
    IntakeFailed,
    ResolvedTarget,
    ResolutionFailed,
    ReservationFailed,
    UpstreamResponse,
)


class RequestBodyPort(Protocol):
    def stream(self): ...
    async def read(self) -> bytes: ...


@dataclass(frozen=True)
class CallInputContract:
    method: str
    raw_rest: str
    raw_headers: tuple[tuple[bytes, bytes], ...]
    query_items: tuple[tuple[str, str], ...]
    raw_query: str
    body: RequestBodyPort
    caller: object
    client_ip: str
    catalog_only: bool = False


@dataclass
class UpstreamResponseContract:
    status: int
    raw_headers: tuple[tuple[bytes, bytes], ...]
    body_stream: object
    close: Callable[[], Awaitable[None]]


Blame = Literal["caller", "treg", "upstream", "org_connection"]


GATEWAY_FAILURES: dict[str, tuple[Blame, str, int, str]] = {
    "refresh_failed": ("org_connection", "call_failed_502", 502, "1"),
    "injection_failed": ("treg", "call_failed_502", 502, "1"),
    "ssrf_refused": ("treg", "call_failed_502", 502, "1"),
    "connect_failed": ("upstream", "call_failed_502", 502, "1"),
    "read_timeout": ("upstream", "call_failed_502", 502, "1"),
    "stream_interrupted": ("upstream", "call_failed_502", 502, "1"),
}


FINALIZATION_TABLE = {
    "pre_reserve_refusal": ("none", "release_claim", "none"),
    "cancel_after_claim": ("none", "release_claim_shielded", "none"),
    "insufficient_balance": ("rollback", "release_claim", "none"),
    "cancel_during_reserve": ("release_call_ref_shielded", "release_claim_shielded", "close_once"),
    "ssrf_refused": ("release", "release_claim", "none"),
    "gateway_failure": ("release_call_failed", "release_claim", "close_once"),
    "unexpected_failure": ("release_call_crashed", "release_claim", "close_once"),
    "cancel_after_reserve": ("release_call_cancelled", "release_claim", "close_once"),
    "upstream_2xx": ("settle", "store_metered_replay", "close_once"),
    "billable_upstream_4xx": ("settle", "release_claim", "close_once"),
    "upstream_5xx": ("release_provider_failed", "release_claim", "close_once"),
    "other_nonbillable": ("release_not_billable", "release_claim", "close_once"),
    "unmetered_response": ("none", "current_idempotency_rule", "close_once"),
}


def test_call_dto_and_port_shapes_are_frozen() -> None:
    assert [field.name for field in fields(CallInputContract)] == [
        "method", "raw_rest", "raw_headers", "query_items", "raw_query", "body", "caller",
        "client_ip",
        "catalog_only",
    ]
    assert [field.name for field in fields(UpstreamResponseContract)] == [
        "status", "raw_headers", "body_stream", "close",
    ]
    assert [field.name for field in fields(UpstreamResponse)] == [
        "status", "raw_headers", "body_stream", "close",
    ]
    assert [field.name for field in fields(CallInput)] == [
        field.name for field in fields(CallInputContract)
    ]
    assert [field.name for field in fields(CallContext)] == [
        "input", "call_ref", "meta", "idempotency", "target", "marketplace",
        "credentials", "finalization", "audited", "cost_micro",
    ]
    assert CallInputContract.__dataclass_params__.frozen is True
    assert CallInput.__dataclass_params__.frozen is True


def test_failure_table_pins_every_terminal_path() -> None:
    assert set(FINALIZATION_TABLE) == {
        "pre_reserve_refusal", "cancel_after_claim", "insufficient_balance",
        "cancel_during_reserve", "ssrf_refused", "gateway_failure", "unexpected_failure",
        "cancel_after_reserve", "upstream_2xx", "billable_upstream_4xx", "upstream_5xx",
        "other_nonbillable", "unmetered_response",
    }
    assert FINALIZATION_TABLE["billable_upstream_4xx"][1] == "release_claim"
    assert FINALIZATION_TABLE["cancel_after_reserve"] == (
        "release_call_cancelled", "release_claim", "close_once")
    assert all(close in {"none", "close_once"} for _, _, close in FINALIZATION_TABLE.values())


def test_gateway_failure_mapping_is_one_source_of_truth() -> None:
    assert set(GATEWAY_FAILURES) == {
        "refresh_failed", "injection_failed", "ssrf_refused", "connect_failed", "read_timeout",
        "stream_interrupted",
    }
    assert all(reason == "call_failed_502" for _, reason, _, _ in GATEWAY_FAILURES.values())
    assert all(status == 502 for _, _, status, _ in GATEWAY_FAILURES.values())
    assert all(header == "1" for _, _, _, header in GATEWAY_FAILURES.values())
    assert GATEWAY_FAILURES["refresh_failed"][0] == "org_connection"
    assert GATEWAY_FAILURES["ssrf_refused"][0] == "treg"


@pytest.mark.parametrize(
    ("status", "cost_type", "billable"),
    [
        (200, "per_call", True),
        (400, "per_call", True),
        (404, "per_call", True),
        (401, "per_call", False),
        (402, "per_call", False),
        (403, "per_call", False),
        (405, "per_call", False),
        (407, "per_call", False),
        (408, "per_call", False),
        (429, "per_call", False),
        (500, "per_call", False),
        (200, "per_result", True),
        (400, "per_result", False),
        (500, "per_result", False),
    ],
)
def test_provider_responses_are_data_and_billability_is_independent(
    status: int, cost_type: str, billable: bool,
) -> None:
    assert settle._platform_billable(status, cost_type) is billable


def test_compatibility_surface_stays_literal_during_boundary_extraction() -> None:
    assert {mapping[3] for mapping in GATEWAY_FAILURES.values()} == {"1"}
    assert FINALIZATION_TABLE["ssrf_refused"][0] == "release"
    assert FINALIZATION_TABLE["upstream_2xx"][0] == "settle"


def test_call_application_modules_are_framework_neutral() -> None:
    for module in (authorize, evidence, idempotency, intake, reserve, resolve, service, settle):
        source = module.__loader__.get_source(module.__name__)
        roots = {
            node.module.split(".", 1)[0]
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.module
        } | {
            alias.name.split(".", 1)[0]
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert not ({"fastapi", "starlette"} & roots)


def test_upstream_relay_is_framework_neutral() -> None:
    relay_module = importlib.import_module("treg.infra.upstream.relay")
    source = relay_module.__loader__.get_source(relay_module.__name__)
    roots = {
        node.module.split(".", 1)[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name.split(".", 1)[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not ({"fastapi", "starlette"} & roots)


async def test_authorization_gate_order_is_frozen(monkeypatch) -> None:
    order = []

    class Session:
        async def commit(self):
            order.append("commit")

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(authorize, "session_maker", SessionContext)
    monkeypatch.setattr(
        authorize.access_policy, "_require_tool_use", lambda caller, tool: order.append("acl"))

    async def deny(*args):
        order.append("deny")

    async def daily(*args, **kwargs):
        order.append("daily")

    async def public(*args):
        order.append("public")

    monkeypatch.setattr(authorize.access_policy, "enforce_deny", deny)
    monkeypatch.setattr(authorize.usage_policy, "enforce_daily_cap", daily)
    monkeypatch.setattr(authorize.publicdemo_policy, "enforce_public_demo_ip_cap", public)
    monkeypatch.setattr(authorize.demo_sandbox, "is_sandbox", lambda org: False)
    caller = type("Caller", (), {
        "role": "member",
        "org": type("Org", (), {"public_demo": True})(),
    })()
    tool = type("Tool", (), {"project_id": None})()

    await authorize.authorize_call(
        caller=caller, tool=tool, upstream_url="https://upstream.test/x",
        method="GET", client_ip="203.0.113.1")

    assert order == ["acl", "deny", "daily", "public", "commit"]


def test_authorization_failure_keeps_mechanism_and_blame_separate() -> None:
    exc = AuthorizationFailed(
        "policy_denied", status_code=403, detail="blocked by policy")
    assert (exc.kind, exc.blame, exc.status_code, exc.detail) == (
        "policy_denied", "caller", 403, "blocked by policy")


def test_reservation_failure_keeps_mechanism_and_blame_separate() -> None:
    short = ReservationFailed(
        "insufficient_balance", status_code=402, detail={"error": "insufficient_balance"})
    unavailable = ReservationFailed(
        "platform_cap_unavailable", status_code=429, detail="retry")
    assert (short.blame, unavailable.blame) == ("caller", "treg")


def test_intake_failures_keep_mechanism_and_blame_separate() -> None:
    with pytest.raises(IntakeFailed) as malformed:
        intake._parse_call_meta("not-a-pair")
    assert (malformed.value.kind, malformed.value.blame, malformed.value.status_code) == (
        "metadata_invalid", "caller", 422)

    waiting = IdempotencyFailed(
        "idempotency_in_progress", status_code=409, detail="wait")
    mismatch = IdempotencyFailed(
        "idempotency_mismatch", status_code=422, detail="new key")
    assert waiting.blame == "treg"
    assert mismatch.blame == "caller"


def test_resolution_result_and_failures_are_framework_neutral() -> None:
    assert [field.name for field in fields(ResolvedTarget)] == ["tool", "upstream"]
    assert "blame" not in signature(CallFailure).parameters
    assert ResolutionFailed(
        "target_not_found", status_code=404, detail="missing").blame == "caller"
    assert ResolutionFailed(
        "credential_missing", status_code=404, detail="connect").blame == "org_connection"
    assert ResolutionFailed(
        "injection_failed", status_code=502, detail="configuration").blame == "treg"
