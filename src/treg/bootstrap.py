"""FastAPI composition root and deployment-role manifests."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import asynccontextmanager
from copy import copy
from typing import Literal

import httpx
from fastapi import FastAPI
from fastapi.routing import APIRoute, request_response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import BaseRoute, Mount

from . import adsconv, analytics, archive, audit
from .application.call import route as routed_call
from . import bootstrap_handlers
from .bootstrap_http import (
    _BodyDecodeMiddleware,
    _LegacyHostRedirectMiddleware,
    _SecurityHeadersMiddleware,
)
from .config import get_settings
from .infra.db import session_maker, verify_db
from .infra.catalog_observations import (
    CachedEndpointObservationReader,
    PostgresEndpointObservationReader,
)
from .routers import call as call_routes


AppRole = Literal["all", "dataplane", "control"]
RouteKey = tuple[str, tuple[str, ...], str]

# Every HTTP route has one workload owner. A new decorator in api.py fails app creation until its
# key is placed here, so the dataplane cannot silently acquire a management or runner endpoint.
_CONTROL_ROUTE_KEYS: frozenset[RouteKey] = frozenset({
    ('/meta', ('GET',), 'meta'),
    ('/providers.json', ('GET',), 'providers_catalog'),
    ('/catalog/platforms', ('GET',), 'catalog_platforms'),
    ('/catalog/platforms/{slug}', ('GET',), 'catalog_platform'),
    ('/catalog/search', ('GET',), 'catalog_search'),
    ('/catalog/endpoints/{endpoint_id}', ('GET',), 'catalog_endpoint'),
    ('/catalog/examples/{endpoint_id}', ('GET',), 'catalog_example'),
    ('/catalog', ('GET',), 'catalog_index'),
    ('/catalog/{slug}', ('GET',), 'catalog_page'),
    ('/agents', ('GET',), 'agents_hub'),
    ('/agents/{agent}', ('GET',), 'agent_page'),
    ('/agents/{agent}.md', ('GET',), 'agent_page'),
    ('/use-cases/{category}/{job}', ('GET',), 'use_case_job_page_nested'),
    ('/use-cases/{category}/{job}.md', ('GET',), 'use_case_job_page_nested'),
    ('/use-cases/{job}', ('GET',), 'use_case_job_page'),
    ('/use-cases/{job}.md', ('GET',), 'use_case_job_page'),
    ('/use-cases', ('GET',), 'use_cases_hub'),
    ('/workflows/{slug}.csv', ('GET',), 'workflow_csv'),
    ('/workflows/{slug}', ('GET',), 'workflow_page'),
    ('/workflows/{slug}.md', ('GET',), 'workflow_page'),
    ('/workflows', ('GET',), 'workflows_hub'),
    ('/catalog.css', ('GET',), 'catalog_css'),
    ('/tools/{service}', ('GET',), 'tools_provider'),
    ('/pricing', ('GET',), 'pricing_page'),
    ('/sitetrack.js', ('GET',), 'sitetrack_js'),
    ('/docs', ('GET',), 'docs_page'),
    ('/tool-requests', ('POST',), 'create_tool_request'),
    ('/auth/github', ('GET',), 'auth_github'),
    ('/auth/github/callback', ('GET',), 'auth_github_callback'),
    ('/auth/google', ('GET',), 'auth_google'),
    ('/auth/google/callback', ('GET',), 'auth_google_callback'),
    ('/auth/cli/start', ('POST',), 'auth_cli_start'),
    ('/auth/cli/poll', ('GET',), 'auth_cli_poll'),
    ('/auth/cli/orgs', ('GET',), 'auth_cli_orgs'),
    ('/auth/cli/approve', ('POST',), 'auth_cli_approve'),
    ('/login', ('GET',), 'login_page'),
    ('/auth/me', ('GET',), 'auth_me'),
    ('/auth/logout', ('POST',), 'auth_logout'),
    ('/auth/email/start', ('POST',), 'auth_email_start'),
    ('/auth/email/verify', ('POST',), 'auth_email_verify'),
    ('/auth/invite-signin', ('GET',), 'auth_invite_signin'),
    ('/auth/invite-signin', ('POST',), 'auth_invite_signin_confirm'),
    ('/', ('GET',), 'landing'),
    ('/app', ('GET',), 'dashboard'),
    ('/app/marketplace/{service}', ('GET',), 'dashboard_marketplace'),
    ('/app/skills/{name}', ('GET',), 'dashboard_skill_page'),
    ('/app/tools/{name}', ('GET',), 'dashboard_tool_page'),
    ('/llms.txt', ('GET',), 'llms_txt'),
    ('/robots.txt', ('GET',), 'robots_txt'),
    ('/sitemap.xml', ('GET',), 'sitemap_xml'),
    ('/7c2e4a91b5d3f8e6treg2026.txt', ('GET',), 'indexnow_key'),
    ('/install.sh', ('GET',), 'install_sh'),
    ('/selfhost.sh', ('GET',), 'selfhost_sh'),
    ('/quickstart.md', ('GET',), 'quickstart_md'),
    ('/tutorial.md', ('GET',), 'tutorial_md'),
    ('/tutorial-import-shell.md', ('GET',), 'tutorial_import_shell_md'),
    ('/tutorial-access.md', ('GET',), 'tutorial_access_md'),
    ('/vendor-listing.md', ('GET',), 'vendor_listing_md'),
    ('/vendor-listing', ('GET',), 'vendor_listing_md'),
    ('/integrate.md', ('GET',), 'integrate_md'),
    ('/skill.md', ('GET',), 'skill_md'),
    ('/favicon.ico', ('GET',), 'favicon'),
    ('/favicon.svg', ('GET',), 'favicon'),
    ('/tutorial.js', ('GET',), 'tutorial_js'),
    ('/legal.css', ('GET',), 'legal_css'),
    ('/terms', ('GET',), 'terms_page'),
    ('/privacy', ('GET',), 'privacy_page'),
    ('/connectors/claude', ('GET',), 'claude_connector_page'),
    ('/adtrack.js', ('GET',), 'adtrack_js'),
    ('/resources', ('GET',), 'resources_page'),
    ('/grokbot', ('GET',), 'grokbot_page'),
    ('/fable', ('GET',), 'fable_page'),
    ('/people-search', ('GET',), 'people_search_page'),
    ('/usecase.css', ('GET',), 'usecase_css'),
    ('/oauth/register', ('POST',), 'oauth_register'),
    ('/oauth/authorize', ('GET',), 'oauth_authorize'),
    ('/oauth/authorize', ('POST',), 'oauth_authorize_approve'),
    ('/oauth/revoke', ('POST',), 'oauth_revoke'),
    ('/oauth/token', ('POST',), 'oauth_token'),
    ('/.well-known/oauth-protected-resource', ('GET',), 'oauth_protected_resource'),
    ('/.well-known/oauth-authorization-server', ('GET',), 'oauth_authorization_server'),
    ('/.well-known/openai-apps-challenge', ('GET',), 'openai_apps_challenge'),
    ('/.well-known/skills/index.json', ('GET',), 'well_known_skills_index'),
    ('/.well-known/skills/treg/SKILL.md', ('GET',), 'well_known_skill_md'),
    ('/connect-demo', ('GET',), 'connect_demo_page'),
    ('/connect-demo/callback', ('GET',), 'connect_demo_callback'),
    ('/help', ('GET',), 'support_page'),
    ('/contact', ('GET',), 'support_page'),
    ('/support', ('GET',), 'support_page'),
    ('/tutorial', ('GET',), 'tutorial_page'),
    ('/auth/cli-token', ('GET',), 'auth_cli_token'),
    ('/auth/revoke-tokens', ('POST',), 'auth_revoke_tokens'),
    ('/users', ('POST',), 'register_user'),
    ('/orgs', ('POST',), 'create_org'),
    ('/oauth/grants', ('GET',), 'oauth_grants'),
    ('/oauth/grants/{family_id}/team', ('POST',), 'oauth_grant_set_team'),
    ('/orgs', ('GET',), 'list_orgs'),
    ('/orgs/{org_id}/invites', ('POST',), 'create_invite'),
    ('/invites/accept', ('POST',), 'accept_invite'),
    ('/invites/mine', ('GET',), 'my_invites'),
    ('/onboard/demo', ('POST',), 'onboard_demo'),
    ('/onboard/skip', ('POST',), 'onboard_skip'),
    ('/onboard/reset', ('POST',), 'onboard_reset'),
    ('/demo/sandbox', ('POST',), 'demo_sandbox_mint'),
    ('/demo/sandbox/live', ('GET',), 'demo_sandbox_live'),
    ('/stripe/webhook', ('POST',), 'stripe_webhook'),
    ('/landing/stripe-feed', ('GET',), 'landing_stripe_feed'),
    ('/demo/sandbox/skill', ('GET',), 'demo_sandbox_skill'),
    ('/skills/samples', ('GET',), 'skill_samples'),
    ('/skills/{name}/install.sh', ('GET',), 'skill_install'),
    ('/onboard/seed-tool', ('POST',), 'onboard_seed_tool'),
    ('/onboard/accept-teammate', ('POST',), 'onboard_accept_teammate'),
    ('/invites/{invite_id}/accept', ('POST',), 'accept_my_invite'),
    ('/orgs/{org_id}/invites', ('GET',), 'list_invites'),
    ('/orgs/{org_id}/invites/{invite_id}', ('DELETE',), 'revoke_invite'),
    ('/orgs/{org_id}/members', ('GET',), 'list_members'),
    ('/orgs/{org_id}/usage', ('GET',), 'org_usage'),
    ('/orgs/{org_id}/balance', ('GET',), 'org_balance'),
    ('/orgs/{org_id}/tag-keys', ('GET',), 'list_tag_keys'),
    ('/orgs/{org_id}/usage/by-tag', ('GET',), 'usage_by_tag'),
    ('/orgs/{org_id}', ('PATCH',), 'rename_org'),
    ('/orgs/{org_id}/settings', ('GET',), 'get_org_settings'),
    ('/orgs/{org_id}/settings', ('PATCH',), 'set_org_settings'),
    ('/orgs/{org_id}/budgets', ('GET',), 'list_tag_budgets'),
    ('/orgs/{org_id}/budgets/{dim}', ('PUT',), 'set_tag_default'),
    ('/orgs/{org_id}/budgets/{dim}/{val}', ('PUT',), 'set_tag_budget'),
    ('/orgs/{org_id}/budgets/{dim}/{val}', ('DELETE',), 'delete_tag_budget'),
    ('/billing', ('GET',), 'billing_get'),
    ('/billing/topup', ('POST',), 'billing_topup'),
    ('/billing/autotopup', ('POST',), 'billing_autotopup'),
    ('/billing/history', ('GET',), 'billing_history'),
    ('/billing/portal', ('POST',), 'billing_portal'),
    ('/referrals', ('GET',), 'my_referrals'),
    ('/referrals/code', ('POST',), 'mint_referral_code'),
    ('/billing/stripe/webhook', ('POST',), 'billing_stripe_webhook'),
    ('/orgs/{org_id}/members/{user_id}/cap', ('PATCH',), 'set_member_cap'),
    ('/orgs/{org_id}/members/{user_id}/access', ('PATCH',), 'set_member_access'),
    ('/usage/me', ('GET',), 'my_usage'),
    ('/orgs/{org_id}/members/{user_id}', ('DELETE',), 'remove_member'),
    ('/orgs/{org_id}/members/{user_id}', ('PATCH',), 'set_member_role'),
    ('/orgs/{org_id}/leave', ('POST',), 'leave_org'),
    ('/orgs/{org_id}', ('DELETE',), 'delete_org'),
    ('/orgs/{org_id}/public-token', ('POST',), 'create_public_token'),
    ('/orgs/{org_id}/public-token', ('DELETE',), 'delete_public_token'),
    ('/orgs/{org_id}/agents', ('POST',), 'create_agent'),
    ('/orgs/{org_id}/agents', ('GET',), 'list_agents'),
    ('/agents/checkin', ('POST',), 'agent_checkin'),
    ('/orgs/{org_id}/agents/observed', ('GET',), 'list_observed_agents'),
    ('/orgs/{org_id}/agents/{user_id}', ('DELETE',), 'revoke_agent'),
    ('/orgs/{org_id}/projects', ('POST',), 'create_project'),
    ('/orgs/{org_id}/projects', ('GET',), 'list_projects'),
    ('/orgs/{org_id}/projects/{project_id}', ('DELETE',), 'delete_project'),
    ('/orgs/{org_id}/pins', ('GET',), 'list_capability_pins'),
    ('/orgs/{org_id}/pins', ('POST',), 'set_capability_pin'),
    ('/orgs/{org_id}/pins', ('DELETE',), 'clear_capability_pin'),
    ('/orgs/{org_id}/deny', ('POST',), 'create_deny_rule'),
    ('/orgs/{org_id}/deny', ('GET',), 'list_deny_rules'),
    ('/orgs/{org_id}/policy/cli-deny', ('GET',), 'list_cli_deny'),
    ('/orgs/{org_id}/deny/{rule_id}', ('DELETE',), 'delete_deny_rule'),
    ('/secrets', ('POST',), 'create_secret'),
    ('/secrets', ('GET',), 'list_secrets'),
    ('/secrets/{secret_id}', ('PATCH',), 'update_secret'),
    ('/secrets/{secret_id}', ('DELETE',), 'delete_secret'),
    ('/tools', ('POST',), 'create_tool'),
    ('/tools', ('GET',), 'list_tools'),
    ('/tools/by-name/{name}', ('GET',), 'get_tool_by_name'),
    ('/tools/{tool_id}', ('PATCH',), 'update_tool'),
    ('/tools/{tool_id}', ('DELETE',), 'delete_tool'),
    ('/tools/{name}/grant', ('POST',), 'grant_local_run'),
    ('/tools/{name}/run-report', ('POST',), 'report_local_run'),
    ('/skills', ('POST',), 'register_skill'),
    ('/skills/analyze', ('POST',), 'analyze_skill_folder'),
    ('/skills/import', ('POST',), 'import_skill_folder'),
    ('/bundles', ('GET',), 'list_bundles'),
    ('/bundles/by-name/{name}', ('GET',), 'get_bundle_by_name'),
    ('/bundles/{bundle_id}', ('GET',), 'get_bundle'),
    ('/bundles/{bundle_id}', ('PATCH',), 'update_bundle'),
    ('/bundles/{bundle_id}', ('DELETE',), 'delete_bundle'),
    ('/calls', ('GET',), 'list_calls'),
    ('/calls/{call_ref}', ('GET',), 'get_call'),
    ('/runs', ('GET',), 'list_runs'),
    ('/oauth/providers', ('GET',), 'oauth_providers_list'),
    ('/oauth/start', ('POST',), 'oauth_start'),
    ('/oauth/callback', ('GET',), 'oauth_callback'),
    ('/connections', ('GET',), 'list_connections'),
    ('/connections/{secret_id}/resources', ('GET',), 'connection_resources'),
    ('/connections/{secret_id}/resource', ('POST',), 'set_connection_resource'),
    ('/connections/token', ('POST',), 'connect_with_token'),
    ('/connections/{secret_id}/extra-credential', ('POST',), 'set_extra_credential'),
    ('/connections/{secret_id}', ('DELETE',), 'revoke_connection'),
    ('/oauth/status/{state}', ('GET',), 'oauth_status'),
    ('/health/run', ('POST',), 'run_health'),
    ('/health', ('GET',), 'get_health'),
    ('/admin/stats', ('GET',), 'admin_stats'),
    ('/admin/orgs', ('GET',), 'admin_orgs'),
    ('/admin/orgs/{org_id}', ('GET',), 'admin_org_detail'),
    ('/admin/users', ('GET',), 'admin_users'),
    ('/admin/tools', ('GET',), 'admin_tools'),
    ('/admin/calls', ('GET',), 'admin_calls'),
    ('/admin/errors', ('GET',), 'admin_errors'),
    ('/admin/health', ('GET',), 'admin_health'),
    ('/admin/users/{user_id}/superadmin', ('POST',), 'admin_set_superadmin'),
    ('/admin/users/{user_id}/suspend', ('POST',), 'admin_suspend_user'),
    ('/admin/users/{user_id}', ('DELETE',), 'admin_delete_user'),
    ('/admin/orgs/{org_id}/suspend', ('POST',), 'admin_suspend_org'),
    ('/admin/orgs/{org_id}', ('DELETE',), 'admin_delete_org'),
    ('/admin/orgs/{org_id}/credit', ('POST',), 'admin_credit_org'),
    ('/admin/reconcile/drift', ('GET',), 'admin_reconcile_drift'),
    ('/admin/reconcile/spend', ('GET',), 'admin_reconcile_spend'),
    ('/admin/reconcile/repeats', ('GET',), 'admin_reconcile_repeats'),
    ('/admin/archive', ('GET',), 'admin_archive'),
    ('/admin/archive/keys', ('GET',), 'admin_archive_keys'),
    ('/admin/archive/body', ('GET',), 'admin_archive_body'),
    ('/admin/archive/panel', ('GET',), 'admin_archive_panel'),
    ('/admin/referrals', ('GET',), 'admin_referrals'),
    ('/catalog/endpoints/{endpoint_id}/access', ('GET',), 'catalog_endpoint_access'),
    ('/run', ('POST',), 'run_tool_server'),
})
_DATAPLANE_ROUTE_KEYS: frozenset[RouteKey] = frozenset({
    ("/call/{rest:path}", ("DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"), "call_tool"),
    ("/catalog/call/{rest:path}",
     ("DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"),
     "call_catalog_endpoint"),
    # MCP is calling traffic, so its mount and its RFC 9728 resource metadata belong to the
    # dataplane. Token issuance (consent, /oauth/*) stays on control; the dataplane only validates.
    ('/.well-known/oauth-protected-resource/mcp', ('GET',), 'oauth_protected_resource'),
    ('/.well-known/oauth-protected-resource/mcp/v2', ('GET',), 'oauth_protected_resource_v2'),
})

ROLE_BACKGROUND_TASKS: dict[AppRole, tuple[str, ...]] = {
    "all": ("treg.adsconv.worker",),
    "dataplane": (),
    "control": ("treg.adsconv.worker",),
}
ROLE_STARTUP_CHECKS: dict[AppRole, tuple[str, ...]] = {
    "all": (
        "treg.infra.db.verify_db",
        "app.state.http",
        "treg.mcp.mcp_lifespan",
    ),
    "dataplane": (
        "treg.infra.db.verify_db",
        "app.state.http",
        "treg.mcp.mcp_lifespan",
    ),
    "control": (
        "treg.infra.db.verify_db",
        "app.state.http",
    ),
}

_STATIC_ANCHOR = ("/auth/cli-token", ("GET",), "auth_cli_token")

try:
    from . import mcp as _mcp
except Exception:  # pragma: no cover - exercised by deploys without the optional dependency
    _mcp = None


class _ImmutableStatic(StaticFiles):
    """Static files whose version-stamped names can be cached permanently."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def _route_key(route: APIRoute) -> RouteKey:
    return route.path, tuple(sorted(route.methods)), route.name


