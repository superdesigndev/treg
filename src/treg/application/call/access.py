"""Catalog endpoint access decisions.

The HTTP router translates this use case. It does not decide how grants, tools, credentials, or
platform service tiers satisfy an endpoint.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ... import oauth_providers
from ...config import get_settings
from ...domain import money as ledger
from ...domain.catalog import store as catalog_store
from ...domain.connections import authorization as connection_authorization
from ...domain.identity.access import Caller
from .resolve import (
    _authorization_method,
    _enforce_catalog_status,
    _marketplace_secret,
    _platform_estimate_micro,
    _platform_offer,
    _provider_tool_grant,
    _resolve_call,
    resolve_call_target,
)
from .route import RouteOptions, build_plan
from .types import CallFailure, ResolutionFailed


async def catalog_endpoint_access(
    *, endpoint_id: str, authorization_method: str, caller: Caller, db: AsyncSession,
) -> dict:
    """Return the catalog service tier and authorization that can serve one endpoint."""
    catalog = catalog_store.load()
    endpoint = catalog.by_id.get(endpoint_id)
    if endpoint is None:
        raise ResolutionFailed(
            "unknown_endpoint", status_code=404,
            detail=f"unknown endpoint {endpoint_id!r}",
        )
    _enforce_catalog_status(endpoint)
    service = endpoint["provider"]
    if endpoint.get("kind") == "routed":
        return await _routed_access(endpoint, caller, catalog)

    registry_provider = oauth_providers.get(service)
    if registry_provider is None or not registry_provider.base_url:
        return {"tier": "none", "detail": f"{service} isn't proxy-callable yet"}
    try:
        methods = connection_authorization.select_endpoint_methods(
            endpoint, authorization_method,
        )
    except ValueError as exc:
        raise ResolutionFailed(
            "catalog_parameter_invalid", status_code=400, detail=str(exc),
        ) from None
    provider = (
        registry_provider.profile_for_authorization(methods[0])
        if authorization_method.strip() and methods else registry_provider
    )
    billed_note = _billed_note(endpoint, provider, service, catalog)

    if methods:
        try:
            grant = await _provider_tool_grant(service, methods, caller, db)
        except CallFailure as exc:
            if exc.status_code == 403:
                return {
                    "tier": "restricted",
                    "detail": (
                        "a connected account exists but your access is restricted — ask an admin"
                    ),
                }
            raise
        if grant is not None:
            tool, _, grant_method = grant
            return {
                "tier": "tool",
                "authorization_method": grant_method,
                "metered": bool(billed_note),
                "detail": f"will use this org's registered {tool.name!r} tool{billed_note}",
            }
        secret = await _marketplace_secret(service, caller.org_id, db, methods)
        if secret is not None:
            return {
                "tier": "credential",
                "authorization_method": _authorization_method(secret),
                "metered": bool(billed_note),
                "detail": f"will use this org's {service} credential (no tool needed){billed_note}",
            }
    else:
        direct = await _direct_access(endpoint, provider, service, caller, db, billed_note)
        if direct is not None:
            return direct

    cost = _platform_offer(endpoint, provider, caller.org)
    if cost is not None:
        # The number is the honest per-call price at the DEFAULT page size — a `per_result`
        # endpoint costs more or less depending on how many rows the caller asks for, so it is "~".
        estimate = _platform_estimate_micro(cost, {})
        low = cost.get("usd_min")  # a price table: the figure depends on model/resolution/duration
        if isinstance(low, (int, float)) and low < ledger.usd(estimate):
            price = (f"${low:g}-${ledger.usd(estimate):g} by model, resolution and duration (the "
                     f"matching rate-card row is held; you pay the provider's reported cost, which "
                     f"can exceed it)" if cost.get("settle") == "usage" else
                     f"${low:g}-${ledger.usd(estimate):g} by model, resolution and duration (reserved "
                     f"at the table row your request matches)")
        else:
            price = f"~${ledger.usd(estimate):g}/call"
        return {
            "tier": "platform",
            "detail": (f"no key needed — uses treg's {service} key, {price} "
                       f"from your team balance (treg balance)"),
            "estimated_cost_micro": estimate,
            "estimated_cost_usd": ledger.usd(estimate),
            **({"estimated_cost_usd_min": low} if isinstance(low, (int, float)) else {}),
        }
    return _missing_access(endpoint, registry_provider, provider, methods, service)


async def _routed_access(endpoint: dict, caller: Caller, catalog) -> dict:
    options = RouteOptions.from_headers(lambda key: None)
    plan = await build_plan(
        endpoint, dict(endpoint.get("test_request", {}).get("body") or {}), caller, options,
    )
    if not plan.candidates:
        contract = catalog.contracts.get(endpoint.get("capability") or "")
        for variant in (contract.identity if contract else []):
            trial = await build_plan(
                endpoint, {key: "example" for key in variant}, caller, options,
            )
            if trial.candidates:
                plan = trial
                break
    if not plan.candidates:
        return {
            "tier": "none",
            "detail": (
                "no provider can serve any identity shape of this job for your team right now"
            ),
            "dropped": plan.dropped,
        }
    first = plan.candidates[0]
    how = (
        "your registered tool" if first.tier == "tool" else
        "your own credential" if first.tier == "credential" else
        f"treg's {first.endpoint['provider']} key, ~${(first.price_micro or 0) / 1e6:g}"
    )
    dropped_note = ""
    if plan.dropped:
        dropped_note = (
            "; for this {" + ", ".join(plan.variant) + "} example, not usable: "
            + ", ".join(
                f"{item['endpoint_id']} ({item['why']})" for item in plan.dropped
            )
        )
    return {
        "tier": "routed",
        "detail": (
            f"routed — {len(plan.candidates)} providers callable now; first: "
            f"{first.endpoint['id']} on {how} (send {{{', '.join(first.variant)}}})"
            + dropped_note
        ),
        "plan": [candidate.view() for candidate in plan.candidates],
        "dropped": plan.dropped,
    }


def _billed_note(endpoint: dict, provider, service: str, catalog) -> str:
    if not (provider.platform_billed and service in get_settings().oauth_billed_set):
        return ""
    cost = catalog.cost_view(endpoint.get("cost"), service) if endpoint.get("cost") else None
    estimate = _platform_estimate_micro(cost, {}) if cost and cost.get("usd") else 0
    if estimate:
        return (
            f" — metered from the team balance (~${ledger.usd(estimate):g}/call: "
            f"{service} bills treg's app per use)"
        )
    return f" — metered from the team balance ({service} bills treg's app per use)"


async def _direct_access(
    endpoint: dict, provider, service: str, caller: Caller, db: AsyncSession,
    billed_note: str,
) -> dict | None:
    probe = provider.base_url.rstrip("/") + "/" + (endpoint["path"] or "/").lstrip("/")
    try:
        target = await resolve_call_target(probe, caller, _resolve_call)
        return {
            "tier": "tool",
            "metered": bool(billed_note),
            "detail": (
                f"will use this org's registered {target.tool.name!r} tool{billed_note}"
            ),
        }
    except CallFailure as exc:
        if exc.status_code == 403:
            return {
                "tier": "restricted",
                "detail": "a registered tool exists but your access is restricted — ask an admin",
            }
        if exc.status_code != 404:
            raise
    if await _marketplace_secret(service, caller.org_id, db) is not None:
        return {
            "tier": "credential",
            "metered": bool(billed_note),
            "detail": f"will use this org's {service} credential (no tool needed){billed_note}",
        }
    return None


def _missing_access(
    endpoint: dict, registry_provider, provider, methods: tuple[str, ...], service: str,
) -> dict:
    specification = (
        connection_authorization.method_spec(registry_provider, methods[0]) if methods else None
    )
    capability = specification.connect_capability if specification else ""
    connect = f"treg connections connect --provider {service}"
    if capability:
        connect += f" --capability {capability}"
    hint = (
        f"connect with: {connect}" if not provider.uses_pasted_secret else
        f"connect with: {connect}, or treg secret add {service} …"
    )
    return {
        "tier": "none",
        "authorization_method": methods[0] if methods else "",
        "connect_capability": capability,
        "connect_command": connect,
        "action_label": specification.action_label if specification else "",
        "missing_message": specification.missing_message if specification else "",
        "detail": f"no {service} credential in this org yet — {hint}",
    }
