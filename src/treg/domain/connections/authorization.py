"""Authorization-method rules for provider connections.

The provider registry supplies data. This module owns the reusable decisions for providers that
offer more than one grant protocol. It has no HTTP or dashboard dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class AuthorizationMethod:
    """One explicit grant method for a logical provider."""

    name: str
    display_name: str
    capabilities: tuple[str, ...]
    connection_name: str
    description: str
    connect_capability: str = ""
    action_label: str = "Add account"
    missing_message: str = ""
    capability_intros: tuple[tuple[str, str], ...] = ()
    capability_details: tuple[tuple[str, tuple[str, ...]], ...] = ()
    capability_labels: tuple[tuple[str, str], ...] = ()
    capability_help: tuple[tuple[str, str], ...] = ()
    capability_action_labels: tuple[tuple[str, str], ...] = ()
    review_key: str = ""
    review_notices: tuple[tuple[str, str], ...] = ()
    review_capability_rollouts: tuple[tuple[str, str, str], ...] = ()
    capability_review_help: tuple[tuple[str, str], ...] = ()
    scope_aliases: tuple[tuple[str, str], ...] = ()
    scope_riders: tuple[str, ...] = ()
    scope_riders_by_scope: tuple[tuple[str, str], ...] = ()
    overrides: tuple[tuple[str, object], ...] = ()


def method_for_capability(provider: Any, capability: str) -> AuthorizationMethod | None:
    """Return the grant method that owns a capability."""
    matches = [method for method in provider.authorization_methods
               if capability in method.capabilities]
    if len(matches) > 1:
        raise ValueError(
            f"{provider.service} capability {capability!r} has multiple authorization methods"
        )
    if not matches:
        if provider.authorization_methods:
            raise ValueError(
                f"{provider.service} capability {capability!r} has no authorization method"
            )
        return None
    return matches[0]


def method_name(provider: Any, stored: str) -> str:
    """Normalize a stored method, including the provider's declared legacy value."""
    if stored or not provider.authorization_methods:
        return stored
    return provider.legacy_authorization_method or method_for_capability(
        provider, provider.default_capability
    ).name


def provider_profile(provider: Any, method: str) -> Any:
    """Return the protocol profile for one grant method."""
    if not provider.authorization_methods:
        return provider
    name = method_name(provider, method)
    selected = next(
        (item for item in provider.authorization_methods if item.name == name), None
    )
    if selected is None:
        raise ValueError(f"{provider.service} has no authorization method {name!r}")
    return replace(
        provider,
        **dict(selected.overrides),
        authorization_methods=(),
        default_capability_name="",
        connect_default_capability_name="",
    )


def endpoint_methods(endpoint: dict) -> tuple[str, ...]:
    """Return supported methods with the endpoint default first."""
    methods = endpoint.get("authorization_methods") or []
    if not methods and endpoint.get("authorization_method"):
        methods = [endpoint["authorization_method"]]
    default = endpoint.get("authorization_method") or ""
    if not default:
        return tuple(methods)
    return tuple([default] + [method for method in methods if method != default])


def select_endpoint_methods(
    endpoint: dict, requested: str,
) -> tuple[str, ...]:
    """Validate an optional caller choice and return the methods to try in order."""
    methods = endpoint_methods(endpoint)
    selected = requested.strip().lower()
    if not selected:
        return methods
    if not methods:
        raise ValueError(
            f"{endpoint['id']} does not support authorization-method selection"
        )
    if selected not in methods:
        raise ValueError(
            f"{endpoint['id']} does not support {selected}; choose " + " or ".join(methods)
        )
    return (selected,)


def method_spec(provider: Any, method: str) -> AuthorizationMethod | None:
    """Return presentation and scope metadata for one grant method."""
    return next(
        (item for item in provider.authorization_methods if item.name == method), None
    )