def _owned_routes(api_module, role: AppRole) -> list[APIRoute]:
    routes = [route for route in api_module.router.routes if isinstance(route, APIRoute)]
    keys = [_route_key(route) for route in routes]
    known = _CONTROL_ROUTE_KEYS | _DATAPLANE_ROUTE_KEYS
    actual = set(keys)
    overlap = _CONTROL_ROUTE_KEYS & _DATAPLANE_ROUTE_KEYS
    missing = actual - known
    stale = known - actual
    duplicates = {key for key in keys if keys.count(key) > 1}
    if overlap or missing or stale or duplicates:
        raise RuntimeError(
            "invalid app role route ownership: "
            f"overlap={sorted(overlap)!r}, missing={sorted(missing)!r}, "
            f"stale={sorted(stale)!r}, duplicates={sorted(duplicates)!r}"
        )

    selected = {
        "all": known,
        "dataplane": _DATAPLANE_ROUTE_KEYS,
        "control": _CONTROL_ROUTE_KEYS,
    }[role]
    return [route for route in routes if _route_key(route) in selected]


def _include_routes(app: FastAPI, routes: Sequence[APIRoute]) -> None:
    if not routes:
        return
    for route in routes:
        cloned = copy(route)
        cloned.dependency_overrides_provider = app
        cloned.app = request_response(cloned.get_route_handler())
        app.router.routes.append(cloned)


