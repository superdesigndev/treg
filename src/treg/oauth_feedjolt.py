"""Feedjolt key-provider entry.

Lives in its own module so the listing can ship without rewriting
the 140k-line oauth_providers.py in one API call. Importing this module
registers FEEDJOLT on oauth_providers.REGISTRY.
"""

from __future__ import annotations

from .oauth_providers import OAuthProvider, REGISTRY

FEEDJOLT = OAuthProvider(
    service="feedjolt",
    display_name="Feedjolt",
    auth_kind="key",
    token_label="API key",
    token_placeholder="your Feedjolt API key (fjk_…)",
    # Published docs use Authorization: Bearer. OpenAPI also lists X-API-Key.
    token_header="Authorization",
    token_format="Bearer {secret}",
    setup_url="https://www.feedjolt.com/en/docs/developers",
    setup_action_label="Get your Feedjolt API key",
    setup_steps=(
        "Sign in to Feedjolt (Startup or Scale; the REST API is in the premium set).",
        "Open the workspace dashboard and create an API key.",
        "Copy the key. It starts with fjk_.",
    ),
    setup_note=(
        "Main agent auth is OAuth 2.1 on the MCP servers (DCR/PKCE; not this catalog). "
        "This listing is REST: API keys (fjk_) also work. REST calls are included in the plan, not billed per call. "
        "GET /workspaces is the probe. Do not use GET /health — it is unauthenticated. "
        "A garbage Bearer/X-API-Key returns 401 {\"detail\":\"Invalid or revoked API key\"}."
    ),
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="Community",
    summary="Read customer feedback boards, posts, roadmaps and changelogs for a Feedjolt workspace.",
    base_url="https://api.feedjolt.com/api/v1",
    docs_url="https://www.feedjolt.com/en/docs/developers",
    probe_path="/workspaces",  # authenticated; GET /health is public and cannot validate a key
)

REGISTRY[FEEDJOLT.service] = FEEDJOLT