def required_scopes(endpoint: dict, method: AuthorizationMethod | None) -> list[str]:
    """Translate endpoint scopes into the selected grant's scope dialect."""
    declared = list(endpoint.get("required_scopes") or [])
    if method is None:
        return declared
    aliases = dict(method.scope_aliases)
    result = [aliases.get(scope, scope) for scope in declared]
    result.extend(method.scope_riders)
    result.extend(
        rider for source, rider in method.scope_riders_by_scope if source in declared
    )
    return list(dict.fromkeys(result))


def connect_capability(provider: Any, endpoint: dict, method: AuthorizationMethod | None) -> str:
    """Return the smallest declared capability that satisfies one endpoint.

    A grant method can have an approved core tier and a wider tier that is still under review.
    Setup guidance must name the endpoint's real tier instead of always naming the method default.
    """
    eligible = method.capabilities if method else tuple(provider.capabilities)
    required = set(required_scopes(endpoint, method))
    candidates = [
        capability for capability in eligible
        if required <= set(provider.scopes.get(capability, ()))
    ]
    if candidates:
        return min(candidates, key=lambda capability: len(provider.scopes[capability]))
    if method and method.connect_capability:
        return method.connect_capability
    return str(endpoint.get("authorization_capability") or "")


def action_label(method: AuthorizationMethod | None, capability: str) -> str:
    if method is None:
        return "Add account"
    return dict(method.capability_action_labels).get(capability, method.action_label)


def description(method: AuthorizationMethod, pending: frozenset[str]) -> str:
    """Return method copy with notices for only the reviews that remain pending."""
    notices = [text for key, text in method.review_notices if key in pending]
    return " ".join((method.description, *notices)).strip()


def capability_help(
    method: AuthorizationMethod, capability: str, pending: frozenset[str],
) -> str:
    """Return normal help, replaced by review help while its gate is pending."""
    rollout_key = next(
        (key for key, gated, _fallback in method.review_capability_rollouts
         if gated == capability),
        "",
    )
    if rollout_key in pending:
        review_help = dict(method.capability_review_help).get(capability, "")
        if review_help:
            return review_help
    return dict(method.capability_help).get(capability, "")


def connect_capabilities(
    method: AuthorizationMethod, pending: frozenset[str],
) -> tuple[str, ...]:
    """Return new-grant choices, with review fallbacks only while needed."""
    capabilities = list(method.capabilities)
    for key, _capability, fallback in method.review_capability_rollouts:
        if key not in pending and fallback in capabilities:
            capabilities.remove(fallback)
    return tuple(capabilities)


def method_connect_capability(
    provider: Any, method: AuthorizationMethod, pending: frozenset[str],
) -> str:
    """Return one method's effective default under the current review state."""
    eligible = list(connect_capabilities(method, pending))
    gated_pending = {
        capability for key, capability, _fallback in method.review_capability_rollouts
        if key in pending
    }
    approved = [capability for capability in eligible if capability not in gated_pending]
    if method.connect_capability and method.connect_capability in approved:
        return method.connect_capability
    preferred = provider.default_capability
    if preferred in approved:
        return preferred
    return max(approved, key=lambda cap: len(provider.scopes.get(cap, ())), default=preferred)


def permission_capabilities(provider: Any, pending: frozenset[str]) -> tuple[str, ...]:
    """Return permission-card capabilities for the current review state."""
    capabilities = list(provider.capabilities)
    for method in provider.authorization_methods:
        for key, _capability, fallback in method.review_capability_rollouts:
            if key not in pending and fallback in capabilities:
                capabilities.remove(fallback)
    return tuple(capabilities)


def capability_presentation(
    method: AuthorizationMethod, pending: frozenset[str],
) -> tuple[dict[str, str], dict[str, str], dict[str, list[str]]]:
    """Merge an approved wider capability into its former fallback permission card."""
    labels = dict(method.capability_labels)
    intros = dict(method.capability_intros)
    details = {capability: list(items) for capability, items in method.capability_details}
    for key, capability, fallback in method.review_capability_rollouts:
        if key in pending:
            continue
        if fallback in labels:
            labels[capability] = labels[fallback]
        if fallback in intros:
            intros[capability] = intros[fallback]
        details[capability] = [
            *details.get(fallback, []),
            *details.get(capability, []),
        ]
    return labels, intros, details