def _mount_static(app: FastAPI, api_module) -> None:
    if api_module._LOGO_DIR.exists():
        app.mount("/logos", StaticFiles(directory=str(api_module._LOGO_DIR)), name="logos")
    if api_module._MEDIA_DIR.exists():
        app.mount("/media", StaticFiles(directory=str(api_module._MEDIA_DIR)), name="media")
    if api_module._TOUR_DIR.exists():
        app.mount(
            "/dashboard-tour",
            StaticFiles(directory=str(api_module._TOUR_DIR), html=True),
            name="dashboard-tour",
        )
    if api_module._VENDOR_DIR.exists():
        app.mount(
            "/vendor",
            _ImmutableStatic(directory=str(api_module._VENDOR_DIR)),
            name="vendor",
        )


def _include_role_routes(app: FastAPI, api_module, role: AppRole) -> None:
    pending: list[APIRoute] = []
    for route in _owned_routes(api_module, role):
        if role != "dataplane" and _route_key(route) == _STATIC_ANCHOR:
            _include_routes(app, pending)
            pending = []
            _mount_static(app, api_module)
        pending.append(route)
    _include_routes(app, pending)


def _install_head_and_openapi(app: FastAPI) -> None:
    """Answer HEAD wherever GET works without advertising duplicate OpenAPI operations."""
    widened: list[APIRoute] = []
    for route in app.routes:
        if isinstance(route, APIRoute) and route.methods == {"GET"}:
            route.methods = {"GET", "HEAD"}
            widened.append(route)

    fastapi_openapi = app.openapi

    def openapi_without_head():
        if app.openapi_schema:
            return app.openapi_schema
        for route in widened:
            route.methods = {"GET"}
        try:
            app.openapi_schema = fastapi_openapi()
        finally:
            for route in widened:
                route.methods = {"GET", "HEAD"}
        return app.openapi_schema

    app.openapi = openapi_without_head


def _route_manifest(routes: Sequence[BaseRoute]) -> list[str]:
    result = []
    for route in routes:
        if isinstance(route, Mount):
            result.append(f"MOUNT {route.path}")
            continue
        methods = ",".join(sorted(getattr(route, "methods", ()) or ()))
        result.append(f"{methods} {route.path}")
    return result


def _lifespan(role: AppRole):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await verify_db()

        limits = httpx.Limits(max_keepalive_connections=100, max_connections=200)
        app.state.http = httpx.AsyncClient(
            limits=limits,
            timeout=httpx.Timeout(float(get_settings().call_timeout_s)),
        )
        ads_task = (
            asyncio.create_task(adsconv.worker(session_maker, app.state.http))
            if ROLE_BACKGROUND_TASKS[role] and adsconv.enabled()
            else None
        )
        # The archive's refresh worker (docs/context/architecture/archive.md): serve mode only,
        # and a zero daily cap disables it without touching serving. Same discipline as the ads
        # task — in-process, cancelled on shutdown, a bad pass never kills the loop.
        archive_task = (
            asyncio.create_task(archive.refresh_worker(app.state.http))
            if ROLE_BACKGROUND_TASKS[role] and archive.worker_enabled()
            else None
        )
        endpoint_observations = app.state.endpoint_observation_reader
        routed_call.configure_endpoint_observation_reader(endpoint_observations)
        mcp_reader_bound = role != "control" and _mcp is not None
        if mcp_reader_bound:
            _mcp.configure_endpoint_observation_reader(endpoint_observations)
        fault_handler = analytics.install_fault_handler()
        try:
            if role == "control" or _mcp is None:
                yield
            elif get_settings().claude_connector_enabled:
                async with _mcp.all_mcp_lifespans():
                    yield
            else:
                async with _mcp.mcp_lifespan():
                    yield
        finally:
            try:
                if ads_task is not None:
                    ads_task.cancel()
                if archive_task is not None:
                    archive_task.cancel()
                if mcp_reader_bound:
                    _mcp.clear_endpoint_observation_reader(endpoint_observations)
                routed_call.clear_endpoint_observation_reader(endpoint_observations)
                await endpoint_observations.aclose()
                await audit.drain()
                await analytics.drain()
                await archive.drain()
                await app.state.http.aclose()
            finally:
                analytics.remove_fault_handler(fault_handler)

    return lifespan


def create_app(role: AppRole = "all") -> FastAPI:
    """Assemble one role from api.py's route definitions through an explicit factory."""
    if role not in ("all", "dataplane", "control"):
        raise ValueError(f"unknown app role: {role!r}")

    # Deferred to avoid a cycle: api.py defines the router, then calls this factory for api:app.
    from . import api as api_module

    expose_docs = role != "dataplane"
    app = FastAPI(
        title="treg",
        version="0.0.1",
        lifespan=_lifespan(role),
        openapi_url="/openapi.json" if expose_docs else None,
        docs_url="/docs/api" if expose_docs else None,
        redoc_url=None,
    )
    app.state.endpoint_observation_reader = CachedEndpointObservationReader(
        PostgresEndpointObservationReader(session_maker)
    )

    # Registration order is part of the compatibility surface. add_middleware prepends entries.
    app.add_middleware(_LegacyHostRedirectMiddleware)
    app.add_middleware(_SecurityHeadersMiddleware)
    app.add_middleware(_BodyDecodeMiddleware)
    app.add_exception_handler(OverflowError, api_module._id_out_of_range)
    bootstrap_handlers._stamp_call_exit = call_routes._stamp_call_exit
    app.add_exception_handler(PoolTimeoutError, bootstrap_handlers._pool_saturated)
    app.add_exception_handler(StarletteHTTPException, bootstrap_handlers._mark_treg_own_errors)

    _include_role_routes(app, api_module, role)
    _install_head_and_openapi(app)

    if role != "control" and _mcp is not None:
        if get_settings().claude_connector_enabled:
            # Claude can remove the final slash. Normalize before the parent V1 mount can match.
            app.add_middleware(_mcp.NormalizeDirectoryMCPPath)
            # Register the nested mount first so the /mcp parent does not consume it.
            app.mount("/mcp/v2", _mcp.directory_mcp_app)
        app.mount("/mcp", _mcp.mcp_app)

    startup_checks = list(ROLE_STARTUP_CHECKS[role])
    if _mcp is None and "treg.mcp.mcp_lifespan" in startup_checks:
        startup_checks.remove("treg.mcp.mcp_lifespan")
    app.state.role = role
    app.state.role_manifest = {
        "routes": _route_manifest(app.routes),
        "background_tasks": list(ROLE_BACKGROUND_TASKS[role]),
        "startup_checks": startup_checks,
    }
    return app
